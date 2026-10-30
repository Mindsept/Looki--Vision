# LOOKi — Documentation du backend principal

Ce dossier décrit le prototype livré à la racine de ce dépôt (`backend/` dans le workspace LOOKi) : réception des JPEG du Raspberry Pi, analyse asynchrone, classement de proximité, alertes et échanges textuels avec l’iPhone. Les schémas représentent le code de cette première version. Les fonctions prévues pour une phase ultérieure sont explicitement identifiées.

```mermaid
flowchart TD
    Start[Comprendre le backend LOOKi] --> Arch[01 — Architecture et responsabilités]
    Arch --> Data[02 — Base de données et persistance]
    Arch --> Flow[03 — Contrats et parcours de bout en bout]
    Flow --> Vision[04 — Vision et proximité]
    Data --> Ops[05 — Installation et exploitation]
    Flow --> Team[06 — Intégration par équipe]
    Vision --> Decisions[07 — Décisions et limites du POC]
    Vision --> Indoor[08 — Essai sur photos réelles]
    Vision --> Urban[09 — Objets urbains et durées isolées]
    Urban --> Metric[10 — Comparaison avec références mesurées]
    Urban --> Live[11 — Vidéo live, YOLO26n et budget CPU]
    Live --> Seg[12 — Segmentation, suivi et proximité possible]
    Seg --> Async[13 — Suivi rapide et profondeur indépendante]
    Async --> UrbanV4[14 — Personnes, vélos et voitures]
    Ops --> Decisions
```

| Lecture | Contenu | Public principal |
| --- | --- | --- |
| [01 — Architecture](01-architecture.md) | Déploiement, couches, classes, dépendances, traitements | Toute l’équipe |
| [02 — Données](02-base-de-donnees.md) | Relations, tables, contraintes, transactions, reprise | Backend principal, centres |
| [03 — Contrats et flux](03-contrats-et-flux.md) | API HTTP, WebSocket, idempotence, événements, reconnexion | Embedded, iPhone, backend |
| [04 — Vision et proximité](04-vision-et-proximite.md) | Distances surestimées, segmentation, suivi, indices visuels, profondeur et limites | Backend, Embedded, Hardware |
| [05 — Exploitation](05-installation-et-exploitation.md) | Compose, configuration, migrations, tests, incidents | Backend, démonstration |
| [06 — Intégration](06-integration-equipe.md) | Travail indépendant, scénarios d’acceptation, branchements | Toute l’équipe |
| [07 — Décisions](07-decisions-et-limites.md) | Justification des choix et travail restant | PO, équipe technique |
| [08 — Test sur images réelles](08-test-images-reelles.md) | Photographies sourcées, procédure, observations et limites de la mesure | Backend, Embedded, Hardware |
| [09 — Test urbain](09-test-urbain.md) | Objets de rue, fusion avec la profondeur, temps isolés et alertes | Backend, Embedded, Hardware |
| [10 — Comparaison métrique](10-metric-depth-comparison.md) | Modèles candidats, calibration, références physiques, erreurs et alertes manquées | Backend, Embedded, Hardware, PO |
| [11 — Vidéo live](11-video-live-yolo.md) | Interface React, YOLO26n, file latest, tickets WebSocket, mesures réelles à 4 CPU | Backend, Embedded, PO |
| [12 — Segmentation et suivi](12-segmentation-suivi.md) | Masques, ByteTrack, indices contradictoires, classes, persistance et tests CPU | Backend, Embedded, PO |
| [13 — Profondeur indépendante](13-profondeur-independante.md) | Deux cadences, tendance de taille, mémoire du risque, reprise et mesures à 4 CPU | Backend, Embedded, iPhone, PO |
| [14 — Proximité urbaine](14-proximite-urbaine.md) | Profils personnes/vélos/voitures, détection, motifs et tests à 4 CPU | Backend, Embedded, iPhone, PO |
| [15 — Mobile Realtime et Pi](15-mobile-realtime-et-pi.md) | Contrats v1.1, classes, tables et limites de validation | Mobile, Embedded, backend |

Les formats échangeables se trouvent dans [`../contracts/`](../contracts/README.md). La spécification HTTP est publiée à l’exécution sur `/openapi.json` et `/docs`. Le schéma WebSocket complète OpenAPI, qui ne décrit pas les messages échangés sur une connexion ouverte.

## Sources Mermaid autonomes

Les fichiers `.mmd` du [catalogue des diagrammes](diagrams/README.md) couvrent l’architecture, les couches, les classes métier et vision, les tables BDD, l’ingestion, les questions, la reconnexion, le cycle des images, la proximité et le déploiement Compose. Ils complètent les schémas intégrés aux chapitres.

La racine de toutes les commandes est désormais le **dépôt `backend/`** : `compose.yaml`, `.env.example`, `README.md`, `contracts/` et ce dossier `docs/` sont réunis au même endroit.

## Parcours conseillé

Pour lancer une démonstration, commencer par le [README du dépôt](../README.md), puis le guide d’exploitation. Pour connecter le Pi ou l’iPhone, lire les contrats et le guide d’intégration. Pour comprendre l’approche face aux distances surestimées, lire le chapitre vision, puis les chapitres 13 et 14 pour les règles et leurs mesures.

```mermaid
flowchart LR
    Pi["Raspberry Pi — JPEG"] -->|"HTTPS multipart"| API["FastAPI — contrôleurs et services"]
    API --> DB[("PostgreSQL")]
    API --> Vision["YOLO, suivi et profondeur indépendante"]
    Vision --> DB
    App["iPhone — STT, WebRTC, clips"] <-->|"HTTPS questions et état"| API
    API -->|"WSS alertes"| App
    API <-->|"WSS sideband"| RT["OpenAI Realtime"]
    RT -->|"Audio WebRTC direct"| App
    API --> Centres["Contexte centres — mock ou REST"]
```

Le profil de `.env.example` active la vision réelle **YOLO26n-seg + ByteTrack + UniDepthV2**, sur CPU. Copier cet exemple dans `.env` avant le premier `compose up --build` : les poids se téléchargent au démarrage et restent dans le volume Docker `looki_model_cache`. Le mode vision `mock` reste disponible explicitement pour tester le protocole ; le contexte des centres reste simulé par défaut. La profondeur métrique est une estimation monoculaire à valider sur la caméra et l’environnement retenus ; la vigilance visuelle ne certifie pas une distance physique.
