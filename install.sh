#!/bin/bash
# Installe ou met à jour la Photothèque sur le Pi.
# Usage : bash ~/phototheque/install.sh [dossier_des_photos]
# Appelé automatiquement par deployer-phototheque.sh depuis le Mac.
set -e

APP_DIR="$HOME/phototheque"
ARCHIVE="$HOME/phototheque.tar.gz"
PHOTOS="$1"
PORT=2342

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mErreur : %s\033[0m\n' "$*" >&2; exit 1; }

# --- Docker ------------------------------------------------------------------
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1 || sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
  else
    fail "Docker ne répond pas. Vérifiez qu'il est installé et démarré (sudo systemctl status docker)."
  fi
fi
$DOCKER compose version >/dev/null 2>&1 || fail "Le plugin « docker compose » est introuvable."
[ "$(uname -m)" = "aarch64" ] || [ "$(uname -m)" = "x86_64" ] || \
  fail "Système $(uname -m) non pris en charge : il faut un système 64 bits (aarch64)."

# --- Extraction (le dossier de photos déjà choisi est repris) -----------------
say "Extraction de l'application dans $APP_DIR"
mkdir -p "$APP_DIR"
KEEP=0
if [ -f "$APP_DIR/docker-compose.yml" ]; then
  cp "$APP_DIR/docker-compose.yml" "$APP_DIR/docker-compose.yml.precedent"
  KEEP=1
fi
tar -xzf "$ARCHIVE" -C "$APP_DIR"
cd "$APP_DIR"

# --- Dossier des photos ------------------------------------------------------
current=""
if [ "$KEEP" = 1 ]; then
  current=$(sed -n 's/^[[:space:]]*-[[:space:]]*"\{0,1\}\(.*\):\/photos:ro"\{0,1\}[[:space:]]*\(#.*\)\{0,1\}$/\1/p' docker-compose.yml.precedent | head -1)
fi

if [ -z "$PHOTOS" ] && [ -n "$current" ] && [ -d "$current" ] && [ "$current" != "/mnt/photos" -o "$KEEP" = 1 ]; then
  PHOTOS="$current"
  echo "Dossier des photos déjà configuré : $PHOTOS"
fi

if [ -z "$PHOTOS" ]; then
  say "Recherche des dossiers qui contiennent des photos (cela peut prendre une minute)…"
  candidates=$(timeout 180 find /mnt /media "$HOME" \
      \( -path "$APP_DIR" -o -path "$HOME/automation" -o -path '*/.*' -o -path '/mnt/downloads' \
         -o -path '*/node_modules' -o -path '*/@eaDir' \) -prune -o \
      -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.heic' -o -iname '*.png' \) -print 2>/dev/null \
    | awk -v roots="/mnt /media $HOME" 'BEGIN { n = split(roots, R, " ") }
        { for (i = 1; i <= n; i++) if (index($0, R[i] "/") == 1) {
            k = split(substr($0, length(R[i]) + 2), S, "/")
            if (k >= 2) print R[i] "/" S[1]
            if (k >= 3) print R[i] "/" S[1] "/" S[2]
            break } }' \
    | sort | uniq -c | sort -rn | awk '$1 >= 20' | head -8)
  if [ -n "$candidates" ]; then
    echo "Dossiers trouvés (nombre d'images, chemin) :"
    echo "$candidates" | awk '{n=$1; $1=""; printf "  %2d) %7d  %s\n", NR, n, substr($0,2)}'
    echo
    printf "Numéro du dossier à utiliser, ou chemin complet d'un autre dossier : "
    read -r answer
    if printf '%s' "$answer" | grep -qE '^[0-9]+$'; then
      PHOTOS=$(echo "$candidates" | sed -n "${answer}p" | awk '{$1=""; print substr($0,2)}')
    else
      PHOTOS="$answer"
    fi
  else
    echo "Aucun dossier avec au moins 20 images trouvé dans /mnt, /media ou $HOME."
    printf "Chemin complet du dossier de photos : "
    read -r PHOTOS
  fi
fi

PHOTOS="${PHOTOS%/}"
[ -n "$PHOTOS" ] || fail "Aucun dossier indiqué."
[ -d "$PHOTOS" ] || fail "Le dossier « $PHOTOS » n'existe pas sur le Pi."
[ -r "$PHOTOS" ] || fail "Le dossier « $PHOTOS » n'est pas lisible."

# remplace la ligne de montage des photos
esc=$(printf '%s' "$PHOTOS" | sed 's/[\\&|]/\\&/g; s/"/\\"/g')
sed -i "s|^\([[:space:]]*-[[:space:]]*\).*:/photos:ro.*$|\1\"$esc:/photos:ro\"|" docker-compose.yml
sed -i "s|PHOTOS_HOST_PATH=.*$|PHOTOS_HOST_PATH=$esc|" docker-compose.yml
grep -q "\"$PHOTOS:/photos:ro\"" docker-compose.yml || fail "Impossible de régler le dossier dans docker-compose.yml."
echo "Photos : $PHOTOS (lecture seule)"

# --- Réglages : repris de la version précédente, ou passés en variables -------
#     (ex. depuis le Mac : PI_FACES=0 bash deployer-phototheque.sh)
for var in WORKERS PI_FACES SCAN_INTERVAL_HOURS ENABLE_FACES FACE_MATCH_THRESHOLD; do
  val="${!var}"
  if [ -z "$val" ] && [ "$KEEP" = 1 ]; then
    val=$(sed -n "s/^[[:space:]]*-[[:space:]]*$var=\([^[:space:]#]*\).*/\1/p" docker-compose.yml.precedent | head -1)
  fi
  if [ -n "$val" ]; then
    printf '%s' "$val" | grep -qE '^[0-9.]+$' || fail "Valeur invalide pour $var : $val"
    sed -i "s/^\([[:space:]]*-[[:space:]]*$var=\).*/\1$val/" docker-compose.yml
  fi
done
if [ "$KEEP" = 1 ]; then
  oldport=$(sed -n 's/^[[:space:]]*-[[:space:]]*"\{0,1\}\([0-9]*\):2342"\{0,1\}.*/\1/p' docker-compose.yml.precedent | head -1)
  if [ -n "$oldport" ] && [ "$oldport" != "2342" ]; then
    sed -i "s/^\([[:space:]]*-[[:space:]]*\)\"2342:2342\"/\1\"$oldport:2342\"/" docker-compose.yml
    PORT="$oldport"
  fi
fi
echo "Réglages : $(grep -oE '(WORKERS|PI_FACES|SCAN_INTERVAL_HOURS|ENABLE_FACES|FACE_MATCH_THRESHOLD)=[0-9.]+' docker-compose.yml | tr '\n' ' ')port $PORT"

# --- Mot de passe (première installation seulement) --------------------------
if [ ! -f .env ]; then
  say "Mot de passe d'accès à la photothèque"
  echo "Laissez vide pour ne pas en demander (accès libre depuis votre réseau local)."
  printf "Mot de passe : "
  read -rs pw; echo
  if [ -n "$pw" ]; then
    printf "APP_PASSWORD='%s'\n" "$(printf '%s' "$pw" | sed "s/'/'\\\\''/g")" > .env
    chmod 600 .env
    echo "Mot de passe enregistré."
  else
    : > .env
  fi
fi

# --- Port --------------------------------------------------------------------
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT\$"; then
  if ! $DOCKER ps --format '{{.Names}}' | grep -qx phototheque; then
    fail "Le port $PORT est déjà utilisé par un autre programme. Modifiez \"$PORT:2342\" dans $APP_DIR/docker-compose.yml."
  fi
fi

# --- Construction et démarrage -----------------------------------------------
say "Construction de l'image (la première fois : quelques minutes)…"
$DOCKER compose build
say "Démarrage…"
if ! out=$($DOCKER compose up -d 2>&1); then
  echo "$out"
  if echo "$out" | grep -qi "shared or slave mount"; then
    # le système ne permet pas de voir les disques branchés après coup : on s'en passe
    echo "Montage des disques sans propagation (un disque branché plus tard demandera un redémarrage du conteneur)."
    sed -i '/propagation: rslave/d' docker-compose.yml
    $DOCKER compose up -d
  else
    fail "Le conteneur n'a pas pu démarrer."
  fi
fi

printf "Attente de la réponse du serveur"
ok=0
for _ in $(seq 1 60); do
  if curl -fs "http://127.0.0.1:$PORT/api/auth" >/dev/null 2>&1; then ok=1; break; fi
  printf "."; sleep 2
done
echo
if [ "$ok" != 1 ]; then
  $DOCKER logs --tail 40 phototheque
  fail "Le serveur ne répond pas. Les dernières lignes du journal sont affichées ci-dessus."
fi

n=$($DOCKER exec phototheque sh -c 'find /photos -type f | head -1 | wc -l')
[ "$n" = "1" ] || echo "Attention : le conteneur ne voit aucun fichier dans /photos."
faces=$($DOCKER logs phototheque 2>&1 | grep -c "Reconnaissance faciale désactivée" || true)
[ "$faces" = "0" ] || echo "Attention : la reconnaissance des visages n'a pas pu démarrer (voir docker logs phototheque)."

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Photothèque en ligne : http://${ip:-IP-du-pi}:$PORT"
echo "L'analyse des photos tourne en fond. Suivi : docker logs -f phototheque"
rm -f "$ARCHIVE"
