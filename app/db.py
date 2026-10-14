"""Accès SQLite : une connexion par thread, mode WAL."""
import sqlite3
import threading
import time
from pathlib import Path

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    folder TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    fp TEXT,
    taken_ts INTEGER NOT NULL,
    taken TEXT NOT NULL,
    ym TEXT NOT NULL,
    width INTEGER, height INTEGER,
    tw INTEGER, th INTEGER,
    make TEXT, model TEXT,
    lat REAL, lon REAL,
    faces_done INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    added_ts INTEGER NOT NULL,
    source_id INTEGER
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    root TEXT NOT NULL UNIQUE,      -- chemin dans le conteneur
    label TEXT NOT NULL,            -- chemin affiché (chemin réel sur le Pi)
    state TEXT NOT NULL DEFAULT 'active',   -- active | removing
    added_ts INTEGER NOT NULL,
    last_scan INTEGER
);
CREATE INDEX IF NOT EXISTS idx_photos_time ON photos(taken_ts DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_photos_folder ON photos(folder);
CREATE INDEX IF NOT EXISTS idx_photos_ym ON photos(ym);
CREATE INDEX IF NOT EXISTS idx_photos_fp ON photos(fp);

CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE COLLATE NOCASE,
    hidden INTEGER NOT NULL DEFAULT 0,
    emb_sum BLOB,
    face_count INTEGER NOT NULL DEFAULT 0,
    photo_count INTEGER NOT NULL DEFAULT 0,
    cover_face_id INTEGER,
    created_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
    id INTEGER PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,
    score REAL NOT NULL,
    emb BLOB,
    person_id INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    manual INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(photos)")]
    if "source_id" not in cols:
        # base créée par une version précédente : chemins relatifs à /photos, un seul dossier
        c.execute("ALTER TABLE photos ADD COLUMN source_id INTEGER")
    if meta_get("default_source") is None:
        has_rows = c.execute("SELECT 1 FROM photos WHERE source_id IS NULL LIMIT 1").fetchone()
        if config.PHOTOS_DIR.is_dir() or has_rows:
            label = config.PHOTOS_HOST_PATH or str(config.PHOTOS_DIR)
            c.execute("INSERT OR IGNORE INTO sources(root, label, added_ts) VALUES(?,?,?)",
                      (str(config.PHOTOS_DIR), label, int(time.time())))
            sid =c.execute("SELECT id FROM sources WHERE root=?", (str(config.PHOTOS_DIR),)).fetchone()["id"]
            c.execute("UPDATE photos SET source_id=?, path=? || '/' || path,"
                      " folder = ? || CASE folder WHEN '' THEN '' ELSE '/' || folder END WHERE source_id IS NULL",
                      (sid, str(config.PHOTOS_DIR), label))
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('default_source', '1')")
    # le chemin réel du dossier /photos a changé (nouveau docker-compose) : mettre à jour l'affichage
    if config.PHOTOS_HOST_PATH:
        r = c.execute("SELECT id, label FROM sources WHERE root=?", (str(config.PHOTOS_DIR),)).fetchone()
        if r and r["label"] != config.PHOTOS_HOST_PATH:
            old, new = r["label"], config.PHOTOS_HOST_PATH
            c.execute("UPDATE photos SET folder = ? || substr(folder, ?) WHERE source_id=?", (new, len(old) + 1, r["id"]))
            c.execute("UPDATE sources SET label=? WHERE id=?", (new, r["id"]))
    c.execute("CREATE INDEX IF NOT EXISTS idx_photos_source ON photos(source_id)")
    c.execute("UPDATE sources SET state='active' WHERE state='removing'")  # retrait interrompu : à reprendre
    c.commit()


def sources(active_only=True):
    q = "SELECT * FROM sources" + (" WHERE state='active'" if active_only else "") + " ORDER BY label"
    return conn().execute(q).fetchall()


def host_to_container(host_path: str):
    """/mnt/usb/Photos (chemin du Pi) -> /host/mnt/usb/Photos (chemin dans le conteneur)."""
    return config.HOST_DIR / host_path.lstrip("/")


def container_to_host(p) -> str:
    p = Path(p)
    try:
        return "/" + p.relative_to(config.HOST_DIR).as_posix()
    except ValueError:
        pass
    try:
        rel = p.relative_to(config.PHOTOS_DIR).as_posix()
        base = config.PHOTOS_HOST_PATH or str(config.PHOTOS_DIR)
        return base if rel == "." else f"{base}/{rel}"
    except ValueError:
        return str(p)


def meta_get(key, default=None):
    r = conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(key, value):
    c = conn()
    c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, str(value)))
    c.commit()
