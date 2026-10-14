"""Lecture des images : EXIF, orientation, miniatures, aperçus."""
import io
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from . import config

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover - HEIC optionnel
    pillow_heif = None

Image.MAX_IMAGE_PIXELS = 300_000_000

_FILENAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)"
                            r"(?:[-_ T.]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d))?")

EXIF_IFD = 0x8769
GPS_IFD = 0x8825


def cache_path(base: Path, ident: int, ext: str) -> Path:
    sub = base / f"{ident % 256:02x}"
    sub.mkdir(exist_ok=True)
    return sub / f"{ident}.{ext}"


def thumb_path(pid):
    return cache_path(config.THUMBS_DIR, pid, "webp")


def preview_path(pid):
    return cache_path(config.PREVIEWS_DIR, pid, "webp")


def face_path(fid):
    return cache_path(config.FACES_DIR, fid, "webp")


def _parse_exif_date(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(s[:19], fmt)
            if 1900 < d.year < 2100:
                return d
        except ValueError:
            continue
    return None


def _date_from_filename(name):
    m = _FILENAME_DATE.search(name)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 12), int(mm or 0), int(ss or 0))
    except ValueError:
        return None


def _gps_to_deg(values, ref):
    try:
        d, m, s = (float(v) for v in values)
        deg = d + m / 60 + s / 3600
        if ref in ("S", "W"):
            deg = -deg
        return round(deg, 6)
    except Exception:
        return None


RAW_EXTS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".orf", ".rw2", ".raf",
            ".pef", ".srw", ".x3f", ".3fr", ".iiq", ".rwl", ".erf", ".kdc", ".mrw"}

_TRANSPOSE = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
              5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
              8: Image.Transpose.ROTATE_90}


def is_raw(name: str) -> bool:
    return os.path.splitext(name or "")[1].lower() in RAW_EXTS


def _tiff_jpegs(data: bytes):
    """Aperçus JPEG déclarés dans la structure TIFF d'un RAW (CR2, NEF, ARW, DNG, ORF, PEF…)."""
    import struct
    if data[:4] == b"II*\0":
        e = "<"
    elif data[:4] == b"MM\0*":
        e = ">"
    else:
        return []
    found, seen = [], set()
    todo = [struct.unpack(e + "I", data[4:8])[0]]
    while todo and len(seen) < 32:
        off = todo.pop()
        if off in seen or off <= 0 or off + 2 > len(data):
            continue
        seen.add(off)
        n = struct.unpack(e + "H", data[off:off + 2])[0]
        if n > 1000:
            continue
        tags = {}
        for i in range(n):
            ent = off + 2 + 12 * i
            if ent + 12 > len(data):
                break
            tag, typ, cnt = struct.unpack(e + "HHI", data[ent:ent + 8])
            if typ in (3,) and cnt == 1:
                val = struct.unpack(e + "H", data[ent + 8:ent + 10])[0]
            elif typ in (4, 13) and cnt == 1:
                val = struct.unpack(e + "I", data[ent + 8:ent + 12])[0]
            elif typ in (4, 13) and tag == 0x14A:   # SubIFDs : liste de pointeurs
                ptr = struct.unpack(e + "I", data[ent + 8:ent + 12])[0]
                val = list(struct.unpack(e + "I" * cnt, data[ptr:ptr + 4 * cnt])) if ptr + 4 * cnt <= len(data) else []
            else:
                continue
            tags[tag] = val
        for start, length in ((0x111, 0x117), (0x201, 0x202)):
            o, l = tags.get(start), tags.get(length)
            if isinstance(o, int) and isinstance(l, int) and l > 1000 and data[o:o + 2] == b"\xff\xd8":
                found.append((o, l))
        sub = tags.get(0x14A)
        if isinstance(sub, int):
            todo.append(sub)
        elif isinstance(sub, list):
            todo += sub
        nxt = off + 2 + 12 * n
        if nxt + 4 <= len(data):
            todo.append(struct.unpack(e + "I", data[nxt:nxt + 4])[0])
    return found


def _scan_jpegs(data: bytes, limit=40):
    """Dernier recours (CR3, RAF…) : JPEG intégrés repérés par leurs marqueurs."""
    out, pos = [], 0
    while len(out) < limit:
        start = data.find(b"\xff\xd8\xff", pos)
        if start < 0:
            break
        end = data.find(b"\xff\xd9", start + 4)
        while end > 0:
            try:
                with Image.open(io.BytesIO(data[start:end + 2])) as t:
                    w, h = t.size
                out.append((start, end + 2 - start, w * h))
                break
            except Exception:
                end = data.find(b"\xff\xd9", end + 2)
                if end - start > 60_000_000:
                    end = -1
        pos = start + 3
    return out


def raw_preview(data: bytes):
    """Plus grand aperçu JPEG intégré à un fichier RAW, et les EXIF du RAW lui-même."""
    exif = None
    if data[:4] in (b"II*\0", b"MM\0*"):
        try:
            exif = Image.Exif()
            exif.load(data)
        except Exception:
            exif = None
    best = None
    for o, l in _tiff_jpegs(data):
        if best is None or l > best[1]:
            best = (o, l)
    if best is None or best[1] < 200_000:  # rien d'utilisable dans la structure : on cherche partout
        scanned = sorted(_scan_jpegs(data), key=lambda t: -t[2])
        if scanned and (best is None or scanned[0][1] > best[1]):
            best = scanned[0][:2]
    if best is None:
        raise ValueError("aucun aperçu JPEG dans ce fichier RAW")
    return data[best[0]:best[0] + best[1]], exif


def read_metadata(im: Image.Image, name: str, mtime: float, exif=None):
    """Date de prise de vue, appareil, GPS et taille orientée."""
    meta = {"make": None, "model": None, "lat": None, "lon": None}
    taken = None
    orientation = 1
    if exif is None:
        try:
            exif = im.getexif()
        except Exception:
            exif = None
    if exif:
        orientation = exif.get(0x0112, 1) or 1
        meta["make"] = (str(exif.get(0x010F) or "").strip("\x00 ") or None)
        meta["model"] = (str(exif.get(0x0110) or "").strip("\x00 ") or None)
        try:
            sub = exif.get_ifd(EXIF_IFD)
            taken = _parse_exif_date(sub.get(0x9003)) or _parse_exif_date(sub.get(0x9004))
        except Exception:
            pass
        taken = taken or _parse_exif_date(exif.get(0x0132))
        try:
            gps = exif.get_ifd(GPS_IFD)
            if gps and 2 in gps and 4 in gps:
                meta["lat"] = _gps_to_deg(gps[2], gps.get(1))
                meta["lon"] = _gps_to_deg(gps[4], gps.get(3))
        except Exception:
            pass
    if meta["make"] and meta["model"] and meta["model"].lower().startswith(meta["make"].lower().split()[0]):
        meta["make"] = None  # « Apple » + « Apple iPhone 12 » -> seulement le modèle
    taken = taken or _date_from_filename(name) or datetime.fromtimestamp(mtime)
    w, h = im.size
    if orientation in (5, 6, 7, 8):
        w, h = h, w
    meta.update(taken=taken, width=w, height=h, orientation=orientation)
    return meta


def open_reduced(src, target: int, name: str = None, mtime: float = None):
    """Ouvre une image (chemin ou fichier en mémoire) et la décode au plus près de `target` px
    (côté long), orientée, en RGB."""
    if isinstance(src, Path):
        name, mtime = src.name, src.stat().st_mtime
    raw_orient = 1
    if is_raw(name):  # RAW : on lit l'aperçu JPEG intégré (rapide, sans décoder le RAW)
        data = src.read_bytes() if isinstance(src, Path) else (src.getvalue() if hasattr(src, "getvalue") else src.read())
        jpeg, cexif = raw_preview(data)
        im = Image.open(io.BytesIO(jpeg))
        meta = read_metadata(im, name or "", mtime or 0, exif=cexif if cexif else None)
        raw_orient = meta.get("orientation", 1)
        try:
            if im.getexif().get(0x0112, 1) not in (None, 1):
                raw_orient = 1   # l'aperçu porte déjà sa propre orientation
        except Exception:
            pass
        if raw_orient in (5, 6, 7, 8):  # dimensions d'après l'aperçu, une fois tourné
            meta["width"], meta["height"] = im.size[1], im.size[0]
        else:
            meta["width"], meta["height"] = im.size
    else:
        im = Image.open(src)
        meta = read_metadata(im, name or "", mtime or 0)
    if getattr(im, "is_animated", False):
        im.seek(0)
    try:
        im.draft("RGB", (target, target))  # JPEG : décodage réduit, très rapide
    except Exception:
        pass
    im = ImageOps.exif_transpose(im)
    if raw_orient in _TRANSPOSE:
        im = im.transpose(_TRANSPOSE[raw_orient])
    if im.mode not in ("RGB",):
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
    if max(im.size) > target:
        im.thumbnail((target, target), Image.LANCZOS)
    return im, meta


def thumb_image(im: Image.Image):
    w, h = im.size
    th = min(config.THUMB_HEIGHT, h)
    tw = max(1, round(w * th / h))
    if tw > config.THUMB_HEIGHT * 4:  # panoramas
        tw = config.THUMB_HEIGHT * 4
        th = max(1, round(h * tw / w))
    return im.resize((tw, th), Image.LANCZOS) if (tw, th) != (w, h) else im


def make_thumb(im: Image.Image, dest: Path):
    t = thumb_image(im)
    tmp = dest.with_suffix(".tmp")
    t.save(tmp, "WEBP", quality=78, method=4)
    os.replace(tmp, dest)
    return t.size


def fingerprint_bytes(data: bytes) -> str:
    """Même empreinte que scanner.fingerprint, calculée sur un fichier en mémoire."""
    import hashlib
    size = len(data)
    h = hashlib.sha1(str(size).encode())
    h.update(data[:65536])
    if size > 131072:
        h.update(data[-65536:])
    return h.hexdigest()


def write_atomic(dest: Path, data: bytes):
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


_preview_locks: dict = {}
_preview_guard = threading.Lock()


def ensure_preview(pid: int, src: Path) -> Path:
    """Aperçu grand format, généré à la demande puis mis en cache."""
    dest = preview_path(pid)
    if dest.exists():
        return dest
    with _preview_guard:
        lock = _preview_locks.setdefault(pid, threading.Lock())
    with lock:
        if not dest.exists():
            im, _ = open_reduced(src, config.PREVIEW_SIZE)
            tmp = dest.with_suffix(".tmp")
            im.save(tmp, "WEBP", quality=85, method=4)
            os.replace(tmp, dest)
    with _preview_guard:
        _preview_locks.pop(pid, None)
    return dest


def remove_cache(pid: int, face_ids=()):
    for p in (thumb_path(pid), preview_path(pid), *(face_path(f) for f in face_ids)):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
