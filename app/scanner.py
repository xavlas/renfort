"""Analyse incrémentale de la photothèque (plusieurs dossiers), en tâche de fond."""
import collections
import hashlib
import queue
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import config, db, faces, imaging
from .paths import db_path, fs_path, pretty

log = logging.getLogger("scanner")

LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", 300))   # délai laissé à un renfort pour rendre une photo


def fingerprint(path: Path, size: int) -> str:
    """Empreinte rapide (taille + début + fin du fichier) pour reconnaître un fichier déplacé."""
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    return h.hexdigest()


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in config.SKIP_DIRS]
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in config.IMAGE_EXTS:
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue
            yield full.relative_to(root).as_posix(), st.st_size, st.st_mtime


def display_folder(label: str, rel: str) -> str:
    """Dossier affiché : chemin réel du dossier source + sous-dossier."""
    sub = pretty(rel.rsplit("/", 1)[0]) if "/" in rel else ""
    return f"{label}/{sub}" if sub else label


def process_one(pid: int, path: str, with_faces: bool):
    """Travail lourd, exécuté dans un thread : décodage, miniature, visages."""
    p = Path(path)
    im, meta = imaging.open_reduced(p, max(config.FACE_DETECT_SIZE, config.THUMB_HEIGHT * 2))
    tw, th = imaging.make_thumb(im, imaging.thumb_path(pid))
    found = faces.detect(im) if with_faces else []
    try:
        fp = fingerprint(p, p.stat().st_size)
    except OSError:
        fp = None
    return {"meta": meta, "tw": tw, "th": th, "faces": found, "fp": fp}


def source_available(root: Path) -> bool:
    try:
        return root.is_dir() and any(True for _ in os.scandir(root))
    except OSError:
        return False


class Scanner:
    def __init__(self):
        self.trigger = threading.Event()
        self.running = False
        self.status = {"state": "idle", "total": 0, "done": 0, "errors": 0,
                       "last_scan": None, "message": ""}
        self.unavailable = set()        # sources injoignables lors de la dernière analyse
        self.source_errors = {}         # source -> message de la dernière erreur
        self.last_error = None
        self._added = collections.deque()  # sources ajoutées pendant une analyse
        self.queue = collections.deque()   # photos à traiter (partagée avec les renforts)
        self.leases = {}                   # photo -> (chemin, échéance, renfort)
        self.remote_results = queue.Queue()
        self.workers = {}                  # renforts vus récemment
        self.processing = False
        self._lock = threading.Lock()
        self._thread = None

    # -- planification -------------------------------------------------------
    def start(self):
        self._thread = threading.Thread(target=self._loop, name="scanner", daemon=True)
        self._thread.start()
        # reprendre un retrait interrompu par un redémarrage
        for s in db.conn().execute("SELECT id FROM sources WHERE state='removing'"):
            self.remove_source(s["id"])

    def request(self):
        self.trigger.set()

    def add_source(self, sid: int):
        """Nouvelle source : traitée tout de suite, même si une longue analyse est en cours."""
        with self._lock:
            self._added.append(sid)
        self.trigger.set()

    def _loop(self):
        time.sleep(2)
        while True:
            try:
                self.scan()
            except Exception as e:
                log.exception("Échec de l'analyse")
                self.last_error = f"{type(e).__name__}: {e}"
                self.status.update(state="idle", message="L'analyse a échoué : " + self.last_error)
                self.running = False
                try:
                    db.conn().rollback()
                except Exception:
                    pass
            interval = config.SCAN_INTERVAL_HOURS * 3600 if config.SCAN_INTERVAL_HOURS > 0 else None
            if self._added:
                continue
            self.trigger.wait(timeout=interval)
            self.trigger.clear()

    # -- analyse -------------------------------------------------------------
    def scan(self):
        self.running = True
        c = db.conn()
        with_faces = faces.available()
        if with_faces and not faces.index.loaded:
            faces.index.load()
        with self._lock:
            self._added.clear()   # une analyse complète couvre aussi les sources ajoutées
        self.status.update(state="indexing", total=0, done=0, errors=0, message="Recherche des photos…")
        self.last_error = None
        todo = []
        for src in sorted(db.sources(), key=lambda s: -s["added_ts"]):  # dossiers ajoutés récemment d'abord
            todo += self._safe_index(c, src, with_faces)
        self._process(c, todo, with_faces)
        db.meta_set("last_scan", int(time.time()))
        total = c.execute("SELECT COUNT(*) FROM photos WHERE tw IS NOT NULL").fetchone()[0]
        self.status.update(state="idle", last_scan=int(time.time()), total=0, done=0, message=f"{total} photos")
        self.running = False

    def _safe_index(self, c, src, with_faces):
        """Un dossier qui pose problème ne bloque pas les autres."""
        try:
            jobs = self._index_source(c, src, with_faces)
            self.source_errors.pop(src["id"], None)
            return jobs
        except Exception as e:
            log.exception("Échec de la lecture de %s", src["label"])
            try:
                c.rollback()
            except Exception:
                pass
            self.source_errors[src["id"]] = f"{type(e).__name__}: {e}"
            return []

    def _index_source(self, c, src, with_faces):
        """Compare un dossier source avec la base ; renvoie les photos à (re)traiter."""
        root = Path(fs_path(src["root"]))
        sid, label = src["id"], src["label"]
        if not source_available(root):
            # disque débranché ou partage réseau absent : surtout ne rien supprimer
            self.unavailable.add(sid)
            log.warning("Dossier inaccessible, ignoré : %s", label)
            return []
        self.status["message"] = f"Lecture de {label}…"

        on_disk = {}
        for rel, size, mtime in walk(root):
            on_disk[rel] = (size, mtime)
            if len(on_disk) % 2000 == 0:
                self.status["message"] = f"{label} : {len(on_disk)} fichiers trouvés…"

        prefix = str(root) + "/"
        known = {fs_path(r["path"])[len(prefix):]: r for r in c.execute(
            "SELECT id, path, size, mtime, fp, faces_done, tw, error FROM photos WHERE source_id=?", (sid,))}
        if not on_disk and known:
            self.unavailable.add(sid)
            log.warning("Aucune photo trouvée dans %s alors que %d sont connues : dossier ignoré.", label, len(known))
            return []
        self.unavailable.discard(sid)

        missing = {p: r for p, r in known.items() if p not in on_disk}
        new = [p for p in on_disk if p not in known]
        todo = []  # (id, chemin complet)

        # fichiers modifiés, ou pas encore traités
        skipped = 0
        for p, (size, mtime) in on_disk.items():
            r = known.get(p)
            if r is None:
                continue
            if r["size"] != size or abs(r["mtime"] - mtime) > 1:
                self._clear_faces(c, [r["id"]])
                c.execute("UPDATE photos SET size=?, mtime=?, fp=NULL, tw=NULL, faces_done=0, error=NULL WHERE id=?",
                          (size, mtime, r["id"]))
                todo.append((r["id"], prefix + p))
            elif not r["error"] and (r["tw"] is None or (with_faces and not r["faces_done"])):
                todo.append((r["id"], prefix + p))

        # nouveaux fichiers (ou déplacés : même empreinte qu'un fichier disparu)
        missing_by_fp = {r["fp"]: r for r in missing.values() if r["fp"]}
        now = int(time.time())
        for i, p in enumerate(new):
            size, mtime = on_disk[p]
            try:
                fp = fingerprint(root / p, size) if missing_by_fp else None
                name = pretty(p.rsplit("/", 1)[-1])
                moved = missing_by_fp.pop(fp, None) if fp else None
                if moved:
                    c.execute("UPDATE photos SET path=?, folder=?, filename=?, mtime=? WHERE id=?",
                              (db_path(prefix + p), display_folder(label, p), name, mtime, moved["id"]))
                    missing.pop(fs_path(moved["path"])[len(prefix):], None)
                    continue
                try:
                    lt = time.localtime(mtime)
                except (OverflowError, OSError, ValueError):
                    lt = time.localtime(0)
                cur = c.execute(
                    "INSERT INTO photos(path, folder, filename, size, mtime, fp, taken_ts, taken, ym, added_ts, source_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (db_path(prefix + p), display_folder(label, p), name, size, mtime, fp, int(time.mktime(lt)),
                     time.strftime("%Y-%m-%d %H:%M:%S", lt), time.strftime("%Y-%m", lt), now, sid))
                todo.append((cur.lastrowid, prefix + p))
            except Exception as e:
                skipped += 1
                log.warning("Fichier ignoré %r : %s", prefix + p, e)
                continue
            if i % 500 == 0:
                c.commit()
                self.status["message"] = f"{label} : nouvelles photos {i + 1} / {len(new)}"
        c.commit()
        if skipped:
            log.warning("%s : %d fichier(s) ignoré(s)", label, skipped)

        # fichiers supprimés
        if missing:
            self._delete_photos(c, [r["id"] for r in missing.values()])
        c.execute("UPDATE sources SET last_scan=? WHERE id=?", (now, sid))
        c.commit()
        return todo

    def _delete_photos(self, c, ids):
        self._clear_faces(c, ids)
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            c.execute(f"DELETE FROM photos WHERE id IN ({','.join('?' * len(part))})", part)
            c.commit()
        for pid in ids:
            imaging.remove_cache(pid)

    def _clear_faces(self, c, photo_ids):
        persons = set()
        for chunk in range(0, len(photo_ids), 500):
            part = photo_ids[chunk:chunk + 500]
            q = ",".join("?" * len(part))
            rows = c.execute(f"SELECT id, person_id FROM faces WHERE photo_id IN ({q})", part).fetchall()
            for r in rows:
                if r["person_id"]:
                    persons.add(r["person_id"])
                try:
                    imaging.face_path(r["id"]).unlink()
                except FileNotFoundError:
                    pass
            c.execute(f"DELETE FROM faces WHERE photo_id IN ({q})", part)
        for pid in persons:
            faces.refresh_person(c, pid)

    # -- retrait d'un dossier ------------------------------------------------
    def remove_source(self, sid: int):
        """Retire un dossier de la photothèque (les fichiers ne sont pas touchés), en tâche de fond."""
        c = db.conn()
        c.execute("UPDATE sources SET state='removing' WHERE id=?", (sid,))
        c.commit()

        def run():
            cc = db.conn()
            try:
                ids = [r["id"] for r in cc.execute("SELECT id FROM photos WHERE source_id=?", (sid,))]
                self._delete_photos(cc, ids)
                cc.execute("DELETE FROM sources WHERE id=?", (sid,))
                cc.commit()
                self.unavailable.discard(sid)
                log.info("Dossier retiré (%d photos)", len(ids))
            except Exception:
                log.exception("Échec du retrait du dossier %s", sid)

        threading.Thread(target=run, name=f"remove-{sid}", daemon=True).start()

    # -- traitement ----------------------------------------------------------
    def _take_added(self, c, with_faces):
        """Indexe les dossiers ajoutés pendant l'analyse ; renvoie leurs photos à traiter."""
        jobs = []
        while True:
            with self._lock:
                if not self._added:
                    break
                sid = self._added.popleft()
            src = c.execute("SELECT * FROM sources WHERE id=? AND state='active'", (sid,)).fetchone()
            if src:
                saved = self.status["message"]
                jobs += self._safe_index(c, src, with_faces)
                self.status["message"] = saved
        return jobs

    # -- renforts distants (ex. un Mac sur le réseau) ---------------------------
    def claim(self, worker: str, n: int):
        """Confie jusqu'à n photos à un renfort. Elles lui sont réservées LEASE_SECONDS secondes."""
        now = time.time()
        w = self.workers.setdefault(worker, {"done": 0, "since": now, "recent": collections.deque()})
        w["last"] = now
        out = []
        with self._lock:
            if not self.processing:
                return out
            while self.queue and len(out) < n:
                pid, path = self.queue.popleft()
                self.leases[pid] = (path, now + LEASE_SECONDS, worker)
                out.append((pid, path))
        return out

    def lease_path(self, pid: int, worker: str):
        with self._lock:
            lease = self.leases.get(pid)
        return lease[0] if lease and lease[2] == worker else None

    def submit(self, worker: str, pid: int, res: dict) -> bool:
        """Résultat d'un renfort : il sera enregistré par le fil d'analyse (seul à écrire en base)."""
        with self._lock:
            lease = self.leases.get(pid)
            if not lease or lease[2] != worker:
                return False   # réservation expirée : la photo a été confiée à quelqu'un d'autre
            del self.leases[pid]
        now = time.time()
        w = self.workers.setdefault(worker, {"done": 0, "since": now, "recent": collections.deque()})
        w["done"] += 1
        w["last"] = now
        w["recent"].append(now)
        while w["recent"] and w["recent"][0] < now - 60:
            w["recent"].popleft()
        self.remote_results.put((pid, lease[0], res))
        return True

    def active_workers(self):
        now = time.time()
        out = []
        for name, w in self.workers.items():
            if w.get("last", 0) > now - 90:
                recent = [t for t in w["recent"] if t > now - 60]
                out.append({"name": name, "done": w["done"], "per_min": len(recent)})
        return out

    def _expire_leases(self):
        now = time.time()
        with self._lock:
            late = [(pid, v[0]) for pid, v in self.leases.items() if v[1] < now]
            for pid, path in late:
                del self.leases[pid]
                self.queue.appendleft((pid, path))
        if late:
            log.warning("%d photo(s) non rendues par un renfort : remises dans la file", len(late))

    # -- traitement ------------------------------------------------------------
    def _process(self, c, todo, with_faces):
        added = self._take_added(c, with_faces)   # hors du verrou : _take_added le prend lui-même
        with self._lock:
            self.queue = collections.deque(added + todo)
            if not self.queue:
                return
            self.leases.clear()
            self.processing = True
        self.status.update(state="processing", total=len(self.queue), done=0, message="")
        dirty = set()
        pending = 0
        last_flush = time.monotonic()
        window = config.WORKERS * 4
        try:
            with ThreadPoolExecutor(max_workers=max(1, config.WORKERS), thread_name_prefix="worker") as ex:
                inflight = {}

                def refill():
                    while len(inflight) < window:
                        with self._lock:
                            if not self.queue:
                                return
                            job = self.queue.popleft()
                        inflight[ex.submit(process_one, job[0], job[1], with_faces)] = job

                def record(pid, path, res=None, err=None):
                    try:
                        if err is not None:
                            raise RuntimeError(err)
                        self._store(c, pid, res, with_faces, dirty)
                    except Exception as e:
                        log.warning("%s : %s", path, e)
                        self.status["errors"] += 1
                        c.execute("UPDATE photos SET error=?, faces_done=1 WHERE id=?", (str(e)[:300], pid))
                    self.status["done"] += 1

                while True:
                    refill()
                    if inflight:
                        done, _ = wait(inflight, timeout=0.5, return_when=FIRST_COMPLETED)
                    else:
                        done = ()
                        try:  # rien en local : attendre les renforts
                            item = self.remote_results.get(timeout=0.5)
                            self.remote_results.put(item)
                        except queue.Empty:
                            pass
                    for fut in done:
                        pid, path = inflight.pop(fut)
                        try:
                            record(pid, path, fut.result())
                        except Exception as e:
                            record(pid, path, err=f"{type(e).__name__}: {e}")
                        pending += 1
                    while True:  # résultats des renforts
                        try:
                            pid, path, res = self.remote_results.get_nowait()
                        except queue.Empty:
                            break
                        record(pid, path, res, res.get("error"))
                        pending += 1
                    self._expire_leases()
                    if self._added:  # un dossier vient d'être ajouté : ses photos passent devant
                        extra = self._take_added(c, with_faces)
                        with self._lock:
                            self.queue.extendleft(reversed(extra))
                        self.status["total"] += len(extra)
                    if pending >= 25 or (pending and time.monotonic() - last_flush > 5):
                        self._flush(c, dirty)
                        pending = 0
                        last_flush = time.monotonic()
                    with self._lock:
                        finished = not inflight and not self.queue and not self.leases
                    if finished and self.remote_results.empty():
                        break
        finally:
            with self._lock:
                self.processing = False
                self.queue.clear()
                self.leases.clear()
            self._flush(c, dirty)

    def _store(self, c, pid, res, with_faces, dirty):
        m = res["meta"]
        t = m["taken"]
        if isinstance(t, str):
            t = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        if res.get("thumb") is not None:  # miniature calculée par un renfort
            imaging.write_atomic(imaging.thumb_path(pid), res["thumb"])
        cur = c.execute(
            "UPDATE photos SET taken_ts=?, taken=?, ym=?, width=?, height=?, tw=?, th=?, make=?, model=?,"
            " lat=?, lon=?, faces_done=?, error=NULL, fp=COALESCE(fp, ?) WHERE id=?",
            (int(t.timestamp()), t.strftime("%Y-%m-%d %H:%M:%S"), t.strftime("%Y-%m"), m["width"], m["height"],
             res["tw"], res["th"], m.get("make"), m.get("model"), m.get("lat"), m.get("lon"),
             1 if with_faces else 0, res.get("fp"), pid))
        if cur.rowcount == 0:  # photo retirée entre-temps
            imaging.remove_cache(pid)
            return
        for (x, y, w, h, score, emb, crop) in res["faces"]:
            person, _ = faces.index.assign(emb)
            if person is None:
                person = faces.new_person(c)
            faces.index.add(person, emb)
            dirty.add(person)
            cur = c.execute("INSERT INTO faces(photo_id, x, y, w, h, score, emb, person_id) VALUES(?,?,?,?,?,?,?,?)",
                            (pid, x, y, w, h, score, emb.tobytes(), person))
            try:
                if isinstance(crop, (bytes, bytearray)):
                    imaging.write_atomic(imaging.face_path(cur.lastrowid), bytes(crop))
                else:
                    img = Image.fromarray(crop)
                    img.thumbnail((200, 200))
                    img.save(imaging.face_path(cur.lastrowid), "WEBP", quality=80)
            except Exception:
                pass

    def _flush(self, c, dirty):
        for pid in dirty:
            faces.refresh_person(c, pid, recompute_sum=False)
        dirty.clear()
        c.commit()


scanner = Scanner()
