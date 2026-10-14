"""Noms de fichiers non UTF-8 (anciens fichiers Windows/NAS, ex. « Noël » en Latin-1).

Python les lit avec des « surrogates » (\\udc80-\\udcff) que SQLite et JSON refusent.
- db_path / fs_path : conversion réversible pour stocker ces chemins en base (zone Unicode privée).
- pretty : version lisible pour l'affichage (octets invalides décodés en Windows-1252).
"""

_TO_DB = {0xDC80 + i: 0xF780 + i for i in range(128)}
_TO_FS = {v: k for k, v in _TO_DB.items()}


def _has_odd(s: str) -> bool:
    return any(0xDC80 <= ord(ch) <= 0xDCFF or 0xF780 <= ord(ch) <= 0xF7FF for ch in s)


def db_path(s: str) -> str:
    return s.translate(_TO_DB)


def fs_path(s: str) -> str:
    return s.translate(_TO_FS)


def pretty(s: str) -> str:
    if not _has_odd(s):
        return s
    out = []
    for seg in fs_path(s).split("/"):
        b = seg.encode("utf-8", "surrogateescape")
        try:
            out.append(b.decode("utf-8"))
        except UnicodeDecodeError:
            out.append(b.decode("cp1252", errors="replace"))
    return "/".join(out)
