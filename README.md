# Photothèque

Une photothèque web pour Raspberry Pi, dans l'esprit d'iPhoto :

- **galerie chronologique** groupée par mois, avec une frise des années sur le côté pour sauter dans le temps ;
- **visionneuse plein écran** (flèches du clavier, balayage sur mobile, téléchargement de l'original) ;
- **reconnaissance des visages** : les visages qui se ressemblent sont regroupés automatiquement, vous leur donnez un nom ;
- **filtres** par personne (plusieurs à la fois = photos où elles sont *ensemble*), période, année, dossier, appareil photo, et recherche par nom de fichier ;
- **plusieurs dossiers** (NAS, disque USB, clé…) à ajouter ou retirer depuis l'interface ;
- vos photos restent **où elles sont, en lecture seule** : rien n'est déplacé ni modifié.

Formats lus : JPEG, HEIC/HEIF (iPhone), PNG, WebP, TIFF, BMP, GIF.

---

## Déploiement en une commande (depuis un Mac ou un PC Linux)

```bash
git clone https://github.com/xavlas/renfort.git
cd renfort
bash deployer-phototheque.sh
```

Le script envoie l'application sur le Pi (`xavier@10.0.0.228` ; une autre adresse avec `PI=utilisateur@adresse bash deployer-phototheque.sh`), repère les dossiers qui contiennent des photos et vous laisse choisir, demande un mot de passe facultatif, construit l'image, démarre le conteneur et vérifie qu'il répond. Pour mettre à jour : `git pull` puis la même commande. Votre configuration et vos noms de visages sont conservés.

Il fonctionne aussi sans Git, avec l'archive `phototheque.tar.gz` posée à côté du script.

Sous Windows, passez par WSL (Ubuntu), ou suivez l'installation manuelle ci-dessous.

## Installation manuelle sur le Pi

Prérequis : Raspberry Pi 4 ou 5 avec un système **64 bits**, Docker et Docker Compose (déjà en place si vous utilisez `~/automation`).

### 1. Copier le dossier sur le Pi

Depuis votre Mac :

```bash
scp phototheque.tar.gz xavier@10.0.0.228:~
```

Puis sur le Pi :

```bash
mkdir -p ~/phototheque && tar -xzf ~/phototheque.tar.gz -C ~/phototheque
cd ~/phototheque
```

### 2. Indiquer où sont vos photos

Ouvrez `docker-compose.yml` et remplacez `/mnt/photos` par le dossier qui contient vos photos sur le Pi :

```yaml
    volumes:
      - /mnt/photos:/photos:ro      # ← à gauche : votre dossier
      - ./data:/data
```

Plusieurs dossiers ? Montez-les dans des sous-dossiers :

```yaml
      - /mnt/disque1/Photos:/photos/Disque1:ro
      - /home/xavier/Images:/photos/Images:ro
```

### 3. (Conseillé) Protéger par un mot de passe

Créez un fichier `.env` à côté du `docker-compose.yml` :

```bash
echo "APP_PASSWORD='choisissez-un-mot-de-passe'" > .env
```

Sans ce fichier, la photothèque est accessible sans mot de passe à tout le réseau local.

### 4. Construire et lancer

```bash
docker compose up -d --build
docker logs -f phototheque
```

La première construction prend quelques minutes (téléchargement de Python, d'OpenCV et des modèles de visages). Quittez les logs avec Ctrl+C.

### 5. Ouvrir

**http://10.0.0.228:2342**

L'analyse démarre toute seule. La barre rouge sous l'en-tête montre sa progression, et les photos apparaissent au fur et à mesure.

---

## Utilisation

**Ajouter ou retirer des dossiers** : onglet *Dossiers*. Parcourez les disques du Pi (tout ce qui est dans `/mnt` et `/media`), ouvrez le dossier voulu et cliquez sur *Ajouter ce dossier*. Ses sous-dossiers sont inclus, et son analyse passe avant celle en cours. *Retirer* enlève les photos du dossier de la photothèque, mais ne touche jamais aux fichiers.
- Un dossier déjà inclus, ou qui contient un dossier déjà ajouté, est refusé, pour éviter les doublons.
- Si un disque ou le NAS est débranché, ses photos ne sont **pas** supprimées : le dossier est signalé « Introuvable » et retrouvé à son retour.
- Pour un disque situé ailleurs que dans `/mnt` ou `/media`, ajoutez un montage `source: … target: /host/…` dans `docker-compose.yml`.

**Nommer les visages** : onglet *Personnes > Qui est-ce ?*. Chaque carte est un groupe de visages qui se ressemblent. Tapez un nom et validez.
- Si le nom existe déjà, les deux groupes sont **fusionnés** : c'est la façon la plus simple de rassembler une même personne découpée en plusieurs groupes.
- *Ignorer* : pour les inconnus, les visages de passants, les affiches. Le groupe disparaît de la liste (récupérable via « Voir les groupes ignorés »).

**Corriger** :
- sur la page d'une personne, *Vérifier les visages* permet de retirer ceux qui ne sont pas elle ;
- dans la visionneuse, touche **I** (infos) : chaque visage de la photo peut être nommé, renommé ou retiré. Des noms sont proposés d'après les ressemblances ;
- touche **V** : affiche des cadres sur les visages.

**Filtrer** : bouton *Filtres*. Choisir deux personnes affiche les photos où elles sont **ensemble**. Cliquez sur un dossier ou un appareil dans les infos d'une photo pour filtrer dessus.

**Raccourcis de la visionneuse** : ← → pour naviguer, Échap pour fermer, I pour les infos, V pour les visages.

**Nouvelles photos** : une nouvelle analyse a lieu toutes les 6 heures (réglable). Pour la lancer tout de suite, cliquez sur le compteur de photos en haut à droite.
- Les photos **déplacées ou renommées** sont reconnues : elles gardent leurs noms de visages.
- Les photos **supprimées** disparaissent de la galerie.

---

## Accélérer l'analyse avec un renfort (Mac)

Un ordinateur plus puissant du réseau peut analyser les photos à la place du Pi. Il les lit via le Pi, calcule les miniatures et les visages, et renvoie les résultats. Le Pi reste seul à gérer la base et continue d'analyser de son côté.

```bash
bash renfort/lancer-renfort.sh            # depuis le dossier du dépôt
```

- Il fonctionne sur Mac et Linux. Il faut Python 3.10 ou plus récent. La première fois, le script installe ce qu'il faut dans `~/.phototheque-renfort`.
- Options : `--fils 6` pour le nombre de photos en parallèle, `--serveur http://10.0.0.228:2342` pour l'adresse du Pi.
- **Encore plus rapide** : si le NAS est aussi monté sur le Mac (Finder > Aller > Se connecter au serveur), le renfort le trouve tout seul dans `/Volumes` et y lit les photos directement, sans passer par le Pi. Sinon, indiquez le dossier avec `--nas /Volumes/NomDuPartage`.
- `WORKERS=0` dans `docker-compose.yml` laisse tout le travail aux renforts, et le Pi se contente de relayer et d'enregistrer.
- Ctrl+C l'arrête à tout moment. Les photos qu'il n'a pas finies sont reprises par le Pi au bout de 5 minutes.
- Le Mac ne se met pas en veille tant que le renfort tourne. Le compteur en haut de la photothèque affiche le renfort et son débit.

## Réglages (`docker-compose.yml`)

| Variable | Défaut | Rôle |
|---|---|---|
| `APP_PASSWORD` | vide | Mot de passe d'accès (via `.env`) |
| `WORKERS` | 2 | Photos analysées en parallèle. 2 sur un Pi 4, 3 sur un Pi 5 |
| `SCAN_INTERVAL_HOURS` | 6 | Fréquence des analyses automatiques (0 = au démarrage et à la demande uniquement) |
| `ENABLE_FACES` | 1 | 0 pour désactiver la reconnaissance des visages (analyse bien plus rapide) |
| `FACE_MATCH_THRESHOLD` | 0.40 | Exigence pour regrouper deux visages. Plus bas (0.35) : moins de groupes mais plus d'erreurs. Plus haut (0.45) : groupes plus sûrs mais plus nombreux |
| `MIN_CLUSTER_SIZE` | 2 | Nombre minimal de visages pour qu'un groupe apparaisse dans « Qui est-ce ? » |
| `FACE_MIN_PX` | 32 | Taille minimale d'un visage pour être pris en compte |

Après modification : `docker compose up -d`.

---

## Bon à savoir

- **Durée de la première analyse** : sur un Pi, comptez de l'ordre d'une seconde par photo avec la reconnaissance des visages (estimation, selon le modèle de Pi et la taille des photos). Une grosse collection peut donc prendre une nuit, voire plus. Les analyses suivantes ne traitent que les nouveautés.
- **Stockage** : le dossier `./data` contient la base, les miniatures et les vignettes de visages, soit environ 1 % de la taille de la photothèque. Les aperçus grand format sont créés à la première ouverture d'une photo, puis gardés (comptez environ 300 Ko par photo ouverte). Placez `./data` sur un disque local (SSD si possible), pas sur la carte SD ni sur un partage réseau.
- **Sauvegarde** : sauvegarder `./data` suffit à conserver tous les noms de personnes.
- **Tout réanalyser de zéro** : `docker compose down`, supprimez `./data`, puis `docker compose up -d`. Cela efface aussi les noms donnés aux visages.
- **Vidéos et fichiers RAW** : non pris en charge pour l'instant, ils sont ignorés.
- **Accès depuis l'extérieur** : l'application est conçue pour le réseau local. Pour y accéder hors de chez vous, passez par un VPN (WireGuard, Tailscale) plutôt que d'ouvrir le port sur votre box.

## Dépannage

| Problème | Solution |
|---|---|
| « Aucune photo pour l'instant » | Vérifiez le chemin des photos : `docker exec phototheque ls /photos` |
| Pas d'onglet Personnes utilisable | `docker logs phototheque \| grep -i faciale` : les modèles n'ont peut-être pas pu être chargés |
| Une même personne en plusieurs groupes | Donnez le même nom aux groupes : ils fusionnent |
| Deux personnes dans le même groupe | *Vérifier les visages* et retirez les intrus, ou montez `FACE_MATCH_THRESHOLD` à 0.45 |
| Port 2342 déjà utilisé | Changez la partie gauche de `"2342:2342"`, par exemple `"2400:2342"` |

## Technique

Python 3.13, FastAPI et SQLite côté serveur, Pillow et pillow-heif pour les images. OpenCV avec les modèles YuNet (détection) et SFace (reconnaissance) pour les visages. L'interface est en HTML, CSS et JavaScript, sans framework ni étape de build. Toutes les dépendances existent en version précompilée ARM64 : rien n'est compilé sur le Pi.
