"""Configuration, lue depuis les variables d'environnement."""
import os
from pathlib import Path


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", "/photos")).resolve()
# Chemin réel (sur le Pi) du dossier monté sur /photos, pour l'affichage.
PHOTOS_HOST_PATH = (os.environ.get("PHOTOS_HOST_PATH") or "").rstrip("/") or None
# Disques du Pi montés en lecture seule pour pouvoir ajouter des dossiers depuis l'interface :
# /mnt du Pi est visible dans /host/mnt, /media dans /host/media.
HOST_DIR = Path(os.environ.get("HOST_DIR", "/host"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")).resolve()
MODELS_DIR = Path(os.environ.get("MODELS_DIR", "/app/models"))

DB_PATH = DATA_DIR / "photos.db"
CACHE_DIR = DATA_DIR / "cache"
THUMBS_DIR = CACHE_DIR / "thumbs"
PREVIEWS_DIR = CACHE_DIR / "previews"
FACES_DIR = CACHE_DIR / "faces"

# Analyse
WORKERS = max(0, _int("WORKERS", 2))   # 0 = le Pi laisse tout le travail aux renforts
SCAN_INTERVAL_HOURS = _float("SCAN_INTERVAL_HOURS", 6)
THUMB_HEIGHT = _int("THUMB_HEIGHT", 360)
PREVIEW_SIZE = _int("PREVIEW_SIZE", 2048)

# Visages
ENABLE_FACES = os.environ.get("ENABLE_FACES", "1") not in ("0", "false", "no")
# 0 = le Pi ne cherche pas les visages lui-même : il fait les miniatures, les renforts font les visages
PI_FACES = os.environ.get("PI_FACES", "1") not in ("0", "false", "no")
FACE_DETECT_SIZE = _int("FACE_DETECT_SIZE", 1280)   # côté max de l'image analysée
FACE_MIN_SCORE = _float("FACE_MIN_SCORE", 0.85)     # confiance minimale de détection
FACE_MIN_PX = _int("FACE_MIN_PX", 32)               # taille minimale d'un visage (px, à FACE_DETECT_SIZE)
FACE_MATCH = _float("FACE_MATCH_THRESHOLD", 0.40)   # similarité cosinus pour « même personne »
MIN_CLUSTER = max(1, _int("MIN_CLUSTER_SIZE", 2))   # taille mini d'un groupe proposé à nommer

# Accès
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif",
              # RAW (lus via leur aperçu JPEG intégré)
              ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf", ".rw2", ".raf",
              ".pef", ".srw", ".x3f", ".3fr", ".iiq", ".rwl", ".erf", ".kdc", ".mrw"}
# Un RAW accompagné d'un JPEG du même nom (prise RAW+JPEG) n'est pas affiché en double
SKIP_RAW_WITH_JPEG = os.environ.get("SKIP_RAW_WITH_JPEG", "1") not in ("0", "false", "no")
SKIP_DIRS = {"@eadir", "#recycle", ".thumbnails", ".trash", "$recycle.bin", "lost+found"}

for d in (DATA_DIR, THUMBS_DIR, PREVIEWS_DIR, FACES_DIR):
    d.mkdir(parents=True, exist_ok=True)
