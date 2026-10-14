"""Détection et reconnaissance des visages (OpenCV YuNet + SFace) et regroupement en personnes."""
import hashlib
import logging
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from . import config, db

log = logging.getLogger("faces")

MODELS = {
    "yunet": ("face_detection_yunet_2023mar.onnx",
              "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
              "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "sface": ("face_recognition_sface_2021dec.onnx",
              "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
              "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}

_local = threading.local()
_available = None


def _sha256(p: Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_path(key) -> Path:
    name, url, sha = MODELS[key]
    for d in (config.MODELS_DIR, config.DATA_DIR / "models"):
        p = d / name
        if p.exists() and p.stat().st_size > 10_000:
            return p
    dest_dir = config.DATA_DIR / "models"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    log.info("Téléchargement du modèle %s", name)
    tmp = dest.with_suffix(".part")
    urllib.request.urlretrieve(url, tmp)
    if _sha256(tmp) != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Empreinte invalide pour {name}")
    tmp.rename(dest)
    return dest


def available():
    """Vrai si la reconnaissance faciale est activée et les modèles chargeables."""
    global _available
    if _available is None:
        if not config.ENABLE_FACES:
            _available = False
        else:
            try:
                import cv2  # noqa: F401
                model_path("yunet")
                model_path("sface")
                _available = True
            except Exception as e:
                log.error("Reconnaissance faciale désactivée : %s", e)
                _available = False
    return _available


def _models():
    m = getattr(_local, "models", None)
    if m is None:
        import cv2
        cv2.setLogLevel(2) if hasattr(cv2, "setLogLevel") else None
        det = cv2.FaceDetectorYN.create(str(model_path("yunet")), "", (320, 320),
                                        config.FACE_MIN_SCORE, 0.3, 5000)
        rec = cv2.FaceRecognizerSF.create(str(model_path("sface")), "")
        m = _local.models = (det, rec)
    return m


def detect(im):
    """Renvoie [(x, y, w, h, score, embedding, crop_rgb)] avec coordonnées relatives (0-1)."""
    import cv2
    det, rec = _models()
    rgb = np.asarray(im)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    H, W = bgr.shape[:2]
    det.setInputSize((W, H))
    _, found = det.detect(bgr)
    out = []
    if found is None:
        return out
    for f in found:
        x, y, w, h = (float(v) for v in f[:4])
        score = float(f[14])
        if score < config.FACE_MIN_SCORE or min(w, h) < config.FACE_MIN_PX:
            continue
        aligned = rec.alignCrop(bgr, f)
        emb = rec.feature(aligned).astype(np.float32).ravel()
        n = np.linalg.norm(emb)
        if n == 0:
            continue
        emb /= n
        # vignette carrée avec marge, pour l'affichage
        cx, cy, side = x + w / 2, y + h / 2, max(w, h) * 1.5
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        crop = rgb[y0:y1, x0:x1]
        x, y = max(0.0, x), max(0.0, y)
        out.append((x / W, y / H, min(w, W - x) / W, min(h, H - y) / H, score, emb, crop))
    return out


class PersonIndex:
    """Index en mémoire des « personnes » (groupes de visages), pour l'affectation incrémentale."""

    def __init__(self):
        self.lock = threading.RLock()
        self.pos = {}                                   # person_id -> ligne
        self.ids = np.zeros(0, np.int64)
        self.sums = np.zeros((0, 128), np.float32)      # somme des embeddings
        self.cent = np.zeros((0, 128), np.float32)      # centroïdes normalisés
        self.counts = np.zeros(0, np.int64)
        self.n = 0
        self.loaded = False

    def load(self):
        with self.lock:
            self.__init__()
            for r in db.conn().execute("SELECT id, emb_sum, face_count FROM persons WHERE emb_sum IS NOT NULL"):
                self._put(r["id"], np.frombuffer(r["emb_sum"], np.float32), r["face_count"])
            self.loaded = True

    def _grow(self):
        cap = max(64, len(self.ids) * 2)
        for name, shape, dt in (("ids", (cap,), np.int64), ("sums", (cap, 128), np.float32),
                                ("cent", (cap, 128), np.float32), ("counts", (cap,), np.int64)):
            old = getattr(self, name)
            new = np.zeros(shape, dt)
            new[:self.n] = old[:self.n]
            setattr(self, name, new)

    def _norm(self, i):
        v = self.sums[i]
        n = np.linalg.norm(v)
        self.cent[i] = v / n if n else v

    def _put(self, pid, emb_sum, count):
        i = self.pos.get(pid)
        if i is None:
            if self.n == len(self.ids):
                self._grow()
            i = self.n
            self.n += 1
            self.pos[pid] = i
            self.ids[i] = pid
        self.sums[i] = emb_sum
        self.counts[i] = count
        self._norm(i)

    def _remove(self, pid):
        i = self.pos.pop(pid, None)
        if i is None:
            return
        last = self.n - 1
        if i != last:
            moved = int(self.ids[last])
            for arr in (self.ids, self.sums, self.cent, self.counts):
                arr[i] = arr[last]
            self.pos[moved] = i
        self.n -= 1

    def assign(self, emb):
        """Renvoie (person_id ou None, similarité) pour le groupe le plus proche."""
        with self.lock:
            if not self.n:
                return None, 0.0
            sims = self.cent[:self.n] @ emb
            i = int(np.argmax(sims))
            if sims[i] >= config.FACE_MATCH:
                return int(self.ids[i]), float(sims[i])
            return None, float(sims[i])

    def add(self, pid, emb):
        with self.lock:
            i = self.pos.get(pid)
            if i is None:
                self._put(pid, emb.copy(), 1)
            else:
                self.sums[i] += emb
                self.counts[i] += 1
                self._norm(i)

    def set(self, pid, emb_sum, count):
        with self.lock:
            if not count or emb_sum is None:
                self._remove(pid)
            else:
                self._put(pid, emb_sum, count)

    def get_sum(self, pid):
        with self.lock:
            i = self.pos.get(pid)
            if i is None:
                return None, 0
            return self.sums[i].copy(), int(self.counts[i])

    def similar(self, emb, k=5):
        """Personnes nommées les plus proches d'un visage (pour suggérer un nom)."""
        with self.lock:
            if not self.n:
                return []
            sims = self.cent[:self.n] @ emb
            order = np.argsort(-sims)[: k * 8]
            return [(int(self.ids[i]), float(sims[i])) for i in order]


index = PersonIndex()


def new_person(c, name=None):
    cur = c.execute("INSERT INTO persons(name, created_ts) VALUES(?, ?)", (name, int(time.time())))
    return cur.lastrowid


def refresh_person(c, pid, recompute_sum=True):
    """Recalcule compteurs, vignette et centroïde d'une personne. Supprime les groupes anonymes vides."""
    row = c.execute("SELECT name FROM persons WHERE id=?", (pid,)).fetchone()
    if not row:
        index.set(pid, None, 0)
        return
    face_count, photo_count = c.execute(
        "SELECT COUNT(*), COUNT(DISTINCT photo_id) FROM faces WHERE person_id=? AND rejected=0", (pid,)).fetchone()
    if not face_count and row["name"] is None:
        c.execute("DELETE FROM persons WHERE id=?", (pid,))
        index.set(pid, None, 0)
        return
    if recompute_sum:
        emb_sum = None
        for f in c.execute("SELECT emb FROM faces WHERE person_id=? AND rejected=0 AND emb IS NOT NULL", (pid,)):
            e = np.frombuffer(f["emb"], np.float32)
            emb_sum = e.copy() if emb_sum is None else emb_sum + e
    else:
        emb_sum, _ = index.get_sum(pid)
    cover = c.execute("SELECT id FROM faces WHERE person_id=? AND rejected=0 ORDER BY w*h*score DESC LIMIT 1",
                      (pid,)).fetchone()
    c.execute("UPDATE persons SET emb_sum=?, face_count=?, photo_count=?, cover_face_id=? WHERE id=?",
              (emb_sum.tobytes() if emb_sum is not None else None, face_count, photo_count,
               cover["id"] if cover else None, pid))
    index.set(pid, emb_sum, face_count)


def merge(c, src, dst):
    if src == dst:
        return
    c.execute("UPDATE faces SET person_id=? WHERE person_id=?", (dst, src))
    c.execute("DELETE FROM persons WHERE id=?", (src,))
    index.set(src, None, 0)
    refresh_person(c, dst)


def person_by_name(c, name, create=True):
    name = name.strip()
    row = c.execute("SELECT id FROM persons WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return new_person(c, name) if create else None
