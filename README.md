# LOOKi — Backend principal

Backend **Python / FastAPI** du prototype de lunettes connectées. Il reçoit les JPEG du Raspberry Pi, analyse les objets et leur proximité, puis transmet descriptions, réponses et alertes à l’iPhone via **un seul WebSocket bidirectionnel**. PostgreSQL conserve les sessions, images, détections et événements rejouables.

Ce dossier est la **racine du dépôt Git** (`LOOKi/backend/` dans le workspace). Toutes les commandes ci-dessous se lancent ici ; aucun fichier du dossier parent n’est nécessaire.

```mermaid
flowchart LR
    Pi["Raspberry Pi — JPEG horodatés"] -->|POST multipart| API["FastAPI — contrôleurs et services"]
    API --> DB[(PostgreSQL)]
    DB --> Worker["Worker visuel et worker profondeur indépendant"]
    Worker --> Vision["YOLO26n / RT-DETR et profondeur métrique<br/>ou provider mock"]
    Vision --> Worker
    Worker -->|Résultats et alertes| DB
    API <-->|Un WebSocket| Phone["iPhone — STT et TTS"]
    API --> Centres["Contexte centres — mock ou REST"]
```

## Démarrer avec Docker

Prérequis : Docker avec Compose. Si le terminal est dans `LOOKi/`, entrer d’abord dans `backend/`.

```bash
test -f .env || cp .env.example .env
docker compose up --build -d
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
```

Le fichier d’exemple sélectionne `mock`, sans poids de modèle. Un `.env` existant conserve ses réglages de vision réelle. Compose construit depuis `.` et monte `./calibration/` ; il applique les migrations Alembic avant de démarrer l’API et son worker. PostgreSQL reste sur le réseau Docker interne. Le nom de projet `looki` conserve les volumes existants après le déplacement du Compose.

- [Interface vidéo réelle](http://127.0.0.1:8000/live/) : masques, suivi des objets, mètres estimés et alertes de proximité possible ; [configuration et règles actuelles](docs/14-proximite-urbaine.md).
- [Swagger HTTP](http://127.0.0.1:8000/docs) et [OpenAPI](http://127.0.0.1:8000/openapi.json).
- Clé locale par défaut : `Authorization: Bearer looki-local-dev-key`.
- Backend lié à `127.0.0.1:8000` : l’accès depuis un Pi/iPhone physique demande une exposition adaptée ; HTTPS/WSS demande un reverse proxy TLS.

```bash
docker compose logs --tail=100 backend
docker compose stop
```

`stop` arrête les services et conserve les données. Voir le [guide d’exploitation](docs/05-installation-et-exploitation.md) pour les paramètres, les modèles, les migrations et le diagnostic.

## Démonstration

Avec `LOOKI_VISION_PROVIDER=mock` :

```bash
docker compose exec backend python scripts/demo.py --base-url http://127.0.0.1:8000
```

La démo crée une session, envoie une image proche simulée, vérifie l’analyse et l’alerte, pose une question via le même WebSocket, teste la reconnexion puis clôture la session. Les fixtures `near.jpg`, `far.jpg` et `clear.jpg` sont dans `fixtures/images/`.

## Organisation du dépôt

```text
.
├── compose.yaml          # Backend + PostgreSQL 16
├── Dockerfile            # Python 3.12, migrations et un processus Uvicorn
├── .env.example          # Configuration de développement partageable
├── app/
│   ├── main.py           # FastAPI et cycle de vie
│   ├── controllers/      # HTTP, WebSocket, adaptation du protocole
│   ├── schemas/          # Validation Pydantic des échanges
│   ├── services/         # Sessions, images, questions, analyse et proximité
│   ├── repositories/     # Accès aux données SQLAlchemy
│   ├── models/           # Tables SQLAlchemy
│   ├── core/             # Configuration, DB, authentification, runtime
│   ├── providers/        # Contexte centres : JSON local ou REST
│   └── vision/           # Mock, Depth Anything, UniDepthV2, YOLO26n, RT-DETR, calibration
├── alembic/              # Migrations PostgreSQL
├── calibration/          # Calibration réelle de caméra, montée en lecture seule
├── contracts/            # OpenAPI, JSON Schema et exemples
├── docs/                 # Architecture, données, flux, exploitation, évaluations
│   └── diagrams/         # 16 diagrammes Mermaid autonomes (.mmd)
├── frontend/             # React / Vite, construit et servi par FastAPI sous /live/
├── media/                # Vidéos locales ignorées par Git, montage en lecture seule
├── fixtures/             # Contextes et images de test sourcées
├── scripts/              # Démo, évaluations et export OpenAPI
├── tests/                # Tests des règles, providers et protocoles
├── pyproject.toml
└── uv.lock
```

Les contrôleurs appellent les services ; les repositories portent les requêtes SQL. Les providers isolent les modèles et le backend centres. L’inférence s’exécute hors de la boucle HTTP : en mode `yolo_seg_track`, un worker visuel et un worker de profondeur tournent dans le même processus. La base sert de file durable et de journal de reprise. **Un seul worker Uvicorn et une seule réplique** sont prévus dans ce POC.

## Documentation et schémas

Le [sommaire détaillé](docs/README.md) décrit les choix et leurs limites. Les [sources Mermaid `.mmd`](docs/diagrams/README.md) peuvent être ouvertes dans un éditeur compatible Mermaid.

| Sujet | Schéma autonome | Explication |
| --- | --- | --- |
| Architecture et frontières | [Architecture](docs/diagrams/01-architecture.mmd), [couches](docs/diagrams/02-couches.mmd) | [Chapitre 01](docs/01-architecture.md) |
| Classes | [Services et repositories](docs/diagrams/03-classes-services.mmd), [vision](docs/diagrams/05-classes-vision.mmd) | [Architecture](docs/01-architecture.md), [vision](docs/04-vision-et-proximite.md) |
| Tables PostgreSQL | [Relations et colonnes](docs/diagrams/04-base-de-donnees.mmd) | [Dictionnaire, contraintes et transactions](docs/02-base-de-donnees.md) |
| Images et alertes | [Séquence d’ingestion](docs/diagrams/06-ingestion-et-alerte.mmd), [états](docs/diagrams/09-cycle-image.mmd) | [Contrats et flux](docs/03-contrats-et-flux.md) |
| Questions et reconnexion | [Questions](docs/diagrams/07-questions-websocket.mmd), [rejeu](docs/diagrams/08-reconnexion-websocket.mmd) | [API et événements](contracts/README.md) |
| Objets et distances | [Fusion vision](docs/diagrams/10-vision-urbaine.mmd), [proximité](docs/diagrams/11-proximite.mmd) | [Vision et proximité](docs/04-vision-et-proximite.md) |
| Conteneurs et volumes | [Déploiement Compose](docs/diagrams/12-deploiement-compose.mmd) | [Installation et exploitation](docs/05-installation-et-exploitation.md) |
| Segmentation et suivi | [Fusion et décision](docs/diagrams/14-segmentation-suivi.mmd) | [Règles, classes, persistance et tests](docs/12-segmentation-suivi.md) |
| Personnes, vélos et voitures | [Décision par classe](docs/diagrams/16-proximite-urbaine.mmd) | [Détection et vigilance v4](docs/14-proximite-urbaine.md) |
| Suivi rapide et profondeur indépendante | [Deux cadences](docs/diagrams/15-profondeur-independante.mmd) | [Règles v3, reprise, contrats et mesures](docs/13-profondeur-independante.md) |
| Vidéo et YOLO26n | [Flux vidéo](docs/diagrams/13-video-live.mmd) | [Interface, 4 vCPU et mesures réelles](docs/11-video-live-yolo.md) |

## Vision réelle et alertes

`yolo_seg_track` active **YOLO26n-seg + ByteTrack**, avec **UniDepthV2 toutes les 2 secondes** dans un traitement indépendant. Le suivi des personnes, vélos et voitures exploite la croissance de taille et la surface du masque pour signaler une approche ou une proximité possible. Un rétrécissement dû à une visibilité partielle ne supprime pas immédiatement la vigilance. Les mètres restent attachés à leur capture source, consultable séparément dans le front.

La campagne de référence v3 (640 pixels, seuil 0,50) sur les 45 secondes de la vidéo NYC, sous quota Docker **4 CPU**, mesurait : **13,1 images analysées/s**, **69 ms** de vision en médiane, 21 échantillons de profondeur et 14 événements de proximité possible ; aucune erreur. Les modèles tournent avec 2 threads chacun. Le profil v4 utilise 768 pixels et un seuil de détection à 0,25, avec des alertes nouvelles à 0,50 ; il mesure **8,54 ips**, avec **14 alertes personnes et 4 alertes voitures**, sans erreur. Le test réel complémentaire sur photo déclenche aussi une alerte vélo. [Résultats et compromis](docs/14-proximite-urbaine.md). Ces chiffres locaux ARM64 ne prédisent pas ceux d’une autre VM et les alertes ne prouvent pas des distances physiques. [Architecture, résultats et limites](docs/13-profondeur-independante.md).

`yolo_objects` associe YOLO26 Nano et la profondeur métrique sur le même JPEG. Le frontend envoie 15 JPEG/s avec une file bornée par remplacement : la campagne locale à 4 CPU mesure **1,45 analyse complète/s**, avec **672 ms** de vision en médiane. [Activation, résultats et limites](docs/11-video-live-yolo.md).

`depth_anything_metric` estime la profondeur de trois régions fixes. `urban_objects` détecte des objets avec RT-DETR et leur associe une profondeur issue de Depth Anything Outdoor ou UniDepthV2. Une calibration réelle peut être fournie à UniDepthV2 : [activation et configuration](docs/05-installation-et-exploitation.md#activer-la-profondeur-métrique).

Par défaut, une observation exploitable à **≤ 1,5 m** est `near`, à **≥ 3 m** elle est `far`, sinon `medium`. Une distance absente ou un score insuffisant donne `unknown`. Une image avec au moins une observation `near` produit un événement `alert.proximity` regroupant toutes les observations proches. Les captures périmées sont refusées pour éviter une alerte obsolète ; le suivi est disponible dans `yolo_seg_track`, avec limitation des répétitions visuelles par piste ; le mobile gère encore la politique audio.

Après installation locale des dépendances de développement, avec le serveur en `urban_objects` :

```bash
.venv/bin/python scripts/test_real_images.py \
  --base-url http://127.0.0.1:8000 \
  --expected-provider urban_objects --repeat 3 \
  --output-dir output/urban-current-evaluation
```

Les [essais urbains](docs/09-test-urbain.md) et la [comparaison avec références physiques](docs/10-metric-depth-comparison.md) distinguent temps de calcul, erreurs métriques et alertes transmises. Les distances monoculaires restent des estimations : le modèle et la calibration doivent être validés sur la caméra Pi finale. Le LLM et les itinéraires restent prévus pour la suite ; les réponses aux questions sont déterministes et le STT/TTS appartient à l’iPhone.

## Développer et tester

Avec Python 3.12 ou 3.13 et `uv` :

```bash
uv sync --frozen --extra dev
.venv/bin/pytest
.venv/bin/ruff check app tests scripts
.venv/bin/python scripts/export_openapi.py
```

L’export écrit dans `contracts/openapi.json`. Les tests de protocole utilisent SQLite temporaire et tournent sans serveur externe. Les adaptateurs vision sont testés avec des sorties contrôlées, sans télécharger de poids. La validation de déploiement PostgreSQL complète ces tests.

Le `.gitignore` exclut `.env`, `.venv`, les caches, les poids et les futurs rapports `output/`. Les rapports historiques restent disponibles localement, mais ne sont pas nécessaires au démarrage après clonage. Les fichiers déjà suivis par Git restent suivis même si une règle les ignore. Les scripts [ETH3D](fixtures/metric-reference/README.md) et [Middlebury](fixtures/metric-bicycle-reference/README.md) permettent de reconstruire les références métriques. Les secrets et calibrations propres à une caméra restent locaux ; `*.example.json` peut servir d’exemple partagé.
