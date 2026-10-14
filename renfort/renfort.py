"""Renfort de la Photothèque : analyse des photos sur une machine plus puissante (ex. un Mac).

Le renfort demande des photos au Pi, les analyse (date, miniature, visages) et renvoie les
résultats. Le Pi reste seul à écrire dans sa base. On peut l'arrêter à tout moment (Ctrl+C) :
les photos en cours sont rendues au Pi au bout de quelques minutes.

Si le NAS est aussi monté sur cette machine (ex. dans /Volumes sur un Mac), le renfort y lit
les photos directement au lieu de les faire passer par le Pi : c'est nettement plus rapide.

Usage : python renfort.py [--serveur http://10.0.0.228:2342] [--fils 8] [--nom Mac] [--nas /Volumes/photos]
"""
import argparse
import base64
import getpass
import hashlib
import http.client
import io
import json
import os
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

HOME = Path(os.environ.get("RENFORT_DIR", Path.home() / ".phototheque-renfort"))


class HttpError(Exception):
    def __init__(self, code, body):
        super().__init__(f"HTTP {code}")
        self.code, self.body = code, body


class Api:
    """Client HTTP minimal : une connexion persistante par fil, reconnexion automatique."""

    def __init__(self, base, name):
        u = urllib.parse.urlsplit(base if "://" in base else "http://" + base)
        self.base = f"{u.scheme}://{u.netloc}"
        self.host, self.port, self.https = u.hostname, u.port, u.scheme == "https"
        self.name = name
        self.cookie = None
        self.password = os.environ.get("PHOTOTHEQUE_PASSWORD")
        self.local = threading.local()
        self.lock = threading.Lock()

    def _conn(self):
        c = getattr(self.local, "c", None)
        if c is None:
            cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
            c = self.local.c = cls(self.host, self.port, timeout=120)
        return c

    def _req(self, method, path, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"X-Worker": self.name}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in (1, 2):  # une connexion inactive a pu être fermée par le Pi : on réessaie une fois
            conn = self._conn()
            try:
                conn.request(method, path, body=data, headers=headers)
                r = conn.getresponse()
                payload = r.read()
                break
            except (http.client.HTTPException, OSError):
                conn.close()
                self.local.c = None
                if attempt == 2:
                    raise
        for k, v in r.getheaders():
            if k.lower() == "set-cookie" and v.startswith("pt_session="):
                self.cookie = v.split(";", 1)[0]
        if r.status >= 400:
            raise HttpError(r.status, payload)
        return (payload, r) if raw else json.loads(payload or b"null")

    def call(self, method, path, body=None, raw=False):
        """Requête avec reconnexion et nouvelles tentatives si le réseau flanche."""
        delay = 2
        while True:
            try:
                return self._req(method, path, body, raw)
            except HttpError as e:
                if e.code == 401 and not path.startswith("/api/login"):
                    self.login(force=True)
                    continue
                if e.code not in (502, 503, 504):
                    raise
                print(f"  Le Pi est occupé ({e.code}), nouvel essai dans {delay} s…", flush=True)
            except (http.client.HTTPException, OSError) as e:
                print(f"  Pi injoignable ({e}), nouvel essai dans {delay} s…", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)

    def login(self, force=False):
        with self.lock:
            state = self._req("GET", "/api/auth")
            if not state["required"] or (state["ok"] and not force):
                return
            for _ in range(3):
                if not self.password:
                    self.password = getpass.getpass("Mot de passe de la photothèque : ")
                try:
                    self._req("POST", "/api/login", {"password": self.password})
                    return
                except HttpError as e:
                    try:
                        print("  " + json.loads(e.body).get("detail", "Connexion refusée."))
                    except Exception:
                        print("  Connexion refusée.")
                    self.password = None
            sys.exit("Connexion impossible.")


class LocalNas:
    """Retrouve sur cette machine les fichiers que le Pi voit sur le NAS (ex. /mnt/nas_photos/... sur
    le Pi = /Volumes/photo/... sur le Mac). La correspondance est apprise toute seule, à partir du
    nom et de la taille des fichiers."""

    def __init__(self, roots):
        self.bases = []
        for r in roots:
            p = Path(r)
            if p.is_dir():
                self.bases.append(p)
                try:
                    self.bases += [c for c in p.iterdir() if c.is_dir() and not c.is_symlink()]
                except OSError:
                    pass
        self.maps = []        # (préfixe sur le Pi, dossier local)
        self.misses = 0
        self.lock = threading.Lock()

    @staticmethod
    def _same(p: Path, size) -> bool:
        try:
            return p.is_file() and p.stat().st_size == size
        except OSError:
            return False

    def _variants(self, base: Path, rel: str):
        yield base / rel
        nfd = unicodedata.normalize("NFD", rel)
        if nfd != rel:
            yield base / nfd

    def find(self, host_path, size):
        if not host_path or not size or not self.bases:
            return None
        if any(0xF780 <= ord(ch) <= 0xF7FF for ch in host_path):
            return None  # nom à l'ancien encodage : on passe par le Pi
        for pi_prefix, base in list(self.maps):
            if host_path.startswith(pi_prefix + "/"):
                for cand in self._variants(base, host_path[len(pi_prefix) + 1:]):
                    if self._same(cand, size):
                        return cand
        with self.lock:
            if self.misses > 40:  # le NAS n'est visiblement pas monté ici
                return None
            parts = host_path.strip("/").split("/")
            for base in self.bases:
                for k in range(1, len(parts)):
                    for cand in self._variants(base, "/".join(parts[k:])):
                        if self._same(cand, size):
                            pi_prefix = "/" + "/".join(parts[:k])
                            if (pi_prefix, base) not in self.maps:
                                self.maps.append((pi_prefix, base))
                                print(f"NAS trouvé sur cette machine : {pi_prefix} (Pi) = {base} (ici). "
                                      "Les photos y seront lues directement.", flush=True)
                            return cand
            self.misses += 1
        return None


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Renfort d'analyse pour la Photothèque")
    ap.add_argument("--serveur", default=os.environ.get("PHOTOTHEQUE", "http://10.0.0.228:2342"))
    ap.add_argument("--fils", type=int, default=max(2, (os.cpu_count() or 4) - 1),
                    help="photos analysées en parallèle (par défaut : nombre de cœurs - 1)")
    ap.add_argument("--nom", default=socket.gethostname().split(".")[0] or "Mac")
    ap.add_argument("--nas", action="append",
                    help="dossier où le NAS est monté sur cette machine (par défaut : cherché dans /Volumes)")
    ap.add_argument("--sans-nas", action="store_true", help="toujours faire passer les photos par le Pi")
    args = ap.parse_args()

    api = Api(args.serveur, args.nom)
    print(f"Connexion à {api.base}…", flush=True)
    try:
        api.login()
        cfg = api.call("GET", "/api/work/config")
    except (OSError, http.client.HTTPException, HttpError) as e:
        sys.exit(f"Impossible de joindre la photothèque à {api.base} : {e}")

    # réglages identiques à ceux du Pi, puis chargement des modules de l'application
    HOME.mkdir(parents=True, exist_ok=True)
    os.environ.update(DATA_DIR=str(HOME), MODELS_DIR=str(HOME / "models"),
                      ENABLE_FACES="1" if cfg["faces"] else "0",
                      FACE_DETECT_SIZE=str(cfg["face_detect_size"]), FACE_MIN_SCORE=str(cfg["face_min_score"]),
                      FACE_MIN_PX=str(cfg["face_min_px"]), THUMB_HEIGHT=str(cfg["thumb_height"]))
    here = Path(__file__).resolve().parent
    sys.path[:0] = [str(here), str(here.parent)]   # app/ à côté (paquet du renfort) ou au-dessus (dépôt Git)
    import cv2
    from PIL import Image
    from app import faces, imaging

    cv2.setNumThreads(1)  # le parallélisme se fait par photo
    if cfg["faces"]:
        (HOME / "models").mkdir(exist_ok=True)
        for key in cfg["models"]:
            dest = HOME / "models" / faces.MODELS[key][0]
            if not dest.exists() or sha256(dest) != faces.MODELS[key][2]:
                print(f"Téléchargement du modèle {dest.name} depuis le Pi…", flush=True)
                data, _ = api.call("GET", f"/api/work/model/{key}", raw=True)
                dest.write_bytes(data)
                if sha256(dest) != faces.MODELS[key][2]:
                    sys.exit(f"Modèle {dest.name} corrompu.")
        if not faces.available():
            sys.exit("Les modèles de visages n'ont pas pu être chargés.")
    detect_size = max(cfg["face_detect_size"], cfg["thumb_height"] * 2)
    nas = LocalNas([] if args.sans_nas else (args.nas or ["/Volumes"]))
    stats = {"local": 0, "pi": 0}

    def read(job):
        local = nas.find(job.get("host_path"), job.get("size"))
        if local is not None:
            try:
                data = local.read_bytes()
                if len(data) == job["size"]:
                    stats["local"] += 1
                    return data, job.get("mtime") or local.stat().st_mtime
            except OSError:
                pass
        data, r = api.call("GET", f"/api/work/file/{job['id']}", raw=True)
        stats["pi"] += 1
        return data, float(r.getheader("X-Mtime") or job.get("mtime") or 0)

    def analyse(job):
        pid = job["id"]
        try:
            data, mtime = read(job)
        except HttpError as e:
            return pid, f"fichier indisponible ({e.code})"
        try:
            im, meta = imaging.open_reduced(io.BytesIO(data), detect_size, name=job["name"], mtime=mtime)
            buf = io.BytesIO()
            thumb = imaging.thumb_image(im)
            thumb.save(buf, "WEBP", quality=78, method=4)
            found = []
            for (x, y, w, h, score, emb, crop) in (faces.detect(im) if cfg["faces"] else []):
                c = Image.fromarray(crop)
                c.thumbnail((200, 200))
                cb = io.BytesIO()
                c.save(cb, "WEBP", quality=80)
                found.append({"box": [x, y, w, h], "score": score,
                              "emb": base64.b64encode(emb.astype("float32").tobytes()).decode(),
                              "crop": base64.b64encode(cb.getvalue()).decode()})
            result = {
                "meta": {"taken": meta["taken"].strftime("%Y-%m-%d %H:%M:%S"), "width": meta["width"],
                         "height": meta["height"], "make": meta["make"], "model": meta["model"],
                         "lat": meta["lat"], "lon": meta["lon"]},
                "tw": thumb.size[0], "th": thumb.size[1], "thumb": base64.b64encode(buf.getvalue()).decode(),
                "faces": found, "fp": imaging.fingerprint_bytes(data)}
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        try:
            api.call("POST", f"/api/work/result/{pid}", result)
        except HttpError as e:
            return pid, f"résultat refusé ({e.code})"
        return pid, result.get("error")

    print(f"Renfort « {args.nom} » prêt : {args.fils} photos en parallèle. Ctrl+C pour arrêter.", flush=True)
    done = errors = 0
    t0 = time.time()
    last_print = time.time()
    waiting = False
    target = args.fils * 2   # photos en cours ou en attente sur ce renfort
    with ThreadPoolExecutor(max_workers=args.fils) as ex:
        inflight = set()
        while True:
            if len(inflight) < target:
                jobs = api.call("POST", f"/api/work/claim?n={min(64, target - len(inflight))}")
                for j in jobs:
                    inflight.add(ex.submit(analyse, j))
                if jobs and waiting:
                    print("Reprise de l'analyse.", flush=True)
                    waiting, t0, done = False, time.time(), 0
                if not jobs and not inflight:
                    if not waiting:
                        print("Rien à analyser pour l'instant. J'attends de nouvelles photos…", flush=True)
                        waiting = True
                    time.sleep(15)
                    continue
            finished, inflight = wait(inflight, timeout=10, return_when=FIRST_COMPLETED)
            for fut in finished:
                pid, err = fut.result()
                done += 1
                if err:
                    errors += 1
                    print(f"  photo {pid} : {err}", flush=True)
            if time.time() - last_print > 15:
                rate = done / max(1e-6, time.time() - t0) * 60
                src = f"lues sur le NAS : {stats['local']}, via le Pi : {stats['pi']}"
                print(f"{done} photos analysées ({rate:.0f}/min ; {src})"
                      f"{f', {errors} en erreur' if errors else ''}", flush=True)
                last_print = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nRenfort arrêté. Les photos qu'il n'a pas finies seront reprises par le Pi.")
