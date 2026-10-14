"""Photothèque : serveur web (API + interface)."""
import hashlib
import hmac
import base64
import binascii
import collections
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, faces, imaging
from .paths import db_path, fs_path, pretty
from .scanner import scanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


class _Recent(logging.Handler):
    """Garde les derniers avertissements et erreurs, consultables depuis l'interface."""

    def __init__(self, size=200):
        super().__init__(logging.WARNING)
        self.lines = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%d/%m %H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)
            self.lines.append(msg.encode("utf-8", "backslashreplace").decode("utf-8"))
        except Exception:
            pass


recent_log = _Recent()
logging.getLogger().addHandler(recent_log)
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    db.init()
    if faces.available():
        faces.index.load()
    scanner.start()
    yield


app = FastAPI(title="Photothèque", lifespan=lifespan, docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- accès

_secret_file = config.DATA_DIR / "secret"
if not _secret_file.exists():
    _secret_file.write_text(secrets.token_hex(32))
_SECRET = _secret_file.read_text().strip().encode()
_TOKEN = hmac.new(_SECRET, config.APP_PASSWORD.encode(), hashlib.sha256).hexdigest()
_failures: dict = {}
PUBLIC = ("/static/", "/api/login", "/api/auth", "/favicon")


def _authed(request: Request) -> bool:
    if not config.APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get("pt_session", ""), _TOKEN)


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    path = request.url.path
    if path == "/" or path.startswith(PUBLIC) or _authed(request):
        return await call_next(request)
    return JSONResponse({"detail": "Connexion requise"}, status_code=401)


@app.get("/api/auth")
def auth_state(request: Request):
    return {"required": bool(config.APP_PASSWORD), "ok": _authed(request)}


@app.post("/api/login")
def login(request: Request, payload: dict = Body(...)):
    ip = request.client.host if request.client else "?"
    fails = [t for t in _failures.get(ip, []) if t > time.time() - 900]
    if len(fails) >= 10:
        raise HTTPException(429, "Trop de tentatives. Réessayez dans 15 minutes.")
    if not hmac.compare_digest(str(payload.get("password", "")), config.APP_PASSWORD):
        fails.append(time.time())
        _failures[ip] = fails
        time.sleep(1)
        raise HTTPException(401, "Mot de passe incorrect.")
    _failures.pop(ip, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("pt_session", _TOKEN, max_age=365 * 86400, httponly=True, samesite="lax")
    return resp


# --------------------------------------------------------------------------- filtres

def _filters(person: Optional[str], date_from: Optional[str], date_to: Optional[str],
             folder: Optional[str], camera: Optional[str], q: Optional[str]):
    where, params = ["p.tw IS NOT NULL"], []
    if person:
        ids = [int(x) for x in person.split(",") if x.strip().isdigit()]
        if ids:
            where.append(f"p.id IN (SELECT photo_id FROM faces WHERE person_id IN ({','.join('?' * len(ids))})"
                         " AND rejected=0 GROUP BY photo_id HAVING COUNT(DISTINCT person_id)=?)")
            params += ids + [len(ids)]
    if date_from:
        where.append("p.taken >= ?")
        params.append(date_from[:10])
    if date_to:
        where.append("p.taken < ?")
        params.append(date_to[:10] + " 99")  # inclut toute la journée
    if folder is not None and folder != "":
        where.append("(p.folder = ? OR p.folder LIKE ? ESCAPE '\\')")
        esc = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params += [folder, esc + "/%"]
    if camera:
        where.append("p.model = ?")
        params.append(camera)
    if q:
        for word in q.split():
            esc = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(p.folder || '/' || p.filename LIKE ? ESCAPE '\\')")
            params.append(f"%{esc}%")
    return " AND ".join(where), params


def _filter_args(person=None, date_from=None, date_to=None, folder=None, camera=None, q=None):
    return dict(person=person, date_from=date_from, date_to=date_to, folder=folder, camera=camera, q=q)


# --------------------------------------------------------------------------- photos

@app.get("/api/photos")
def list_photos(limit: int = Query(200, le=1000), cursor: Optional[str] = None, before: Optional[str] = None,
                person: Optional[str] = None, date_from: Optional[str] = Query(None, alias="from"),
                date_to: Optional[str] = Query(None, alias="to"), folder: Optional[str] = None,
                camera: Optional[str] = None, q: Optional[str] = None):
    where, params = _filters(person, date_from, date_to, folder, camera, q)
    if cursor:
        ts, pid = (int(x) for x in cursor.split(":"))
        where += " AND (p.taken_ts < ? OR (p.taken_ts = ? AND p.id < ?))"
        params += [ts, ts, pid]
    elif before:
        where += " AND p.taken < ?"
        params.append(before[:10] + " 99")
    rows = db.conn().execute(
        f"SELECT p.id, p.tw, p.th, p.taken_ts, p.taken FROM photos p WHERE {where}"
        " ORDER BY p.taken_ts DESC, p.id DESC LIMIT ?", params + [limit]).fetchall()
    items = [{"id": r["id"], "w": r["tw"], "h": r["th"], "t": r["taken"]} for r in rows]
    nxt = f"{rows[-1]['taken_ts']}:{rows[-1]['id']}" if len(rows) == limit else None
    return {"items": items, "next": nxt}


@app.get("/api/timeline")
def timeline(person: Optional[str] = None, date_from: Optional[str] = Query(None, alias="from"),
             date_to: Optional[str] = Query(None, alias="to"), folder: Optional[str] = None,
             camera: Optional[str] = None, q: Optional[str] = None):
    where, params = _filters(person, date_from, date_to, folder, camera, q)
    rows = db.conn().execute(f"SELECT p.ym, COUNT(*) n FROM photos p WHERE {where} GROUP BY p.ym ORDER BY p.ym DESC",
                             params).fetchall()
    return [{"ym": r["ym"], "n": r["n"]} for r in rows]


@app.get("/api/photos/{pid}")
def photo_detail(pid: int):
    c = db.conn()
    r = c.execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "Photo introuvable")
    fs = c.execute("SELECT f.id, f.x, f.y, f.w, f.h, f.person_id, f.manual, pe.name, pe.hidden FROM faces f"
                   " LEFT JOIN persons pe ON pe.id=f.person_id WHERE f.photo_id=? AND f.rejected=0", (pid,)).fetchall()
    return {
        "id": r["id"], "path": r["path"], "folder": r["folder"], "filename": r["filename"], "taken": r["taken"],
        "width": r["width"], "height": r["height"], "size": r["size"], "make": r["make"], "model": r["model"],
        "lat": r["lat"], "lon": r["lon"], "error": r["error"],
        "faces": [{"id": f["id"], "x": f["x"], "y": f["y"], "w": f["w"], "h": f["h"], "person_id": f["person_id"],
                   "name": f["name"], "hidden": bool(f["hidden"]), "manual": bool(f["manual"])} for f in fs],
    }


def _photo_path(pid):
    r = db.conn().execute("SELECT path FROM photos WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "Photo introuvable")
    p = Path(fs_path(r["path"])).resolve()
    roots = [Path(fs_path(s["root"])).resolve() for s in db.sources(active_only=False)]
    if not any(root in p.parents for root in roots) or not p.exists():
        raise HTTPException(404, "Fichier introuvable")
    return p


IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}


@app.get("/thumb/{pid}")
def thumb(pid: int):
    p = imaging.thumb_path(pid)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/webp", headers=IMMUTABLE)


@app.get("/preview/{pid}")
def preview(pid: int):
    try:
        p = imaging.ensure_preview(pid, _photo_path(pid))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Impossible d'ouvrir cette photo : {e}")
    return FileResponse(p, media_type="image/webp", headers=IMMUTABLE)


@app.get("/original/{pid}")
def original(pid: int, download: int = 0):
    p = _photo_path(pid)
    return FileResponse(p, filename=pretty(p.name), content_disposition_type="attachment" if download else "inline")


@app.get("/face/{fid}")
def face_crop(fid: int):
    p = imaging.face_path(fid)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/webp", headers=IMMUTABLE)


# --------------------------------------------------------------------------- personnes

def _person_row(r):
    return {"id": r["id"], "name": r["name"], "hidden": bool(r["hidden"]), "faces": r["face_count"],
            "photos": r["photo_count"], "cover": r["cover_face_id"]}


@app.get("/api/persons")
def persons(named: int = 1, hidden: int = 0, limit: int = Query(500, le=5000), offset: int = 0):
    c = db.conn()
    if named:
        rows = c.execute("SELECT * FROM persons WHERE name IS NOT NULL AND hidden=? ORDER BY photo_count DESC, name"
                         " LIMIT ? OFFSET ?", (hidden, limit, offset)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM persons WHERE name IS NOT NULL AND hidden=?", (hidden,)).fetchone()[0]
    else:
        rows = c.execute("SELECT * FROM persons WHERE name IS NULL AND hidden=? AND face_count>=?"
                         " ORDER BY face_count DESC LIMIT ? OFFSET ?",
                         (hidden, config.MIN_CLUSTER, limit, offset)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM persons WHERE name IS NULL AND hidden=? AND face_count>=?",
                          (hidden, config.MIN_CLUSTER)).fetchone()[0]
    return {"items": [_person_row(r) for r in rows], "total": total}


@app.get("/api/persons/{pid}")
def person(pid: int):
    r = db.conn().execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(404, "Personne introuvable")
    return _person_row(r)


@app.patch("/api/persons/{pid}")
def update_person(pid: int, payload: dict = Body(...)):
    c = db.conn()
    if not c.execute("SELECT 1 FROM persons WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404, "Personne introuvable")
    target = pid
    if "name" in payload:
        name = (payload["name"] or "").strip()[:80]
        if name:
            other = c.execute("SELECT id FROM persons WHERE name=? AND id<>?", (name, pid)).fetchone()
            if other:  # le nom existe déjà : on regroupe
                faces.merge(c, pid, other["id"])
                target = other["id"]
            else:
                c.execute("UPDATE persons SET name=?, hidden=0 WHERE id=?", (name, pid))
        else:
            c.execute("UPDATE persons SET name=NULL WHERE id=?", (pid,))
    if "hidden" in payload:
        c.execute("UPDATE persons SET hidden=? WHERE id=?", (1 if payload["hidden"] else 0, target))
    c.commit()
    return person(target)


@app.post("/api/persons/{pid}/merge")
def merge_person(pid: int, payload: dict = Body(...)):
    into = int(payload.get("into", 0))
    c = db.conn()
    if not c.execute("SELECT 1 FROM persons WHERE id=?", (into,)).fetchone():
        raise HTTPException(404, "Personne cible introuvable")
    faces.merge(c, pid, into)
    c.commit()
    return person(into)


@app.get("/api/persons/{pid}/faces")
def person_faces(pid: int, limit: int = Query(200, le=1000), offset: int = 0):
    rows = db.conn().execute("SELECT f.id, f.photo_id, f.manual FROM faces f JOIN photos p ON p.id=f.photo_id"
                             " WHERE f.person_id=? AND f.rejected=0 ORDER BY f.manual, f.score LIMIT ? OFFSET ?",
                             (pid, limit, offset)).fetchall()
    return [{"id": r["id"], "photo": r["photo_id"], "manual": bool(r["manual"])} for r in rows]


@app.post("/api/faces/{fid}")
def update_face(fid: int, payload: dict = Body(...)):
    """Corriger un visage : {"name": "Léa"} | {"person_id": 12} | {"remove": true}."""
    c = db.conn()
    f = c.execute("SELECT id, person_id FROM faces WHERE id=?", (fid,)).fetchone()
    if not f:
        raise HTTPException(404, "Visage introuvable")
    old = f["person_id"]
    if payload.get("remove"):
        c.execute("UPDATE faces SET person_id=NULL, rejected=1, manual=1 WHERE id=?", (fid,))
        new = None
    else:
        if payload.get("name"):
            new = faces.person_by_name(c, payload["name"][:80])
            c.execute("UPDATE persons SET hidden=0 WHERE id=?", (new,))
        elif payload.get("person_id"):
            new = int(payload["person_id"])
        else:
            raise HTTPException(400, "Indiquez un nom ou une personne.")
        c.execute("UPDATE faces SET person_id=?, rejected=0, manual=1 WHERE id=?", (new, fid))
    for pid in {old, new} - {None}:
        faces.refresh_person(c, pid)
    c.commit()
    return {"ok": True, "person_id": new}


@app.get("/api/faces/{fid}/suggest")
def suggest(fid: int):
    """Noms proposés pour un visage, d'après les personnes déjà nommées."""
    c = db.conn()
    f = c.execute("SELECT emb FROM faces WHERE id=?", (fid,)).fetchone()
    if not f or not f["emb"]:
        return []
    emb = np.frombuffer(f["emb"], np.float32)
    out = []
    for pid, sim in faces.index.similar(emb):
        r = c.execute("SELECT id, name FROM persons WHERE id=? AND name IS NOT NULL AND hidden=0", (pid,)).fetchone()
        if r and sim > config.FACE_MATCH * 0.6:
            out.append({"id": r["id"], "name": r["name"], "score": round(sim, 3)})
        if len(out) >= 4:
            break
    return out


# --------------------------------------------------------------------------- divers

@app.get("/api/folders")
def folders():
    rows = db.conn().execute("SELECT folder, COUNT(*) n FROM photos WHERE tw IS NOT NULL GROUP BY folder"
                             " ORDER BY folder").fetchall()
    return [{"folder": r["folder"], "n": r["n"]} for r in rows]


@app.get("/api/cameras")
def cameras():
    rows = db.conn().execute("SELECT model, COUNT(*) n FROM photos WHERE tw IS NOT NULL AND model IS NOT NULL"
                             " GROUP BY model ORDER BY n DESC").fetchall()
    return [{"model": r["model"], "n": r["n"]} for r in rows]


_fp_cache = [0.0, 0]


def _faces_pending():
    """Photos dont les visages restent à analyser (mis en cache quelques secondes)."""
    if not faces.available():
        return 0
    if time.time() - _fp_cache[0] > 10:
        _fp_cache[1] = db.conn().execute(
            "SELECT COUNT(*) FROM photos WHERE tw IS NOT NULL AND faces_done=0 AND error IS NULL").fetchone()[0]
        _fp_cache[0] = time.time()
    return _fp_cache[1]


@app.get("/api/status")
def status():
    c = db.conn()
    s = dict(scanner.status)
    s.update(
        photos=c.execute("SELECT COUNT(*) FROM photos WHERE tw IS NOT NULL").fetchone()[0],
        people=c.execute("SELECT COUNT(*) FROM persons WHERE name IS NOT NULL AND hidden=0").fetchone()[0],
        unnamed=c.execute("SELECT COUNT(*) FROM persons WHERE name IS NULL AND hidden=0 AND face_count>=?",
                          (config.MIN_CLUSTER,)).fetchone()[0],
        faces_enabled=faces.available(),
        unavailable=[s["label"] for s in db.sources() if s["id"] in scanner.unavailable],
        last_error=scanner.last_error,
        workers=scanner.active_workers(),
        faces_pending=_faces_pending(),
        pi_faces=config.PI_FACES,
        remaining=len(scanner.queue) + len(scanner.leases),
        auth=bool(config.APP_PASSWORD),
    )
    return s


# --------------------------------------------------------------------------- dossiers analysés

def _inside(a: str, b: str) -> bool:
    """a est b, ou un sous-dossier de b (chemins affichés, ex. /mnt/nas/2010 dans /mnt/nas)."""
    a, b = a.rstrip("/") or "/", b.rstrip("/") or "/"
    return a == b or a.startswith(b if b == "/" else b + "/")


@app.get("/api/sources")
def list_sources():
    c = db.conn()
    counts = {r["source_id"]: r["n"] for r in c.execute(
        "SELECT source_id, COUNT(*) n FROM photos WHERE tw IS NOT NULL GROUP BY source_id")}
    found = {r["source_id"]: r["n"] for r in c.execute(
        "SELECT source_id, COUNT(*) n FROM photos WHERE error IS NULL OR tw IS NOT NULL GROUP BY source_id")}
    out = []
    for s in db.sources(active_only=False):
        out.append({"id": s["id"], "path": s["label"], "photos": counts.get(s["id"], 0),
                    "found": found.get(s["id"], 0), "state": s["state"], "last_scan": s["last_scan"],
                    "available": s["id"] not in scanner.unavailable,
                    "error": scanner.source_errors.get(s["id"]),
                    "removable": True, "fixed": s["root"] == str(config.PHOTOS_DIR)})
    return out


@app.post("/api/sources")
def add_source(payload: dict = Body(...)):
    raw = "/" + fs_path(str(payload.get("path", ""))).strip().strip("/")
    path = pretty(raw)
    if raw == "/":
        raise HTTPException(400, "Choisissez un dossier précis, pas la racine.")
    for s in db.sources(active_only=False):
        if _inside(path, s["label"]):
            raise HTTPException(409, f"Ce dossier fait déjà partie de la photothèque (dans {s['label']}).")
        if _inside(s["label"], path):
            raise HTTPException(409, f"Ce dossier contient {s['label']}, déjà dans la photothèque. "
                                     "Retirez d'abord ce dossier, puis ajoutez celui-ci.")
    root = db.host_to_container(raw)
    try:
        real = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Dossier introuvable sur le Pi.")
    if config.HOST_DIR.resolve() not in real.parents or not real.is_dir():
        raise HTTPException(400, "Seuls les dossiers situés dans /mnt ou /media peuvent être ajoutés.")
    c = db.conn()
    cur = c.execute("INSERT INTO sources(root, label, added_ts) VALUES(?,?,?)",
                    (db_path(str(root)), path, int(time.time())))
    c.commit()
    scanner.add_source(cur.lastrowid)
    return {"id": cur.lastrowid, "path": path}


@app.delete("/api/sources/{sid}")
def remove_source(sid: int):
    s = db.conn().execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "Dossier introuvable.")
    if s["state"] != "removing":
        scanner.remove_source(sid)
    return {"ok": True}


@app.get("/api/browse")
def browse(path: str = "/"):
    """Parcourir les dossiers du Pi visibles par l'application (lecture seule)."""
    path = "/" + fs_path(path).strip().strip("/")
    base = config.HOST_DIR.resolve()
    target = db.host_to_container(path) if path != "/" else config.HOST_DIR
    try:
        real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Dossier introuvable.")
    if real != base and base not in real.parents:
        raise HTTPException(400, "Dossier hors de portée.")
    srcs = [s["label"] for s in db.sources(active_only=False)]
    dirs, images = [], 0
    deadline = time.monotonic() + 4
    try:
        entries = sorted(os.scandir(target), key=lambda e: e.name.lower())
    except OSError as e:
        raise HTTPException(403, f"Lecture impossible : {e.strerror}")
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if e.is_dir():
                if path == "/" and e.name not in ("mnt", "media"):
                    continue
                if e.name.lower() in config.SKIP_DIRS:
                    continue
                child = (path.rstrip("/") + "/" + e.name)
                n = None
                if time.monotonic() < deadline:
                    n = _count_images(e.path, deadline)
                dirs.append({"name": pretty(e.name), "path": db_path(child), "images": n,
                             "in_library": next((s for s in srcs if _inside(pretty(child), s)), None)})
            elif os.path.splitext(e.name)[1].lower() in config.IMAGE_EXTS:
                images += 1
        except OSError:
            continue
    parent = None if path == "/" else ("/" + path.strip("/").rsplit("/", 1)[0] if "/" in path.strip("/") else "/")
    shown = pretty(path)
    return {"path": db_path(path), "label": shown, "parent": db_path(parent) if parent else None,
            "dirs": dirs, "images": images,
            "in_library": next((s for s in srcs if _inside(shown, s)), None) if path != "/" else None,
            "contains_library": [s for s in srcs if path != "/" and _inside(s, shown) and s != shown]}


@app.get("/api/logs")
def logs():
    return list(recent_log.lines)


def _count_images(p, deadline, limit=5000):
    """Nombre d'images directement dans un dossier (limité, pour rester rapide sur un NAS)."""
    n = 0
    try:
        with os.scandir(p) as it:
            for i, e in enumerate(it):
                if i >= limit or time.monotonic() > deadline:
                    return n
                if not e.name.startswith(".") and os.path.splitext(e.name)[1].lower() in config.IMAGE_EXTS:
                    n += 1
    except OSError:
        return None
    return n


# --------------------------------------------------------------------------- renforts (ex. un Mac)

def _worker_name(request: Request) -> str:
    return (request.headers.get("X-Worker") or (request.client.host if request.client else "renfort"))[:60]


@app.get("/api/work/config")
def work_config():
    return {"faces": faces.available(), "face_detect_size": config.FACE_DETECT_SIZE,
            "face_min_score": config.FACE_MIN_SCORE, "face_min_px": config.FACE_MIN_PX,
            "thumb_height": config.THUMB_HEIGHT, "models": list(faces.MODELS)}


@app.get("/api/work/model/{key}")
def work_model(key: str):
    if key not in faces.MODELS:
        raise HTTPException(404)
    return FileResponse(faces.model_path(key), media_type="application/octet-stream")


@app.post("/api/work/claim")
def work_claim(request: Request, n: int = Query(8, ge=1, le=64)):
    jobs = scanner.claim(_worker_name(request), n)
    if not jobs:
        return []
    c = db.conn()
    q = ",".join("?" * len(jobs))
    info = {r["id"]: r for r in c.execute(f"SELECT id, size, mtime FROM photos WHERE id IN ({q})", [j[0] for j in jobs])}
    out = []
    for pid, path in jobs:
        r = info.get(pid)
        out.append({"id": pid, "name": pretty(Path(path).name),
                    # chemin réel sur le Pi : un renfort qui voit le même NAS peut lire le fichier directement
                    "host_path": db_path(db.container_to_host(path)),
                    "size": r["size"] if r else None, "mtime": r["mtime"] if r else None})
    return out


@app.get("/api/work/file/{pid}")
def work_file(pid: int, request: Request):
    path = scanner.lease_path(pid, _worker_name(request))
    if not path:
        raise HTTPException(410, "Cette photo n'est plus réservée pour ce renfort.")
    p = Path(path)
    try:
        st = p.stat()
    except OSError as e:
        raise HTTPException(404, f"Fichier illisible : {e.strerror}")
    return FileResponse(p, media_type="application/octet-stream", headers={"X-Mtime": str(st.st_mtime)})


def _b64(v, limit):
    data = base64.b64decode(v or "", validate=True)
    if len(data) > limit:
        raise ValueError("donnée trop volumineuse")
    return data


@app.post("/api/work/result/{pid}")
def work_result(pid: int, request: Request, payload: dict = Body(...)):
    worker = _worker_name(request)
    if payload.get("error"):
        ok = scanner.submit(worker, pid, {"error": str(payload["error"])[:300]})
        return {"ok": ok}
    try:
        m = payload["meta"]
        meta = {"taken": datetime.strptime(m["taken"], "%Y-%m-%d %H:%M:%S"),
                "width": int(m["width"]), "height": int(m["height"]),
                "make": (m.get("make") or None) and str(m["make"])[:80],
                "model": (m.get("model") or None) and str(m["model"])[:80],
                "lat": float(m["lat"]) if m.get("lat") is not None else None,
                "lon": float(m["lon"]) if m.get("lon") is not None else None}
        found = []
        for f in payload.get("faces", [])[:200]:
            x, y, w, h = (float(v) for v in f["box"])
            emb = np.frombuffer(_b64(f["emb"], 4096), dtype=np.float32).copy()
            if emb.shape != (128,):
                raise ValueError("empreinte de visage invalide")
            found.append((x, y, w, h, float(f["score"]), emb, _b64(f["crop"], 500_000)))
        res = {"meta": meta, "tw": int(payload["tw"]), "th": int(payload["th"]), "faces": found,
               "thumb": _b64(payload["thumb"], 3_000_000), "fp": str(payload.get("fp") or "")[:64] or None}
    except (KeyError, ValueError, TypeError, binascii.Error) as e:
        raise HTTPException(400, f"Résultat invalide : {e}")
    return {"ok": scanner.submit(worker, pid, res)}


@app.post("/api/scan")
def start_scan():
    scanner.request()
    return {"ok": True, "running": scanner.running}


# --------------------------------------------------------------------------- interface

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
