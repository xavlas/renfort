#!/bin/bash
# Lance le renfort d'analyse de la Photothèque sur ce Mac.
# Usage : bash ~/Public/phototheque-renfort/lancer-renfort.sh [--fils 8] [--serveur http://10.0.0.228:2342]
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/.phototheque-renfort"
VENV="$STATE/venv"
mkdir -p "$STATE"

PY=""
for c in python3.13 python3.12 python3.11 python3.10 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Il faut Python 3.10 ou plus récent sur ce Mac."
  echo "Installez-le avec Homebrew (brew install python) ou depuis https://www.python.org/downloads/ puis relancez."
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "Première utilisation : installation des outils d'analyse (une ou deux minutes)…"
  "$PY" -m venv "$VENV"
fi
if [ ! -f "$STATE/.installe" ] || [ "$HERE/requirements.txt" -nt "$STATE/.installe" ]; then
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q -r "$HERE/requirements.txt"
  touch "$STATE/.installe"
fi

# caffeinate (Mac) : empêche la mise en veille tant que le renfort tourne
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "$VENV/bin/python" "$HERE/renfort.py" "$@"
fi
exec "$VENV/bin/python" "$HERE/renfort.py" "$@"
