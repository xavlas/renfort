#!/bin/bash
# Déploie (ou met à jour) la Photothèque sur le Raspberry Pi, depuis un autre ordinateur (Mac ou Linux).
# Usage : bash deployer-phototheque.sh [dossier_des_photos_sur_le_pi]
# Variable facultative : PI=utilisateur@adresse (par défaut xavier@10.0.0.228)
# Fonctionne depuis une copie du dépôt Git, ou avec une archive phototheque.tar.gz posée à côté du script.
set -e

PI="${PI:-xavier@10.0.0.228}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$HERE/phototheque.tar.gz"
TEMP_ARCHIVE=""

if [ -f "$HERE/Dockerfile" ] && [ -d "$HERE/app" ]; then
  # copie du dépôt Git : on emballe le code tel qu'il est
  TEMP_ARCHIVE="${TMPDIR:-/tmp}/phototheque-$$.tar.gz"
  COPYFILE_DISABLE=1 tar --exclude data --exclude .git --exclude __pycache__ --exclude .env \
    --exclude phototheque.tar.gz -czf "$TEMP_ARCHIVE" -C "$HERE" .
  ARCHIVE="$TEMP_ARCHIVE"
fi
[ -f "$ARCHIVE" ] || { echo "Archive introuvable : $ARCHIVE"; exit 1; }

# une seule connexion SSH réutilisée : le mot de passe n'est demandé qu'une fois
CTL="/tmp/phototheque-ssh-$$"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$CTL" -o ControlPersist=120 -o ConnectTimeout=10)
cleanup() {
  ssh "${SSH_OPTS[@]}" -O exit "$PI" >/dev/null 2>&1 || true
  [ -n "$TEMP_ARCHIVE" ] && rm -f "$TEMP_ARCHIVE"
}
trap cleanup EXIT

echo "Connexion à $PI…"
ssh "${SSH_OPTS[@]}" "$PI" true || { echo "Connexion SSH impossible à $PI."; exit 1; }

echo "Envoi de l'application…"
scp -q "${SSH_OPTS[@]}" "$ARCHIVE" "$PI:phototheque.tar.gz"

ARG=""
[ -n "$1" ] && ARG=$(printf '%q' "$1")
ssh -t "${SSH_OPTS[@]}" "$PI" \
  "mkdir -p ~/phototheque && tar -xzf ~/phototheque.tar.gz -C ~/phototheque ./install.sh && bash ~/phototheque/install.sh $ARG"
