# 01 — Architecture logicielle

> Pour `yolo_seg_track`, le flux courant sépare le suivi rapide et la profondeur : voir [le chapitre 13](13-profondeur-independante.md). Les parcours synchrones ci-dessous restent applicables aux autres providers et au mode `LOOKI_DECOUPLED_DEPTH=false`.

Sources Mermaid : [architecture](diagrams/01-architecture.mmd), [couches](diagrams/02-couches.mmd), [classes](diagrams/03-classes-services.mmd) et [déploiement](diagrams/12-deploiement-compose.mmd).

## Périmètre et frontières

Le backend reçoit des images JPEG indépendantes. Il orchestre un provider de vision, applique une politique de proximité, persiste le résultat et expose les textes à l’application mobile. Le Raspberry Pi utilise l’iPhone comme point d’accès réseau ; il n’appelle pas l’application iOS.

```mermaid
flowchart TB
    subgraph Lunettes
        Camera[Caméra] --> Pi[Raspberry Pi<br/>Capture et JPEG]
    end
    subgraph Mobile
        Hotspot[Partage de connexion Wi-Fi]
        iOS[Application iPhone]
        STT[STT — voix vers texte]
        TTS[TTS — texte vers voix]
        STT --> iOS
        iOS --> TTS
        TTS --> Audio[Écouteurs]
    end
    subgraph Serveur
        FastAPI[Backend FastAPI]
        Worker[Worker d’analyse]
        Model[Provider vision<br/>mock, profondeur ou objets urbains]
        DB[(PostgreSQL)]
        FastAPI --> DB
        Worker --> DB
        Worker --> Model
    end
    Pi -. Accès Internet .-> Hotspot
    Pi -->|POST JPEG - HTTP local, HTTPS distant| FastAPI
    iOS <-->|Un WebSocket bidirectionnel<br/>WS local, WSS distant| FastAPI
    FastAPI -->|Chargement du contexte| Centres[Provider centres<br/>JSON local ou REST]
    Worker -->|Événements persistés| DB
```

Les routes HTTP ne réalisent pas l’inférence dans la requête d’upload. Une image valide est enregistrée puis une réponse `202 Accepted` est renvoyée. Le worker récupère les images en attente. Le flux WebSocket lit le journal d’événements en base et transmet les nouvelles entrées au mobile.

## Déploiement du POC

```mermaid
flowchart TB
    Client[Pi, application ou scripts locaux] -->|localhost:8000| Port[Port backend publié]
    subgraph Compose[Projet Docker Compose]
        Port --> App[Conteneur backend<br/>FastAPI + un worker Uvicorn]
        App --> Migration[Alembic upgrade head<br/>au démarrage]
        App --> Worker[Worker asynchrone intégré]
        App -->|SQL / réseau Compose| PG[Conteneur db<br/>PostgreSQL 16]
        Worker --> PG
        Migration --> PG
        PG --> Volume[(Volume PostgreSQL persistant)]
    end
    subgraph Optionnel[Configuration de vision réelle]
        App --> Depth[Profondeur métrique<br/>Depth Anything ou UniDepthV2]
        Depth --> Zones[Provider régions<br/>gauche, centre et droite]
        App --> Detector[Provider urbain<br/>détecteur RT-DETR]
        Detector --> Fusion[Objets et profondeur métrique]
        Depth --> Fusion
    end
```

La base n’a pas besoin d’un port publié sur l’hôte pour que le backend y accède. Un seul processus applicatif est prévu : le worker s’exécute dans le même cycle de vie que FastAPI. Augmenter le nombre de workers ou de réplicas demanderait un mécanisme de réservation distribuée des images et une politique de reprise adaptée.

Compose fournit HTTP et WS pour le développement local. Pour un accès distant, placer un reverse proxy TLS devant le backend pour obtenir HTTPS et WSS et conserver l’en-tête `Authorization`. Le chiffrement n’est pas fourni par Uvicorn dans cette composition.

## Organisation par responsabilité

```mermaid
flowchart TD
    HTTP[Contrôleurs HTTP et WebSocket] --> Schemas[Schémas Pydantic<br/>validation et sérialisation]
    HTTP --> Services[Services<br/>cas d’usage]
    Worker[Worker<br/>ordonnancement et reprise] --> Services
    Services --> Repos[Repositories<br/>requêtes et persistance]
    Repos --> Models[Modèles SQLAlchemy]
    Models --> PG[(PostgreSQL)]
    Services --> Providers[Providers<br/>vision et contexte]
    Providers --> External[Modèles ou API externes]
    Services --> Domain[Domaine<br/>observations et politique de proximité]
    Config[Configuration] --> HTTP
    Config --> Services
    Config --> Providers
```

| Couche | Responsabilité | Ne doit pas porter |
| --- | --- | --- |
| Contrôleur | Lire une requête, appliquer l’authentification, appeler un cas d’usage, renvoyer le protocole attendu | Calcul de proximité, requêtes SQL dispersées |
| Schéma | Contraintes de format et forme des échanges | Connexion réseau ou inférence |
| Service | Enchaîner les opérations, décider les transitions et les événements | Détails internes d’un modèle ML |
| Repository | Charger, insérer et modifier les données d’un agrégat | Règles d’alerte ou présentation audio |
| Provider | Adapter une dépendance remplaçable : modèle ou centres | Routage FastAPI |
| Domaine | Représenter les observations et classer les distances | Dépendance aux endpoints HTTP |
| Worker | Reprendre les images, appeler l’analyse, gérer le statut final | Transcription et synthèse vocales |

La séparation permet de garder les mêmes contrôleurs et cas d’usage quand le provider mock est remplacé par une implémentation réelle. Elle ne suppose pas un microservice par couche : toutes ces classes sont dans le même backend.

## Classes et composition du runtime

```mermaid
classDiagram
    class Runtime {
        +Settings settings
        +Database db
        +Lock write_lock
        +ContextProvider centres
        +VisionProvider vision
        +SessionService sessions
        +FrameService frames
        +QuestionService questions
        +AnalysisWorker worker
    }
    class SessionService {
        +create(data)
        +get(session_id)
        +close(session_id)
        +events(session_id, after, limit)
    }
    class FrameService {
        +ingest(session_id, frame_id, timestamp, image)
        +get(session_id, frame_id)
    }
    class QuestionService {
        +answer(session_id, question)
    }
    class AnalysisWorker {
        +recover()
        +run()
        +process(frame)
        +fail(frame, code, text)
    }
    class ProximityService {
        +classify(distance_m, confidence) str
    }
    class SessionRepository {
        +get(session_id)
        +create(center_id, user_id, context)
    }
    class FrameRepository {
        +get(session_id, frame_id)
        +create(fields)
        +claim_next()
        +reset_interrupted()
        +latest_completed(session_id)
        +detections(internal_id)
    }
    class EventRepository {
        +add(session_id, type, payload)
        +after(session_id, cursor, limit)
        +for_request(session_id, request_id)
    }
    class ContextProvider {
        +context(center_id)
    }
    class VisionProvider {
        <<interface>>
        +analyze(image) VisionResult
    }
    Runtime *-- SessionService
    Runtime *-- FrameService
    Runtime *-- QuestionService
    Runtime *-- AnalysisWorker
    Runtime *-- ContextProvider
    Runtime --> VisionProvider
    SessionService --> SessionRepository
    SessionService --> EventRepository
    SessionService --> ContextProvider
    FrameService --> FrameRepository
    FrameService --> SessionRepository
    QuestionService --> FrameRepository
    QuestionService --> EventRepository
    AnalysisWorker --> FrameRepository
    AnalysisWorker --> EventRepository
    AnalysisWorker --> VisionProvider
    AnalysisWorker *-- ProximityService
```

Le `Runtime` est créé une fois pendant le démarrage de l’application. Il compose les dépendances et expose les services aux contrôleurs via l’état FastAPI. Les repositories reçoivent une session SQLAlchemy correspondant à l’opération en cours ; ils ne conservent pas une connexion partagée entre requêtes.

```text
backend/                       # Racine du dépôt Git
├── app/
│   ├── main.py                  # Application FastAPI et cycle de vie
│   ├── controllers/             # Routes HTTP et WebSocket
│   ├── core/                    # Configuration, DB, auth, runtime, limites
│   ├── models/                  # Entités SQLAlchemy
│   ├── schemas/                 # Schémas d’entrée et de sortie
│   ├── repositories/            # Accès SQL
│   ├── services/                # Sessions, images, questions, worker, proximité
│   ├── providers/               # Contexte centres mock ou REST
│   └── vision/                  # Contrat et providers vision
├── compose.yaml                 # Backend + PostgreSQL depuis cette racine
├── README.md                    # Démarrage et index
├── docs/                        # Chapitres détaillés et diagrams/*.mmd
├── contracts/                   # OpenAPI, WebSocket et exemples
├── alembic/                     # Migrations versionnées
├── fixtures/                    # JPEG de démonstration et contexte JSON
├── scripts/                     # Démo, génération des images, export OpenAPI
└── tests/                       # Protocole, reprise, vision et règles
```

## Circulation des données

```mermaid
flowchart LR
    Raw[JPEG + horodatage + frame_id] --> Valid[Validation et contrôle de session]
    Valid --> Pending[(Image pending)]
    Pending --> Analysis[Worker + provider vision]
    Context[(Contexte mémorisé<br/>dans la session)] --> Questions[Réponses déterministes<br/>nom et identifiant du centre]
    Questions --> Events
    Analysis --> Observations[Observations et distances]
    Observations --> Rules[Politique de proximité]
    Rules --> Tx[Transaction résultat]
    Tx --> Result[(Frame + detections)]
    Tx --> Events[(Journal events)]
    Events --> Replay[Lecture incrémentale]
    Replay --> Phone[Application iPhone]
```

La base joue deux rôles : stockage durable des résultats et file d’attente minimale. Le journal d’événements sert aussi de source de reprise pour les clients WebSocket ; le backend n’a pas besoin que le mobile soit connecté au moment de l’analyse.

## Durées de vie

```mermaid
sequenceDiagram
    participant Process as Processus backend
    participant DB as PostgreSQL
    participant Worker as Worker intégré
    participant API as FastAPI
    Process->>DB: Appliquer les migrations
    Process->>Worker: Démarrer le cycle de vie
    Worker->>DB: Repasser les images processing en pending
    Process->>API: Accepter les requêtes
    loop Tant que le service tourne
        Worker->>DB: Chercher une image pending
        DB-->>Worker: Image disponible ou aucune
        Worker->>Worker: Analyser et persister
    end
    Process->>Worker: Arrêt du service
    Worker-->>Process: Arrêt de la boucle
```

Un redémarrage peut reprendre une inférence interrompue. Le worker effectue aussi une reprise après une erreur transitoire de base empêchant la validation des résultats. L’opération de calcul peut donc être exécutée à nouveau, mais les résultats et les événements d’un traitement terminé sont enregistrés ensemble. La garantie visée est une reprise cohérente du POC, pas une exécution physique unique du modèle.

## Ce qui appartient aux autres composants

```mermaid
flowchart LR
    subgraph HorsBackend[Autres responsabilités]
        Mechanics[Monture, caméra et alimentation]
        Capture[Capture, compression et cadence]
        Speech[STT, TTS et routage audio]
        Admin[CRUD centres, lieux et graphe]
    end
    subgraph Backend[Backend principal livré]
        Ingest[Ingestion des JPEG]
        Infer[Analyse et proximité]
        Notify[Journal et WebSocket]
        Context[Lecture du contexte]
    end
    Capture --> Ingest
    Mechanics --> Capture
    Ingest --> Infer
    Infer --> Notify
    Notify --> Speech
    Admin --> Context
    Context --> Questions[Réponse textuelle<br/>nom du centre]
```

Le backend ne pilote pas la caméra, ne calcule pas le partage de connexion, ne produit pas de son et ne maintient pas le référentiel métier des centres. Dans cette phase, une question reçoit une réponse déterministe fondée sur les observations disponibles ; l’intégration d’un LLM, d’itinéraires et d’un modèle vidéo temporel reste à ajouter.

Le contexte centres est conservé dans la session et ses métadonnées sont jointes aux réponses. Il n’est pas envoyé au provider de vision dans cette version et ne modifie pas encore les classes reconnues ou le calcul de proximité.
