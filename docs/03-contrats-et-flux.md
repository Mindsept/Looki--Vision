# 03 — Contrats et flux applicatifs

Sources Mermaid : [ingestion et alerte](diagrams/06-ingestion-et-alerte.mmd), [questions](diagrams/07-questions-websocket.mmd), [reconnexion](diagrams/08-reconnexion-websocket.mmd).

**Mise à jour du 30 octobre 2026 :** les contrats [Pi JPEG](../contracts/raspberry-backend.md), [mobile HTTP](../contracts/mobile-backend.md), [accès](../contracts/session-access.md) et [alertes WSS](../contracts/alerts.md) sont normatifs.

## Vue des échanges

```mermaid
sequenceDiagram
    participant iOS as iPhone
    participant API as FastAPI
    participant Context as Provider centres
    participant Pi as Raspberry Pi
    participant DB as PostgreSQL
    participant Worker as Worker vision
    iOS->>API: POST sessions
    API->>Context: Charger le contexte du centre
    Context-->>API: Lieux et graphe
    API->>DB: Session active + session.state
    API-->>iOS: 201 et session_id
    iOS->>API: Ouvrir WS avec after et Bearer
    API-->>iOS: État initial puis journal
    Note over Pi,iOS: Le scénario de démonstration partage le session_id
    Pi->>API: POST image JPEG, frame_id, timestamp
    API->>DB: Image pending
    API-->>Pi: 202 Accepted
    Worker->>DB: Lire et réserver une image
    Worker->>Worker: Inférence et proximité
    Worker->>DB: Résultat et événements en une transaction
    API->>DB: Lire les événements après le curseur
    API-->>iOS: frame.analyzed puis alerte éventuelle
    iOS->>iOS: Clip local selon sound_id
```

Les endpoints et structures complètes sont dans [le contrat partagé](../contracts/README.md) et l’OpenAPI. Cette page décrit leur comportement et leurs garanties.

## Admission d’une capture

```mermaid
flowchart TD
    Request[Requête d’upload] --> Body[Taille totale HTTP bornée]
    Body --> Auth[Authentification Bearer]
    Auth --> Params[UUID, timestamp et image/jpeg]
    Params --> Existing{Même session et frame_id<br/>déjà connus ?}
    Existing -->|Oui| Equal{Même empreinte et instant ?}
    Equal -->|Oui| Previous[200 — état actuel de l’image existante]
    Equal -->|Non| Conflict[409 — frame_id_conflict]
    Existing -->|Non| Decode[Décoder et vérifier le vrai JPEG]
    Decode --> Active{Session active ?}
    Active -->|Non| Closed[409 — session_closed]
    Active -->|Oui| Fresh{Capture assez récente ?}
    Fresh -->|Non| Timestamp[422 — invalid_timestamp]
    Fresh -->|Oui| Capacity{Capacité disponible ?}
    Capacity -->|Non| Full[429 — queue_full<br/>Retry-After: 1]
    Capacity -->|Oui| Save[Persister pending et octets JPEG]
    Save --> Accepted[202 — traitement asynchrone]
```

Le backend recontrôle l’existence de l’image après le décodage, dans la section d’admission sérialisée. Deux requêtes concurrentes portant le même identifiant ne créent pas deux captures. La limite de file compte les images `pending` et `processing` de toutes les sessions, avec un maximum de `32` par défaut.

La validation des dimensions empêche de se fier à la seule taille compressée. Un fichier déclaré `image/jpeg` mais qui ne contient pas un JPEG valide est refusé. La validation vérifie aussi le chargement des pixels, pour détecter des fichiers tronqués qu’une simple inspection d’en-tête pourrait manquer.

## Réessai après perte de réponse

```mermaid
sequenceDiagram
    participant Pi
    participant API
    participant DB
    Pi->>Pi: Générer frame_id, timestamp et JPEG une seule fois
    Pi->>API: Première tentative
    API->>DB: Insérer l’image
    API--xPi: Réponse 202 perdue
    Pi->>API: Deuxième tentative, données strictement identiques
    API->>DB: Retrouver session_id + frame_id
    DB-->>API: Image déjà connue
    API-->>Pi: 200 et état actuel
    Note over API,DB: Aucun nouveau travail ajouté
```

L’idempotence porte sur l’identité de capture, les octets et l’instant de capture. Elle ne compare pas seulement le nom du fichier. Une représentation d’horodatage équivalente après normalisation UTC décrit le même instant. Le réessai exact reste accepté après la fermeture de session ou après la fenêtre de fraîcheur, car il ne correspond pas à une nouvelle admission.

## Analyse, âge et alerte

```mermaid
sequenceDiagram
    participant W as Worker
    participant DB
    participant Model as Provider vision
    W->>DB: Réserver la plus ancienne image pending
    W->>W: Vérifier l’âge depuis captured_at
    alt Capture déjà périmée
        W->>DB: failed + analysis.failed code stale_frame
    else Capture fraîche
        W->>Model: analyze hors boucle asyncio
        Model-->>W: VisionResult
        W->>W: Recontrôler l’âge après l’inférence
        alt Résultat devenu périmé
            W->>DB: failed + analysis.failed code stale_frame
        else Résultat exploitable
            W->>W: Classer les observations
            W->>DB: completed + detections + frame.analyzed
            opt Au moins une observation near
                W->>DB: Ajouter alert.proximity
            end
            W->>DB: COMMIT commun
        end
    end
```

L’âge est contrôlé à l’upload, avant le calcul et après le calcul. Une inférence lente peut donc finir par un événement d’échec au lieu d’une alerte obsolète. Une exception de provider produit `analysis.failed` avec `inference_failed`. Ces deux échecs sont consultables sur le résultat de l’image et dans le journal.

Un événement d’alerte regroupe les détections proches d’une image et expose `nearest_distance_m`, la plus petite distance parmi elles. Il y a au plus une alerte de proximité par résultat d’image, même si plusieurs régions sont proches, puis une nouvelle alerte pour chaque nouvelle image proche. Aucun cooldown n’est appliqué.

## Connexion et reprise WebSocket

```mermaid
stateDiagram-v2
    [*] --> disconnected
    disconnected --> connecting: Ouvrir avec Bearer et after
    connecting --> disconnected: Authentification ou session refusée
    connecting --> connected: Connexion acceptée
    connected --> connected: État initial avec id null
    connected --> connected: Rejeu puis nouveaux événements
    connected --> connected: événements, ping, pong
    connected --> disconnected: Rupture réseau ou client trop lent
    disconnected --> connecting: Temporisation puis reprise du curseur
```

```mermaid
sequenceDiagram
    participant Phone as iPhone
    participant Socket as WebSocket
    participant DB as Journal
    Phone->>Socket: Connecter after=17
    Socket-->>Phone: session.state avec id null
    Socket->>DB: Lire id > 17 pour cette session
    DB-->>Socket: 19, 22, 23
    Socket-->>Phone: Événement 19
    Phone->>Phone: Traiter puis mémoriser 19
    Socket--xPhone: Événement 22, connexion interrompue
    Phone->>Socket: Reconnecter after=19
    Socket-->>Phone: session.state avec id null
    Socket->>DB: Lire id > 19
    DB-->>Socket: 22, 23
    Socket-->>Phone: Événements 22 et 23
    Phone->>Phone: Dédupliquer puis avancer le curseur
```

Le journal est interrogé toutes les `0.2 s` par défaut. Les pages contiennent au plus `100` événements par lecture WebSocket et sont enchaînées immédiatement lorsqu’elles sont pleines. Le flux est livré au moins une fois lorsque le client reprend avec un curseur conservé : il doit gérer les répétitions.

Le rejeu conserve les événements historiques, y compris les anciennes alertes. Avant de jouer un clip, le mobile vérifie `payload.expires_at`, calculé depuis la capture ; une alerte trop ancienne reste dans l’historique mais n’est pas annoncée comme un obstacle présent. La politique audio doit donner priorité aux alertes actuelles. Mémoriser `max(curseur, event.id)` pour les événements durables traités : une répétition explicite de question peut retransmettre une ancienne réponse déjà livrée.

Les envois concurrents du même socket sont sérialisés. Un envoi qui dépasse `5 s` entraîne l’arrêt du traitement de cette connexion ; les événements durables restent disponibles pour une reprise. Une trame binaire ou trop volumineuse est refusée avec le code WebSocket `1009`. Un Bearer absent/invalide ou une session inconnue est rejeté avant acceptation ; selon le client ASGI, cela apparaît comme un échec du handshake HTTP plutôt qu’une frame de clôture visible `1008`.

Les erreurs applicatives d’un message valide au niveau transport sont renvoyées dans un événement transitoire `error`. Le client peut conserver la connexion pour corriger son prochain message.

## Historique par HTTP

```mermaid
flowchart LR
    Start[after = 0] --> Get[GET events?after=curseur&limit=100]
    Get --> Response[Tableau ordonné d’événements]
    Response --> Empty{Tableau vide ?}
    Empty -->|Oui| End[Historique rattrapé à cet instant]
    Empty -->|Non| Process[Traiter les événements]
    Process --> Cursor[Curseur = dernier id]
    Cursor --> Get
```

`after` vaut au minimum `0`. `limit` vaut de `1` à `500`, avec `100` par défaut. La réponse est un tableau brut ; il n’y a pas d’enveloppe `items` ni de champ `next_cursor`. Reprendre avec l’identifiant du dernier événement reçu. Les accès sont filtrés par session.

## Questions et idempotence

Le mobile utilise maintenant [le cycle HTTP durable](../contracts/mobile-backend.md). Le schéma ci-dessous décrit uniquement le **diagnostic WSS opérateur historique**, conservé pour les fixtures ; il ne décrit pas Realtime.

```mermaid
flowchart TD
    Message[question + request_id + text] --> Validate[Valider JSON et retirer espaces périphériques]
    Validate --> Existing{request_id déjà traité ?}
    Existing -->|Oui| Same{Même texte normalisé ?}
    Same -->|Oui| Replay[Renvoyer le même answer durable]
    Same -->|Non| Conflict[error: request_id_conflict]
    Existing -->|Non| Active{Session active ?}
    Active -->|Non| Closed[error: session_closed]
    Active -->|Oui| Observation[Lire la dernière image completed<br/>par instant de capture]
    Observation --> Fresh{Observation encore récente ?}
    Fresh -->|Oui| Answer[Réponse déterministe<br/>avec description existante]
    Fresh -->|Non| Unknown[Aucune observation récente disponible]
    Answer --> Persist[Persister answer]
    Unknown --> Persist
    Persist --> Publish[Publier par le journal WebSocket]
```

Le backend ne déduit pas encore l’intention de la question. Il retourne les dernières observations récentes, rappelle le mode POC et fournit le nom du centre dans le payload. Le champ `mode` vaut `observations_only`. Il n’y a ni génération d’itinéraire ni réponse fondée sur des connaissances générales.

## Erreurs et diagnostic

| HTTP / événement | Exemple de cause | Réaction client |
| --- | --- | --- |
| `401` | Clé absente ou invalide | Vérifier l’en-tête Bearer |
| `404` | Session, image ou centre inconnu | Vérifier les identifiants |
| `409` | Session fermée ou identité de capture réutilisée différemment | Corriger le parcours ou l’identité de capture |
| `413` | Trop d’octets ou trop de pixels | Réduire l’image côté Pi |
| `415` | Format non JPEG ou MIME différent | Envoyer une vraie image avec le bon MIME |
| `422` | Champs invalides, image illisible ou horodatage hors fenêtre | Corriger la donnée source |
| `429` | File globale pleine | Respecter `Retry-After: 1` et réduire la cadence |
| `503` | Contexte centres indisponible ou readiness en échec | Examiner la dépendance concernée |
| `analysis.failed` | `stale_frame` ou `inference_failed` | Signaler l’indisponibilité d’analyse, lire l’état de l’image |

Les erreurs métier HTTP utilisent `{"error":{"code":"...","message":"..."}}`. Les erreurs de validation FastAPI et d’authentification utilisent le format `detail` de FastAPI ; un dépassement détecté pendant la lecture d’un corps HTTP utilise également `detail` avec `code` et `message`. Ne pas supposer une enveloppe unique pour toutes les erreurs. Les traces internes sont journalisées sur le serveur, pas renvoyées dans les réponses métier.
