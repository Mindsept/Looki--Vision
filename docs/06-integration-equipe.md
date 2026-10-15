# 06 — Intégration avec l’équipe

## Avancer en parallèle

```mermaid
flowchart TD
    Contracts[Contrats partagés et exemples JSON] --> Embedded[Elyes — capture et upload]
    Contracts --> Mobile[Leandro — WebSocket et audio]
    Contracts --> Centres[Hugo — contexte REST]
    Hardware[Mathis — caméra et alimentation] --> Embedded
    Embedded --> Core[Hamza — backend principal]
    Mobile <--> Core
    Centres --> Core
    Core --> Mock[Provider vision simulé]
    Core --> Real[Provider profondeur métrique]
```

Chaque intégration peut démarrer avec `docker compose up --build -d`. Aucun modèle lourd ni Raspberry Pi n’est requis pour les scénarios simulés. Les contrats de référence se trouvent dans `/contracts`, et les tests de protocole dans `tests`.

## Pi → backend

```mermaid
sequenceDiagram
    participant Config as Configuration de démonstration
    participant Pi
    participant Backend
    Config->>Backend: Créer une session
    Backend-->>Config: session_id
    Config->>Pi: Fournir URL, clé API et session_id
    loop Cadence de capture configurée sur le Pi
        Pi->>Pi: Capturer, redimensionner et compresser en JPEG
        Pi->>Pi: Fixer frame_id et timestamp avec fuseau
        Pi->>Backend: POST multipart image, timestamp, frame_id
        alt 202
            Backend-->>Pi: Image durablement acceptée
        else Réponse perdue ou incident réseau
            Pi->>Backend: Réessayer les mêmes octets, UUID et timestamp
            Backend-->>Pi: 200 si la capture était déjà acceptée
        else 429
            Backend-->>Pi: File pleine
            Pi->>Pi: Réduire la cadence / attendre avant nouvelle capture
        end
    end
```

Le Pi doit conserver l’UUID, l’horodatage et les octets exacts tant qu’il réessaie une capture. Recompresser le même fichier tout en réutilisant le même `frame_id` peut produire un conflit, puisque l’empreinte change. Pour une nouvelle capture, générer un nouvel UUID.

Synchroniser l’horloge du Pi. Une image trop ancienne ou datée trop loin dans le futur est refusée. Le backend applique aussi une limite d’âge avant l’analyse ; accumuler une longue file de captures pendant une coupure est peu utile pour l’alerte de proximité. Le contrôle de la cadence appartient au logiciel embarqué.

Le `session_id` est fourni aux deux clients par le parcours de démonstration. Aucun protocole d’appairage automatique, QR code ou canal applicatif iPhone → Pi n’est ajouté dans ce POC.

## iPhone ↔ backend

```mermaid
flowchart TD
    Launch[Ouvrir une session existante<br/>ou en créer une] --> Connect[WSS avec Authorization Bearer<br/>et after = dernier curseur]
    Connect --> State[Recevoir session.state initial]
    State --> Receive[Lire les événements]
    Receive --> Durable{ID durable ?}
    Durable -->|Oui| Dedup[Ignorer un ID déjà traité]
    Dedup --> Audio[Planifier TTS selon le type]
    Audio --> Save[Conserver le dernier ID traité]
    Durable -->|Non| Control[Traiter état, pong ou erreur]
    Mic[Microphone] --> STT[STT côté iPhone]
    STT --> Question[Envoyer question + request_id]
    Question --> Connect
    Receive -->|Déconnexion| Reconnect[Reconnexion avec temporisation]
    Reconnect --> Connect
```

L’application utilise une seule connexion WebSocket bidirectionnelle pour les questions et les réponses. L’en-tête Bearer doit être ajouté lors de la requête d’ouverture du WebSocket. La clé n’est pas placée dans l’URL. Un client WebSocket natif peut porter cet en-tête ; le constructeur WebSocket standard d’un navigateur ne permet pas librement les mêmes en-têtes, ce qui doit être pris en compte pour un éventuel dashboard web.

Les événements durables peuvent être livrés à nouveau après une reconnexion. Dédupliquer par `id` avant de relancer une vocalisation déjà planifiée. Une alerte pour une nouvelle image porte un nouvel `id`, même si son texte ressemble à la précédente. La politique de répétition audio est à définir côté mobile en gardant cette distinction.

Avant le TTS d’une alerte rejouée, vérifier son `captured_at` : le backend conserve volontairement l’historique et ne retire pas les événements anciens lors de la reconnexion. Conserver une alerte périmée pour consultation, sans la présenter comme un danger actuel. Donner priorité aux alertes fraîches dans la file audio. Avancer le curseur avec le maximum de sa valeur actuelle et de l’identifiant traité.

Les messages `pong`, `error` et l’état initial de connexion ont `id: null` : ils ne font pas avancer le curseur. Ne pas remettre le curseur à zéro à leur réception. Les descriptions et réponses restent textuelles ; le backend n’envoie pas de flux audio.

### Questions

```mermaid
sequenceDiagram
    participant User as Utilisateur
    participant iOS as iPhone
    participant Backend
    User->>iOS: Question vocale
    iOS->>iOS: Transcrire la voix
    iOS->>Backend: question avec UUID request_id et text
    Backend->>Backend: Lire les observations disponibles
    Backend-->>iOS: answer durable avec request_id
    iOS->>iOS: TTS du texte
    iOS-->>User: Réponse dans les écouteurs
```

La réponse de cette phase est déterministe. Tester les cas « aucune image analysée », « observation proche », « résultat devenu trop ancien » et « session fermée ». Garder le même `request_id` si une question doit être répétée après un incident ; en générer un nouveau pour une question réellement nouvelle.

## Backend centres → backend principal

```mermaid
sequenceDiagram
    participant Client
    participant Main as Backend principal
    participant Provider as Provider centres
    participant Centres as Backend centres ou JSON local
    participant DB as PostgreSQL
    Client->>Main: POST sessions avec center_id
    Main->>Provider: context(center_id)
    Provider->>Centres: Lire le contexte
    Centres-->>Provider: JSON version 1.0
    Provider->>Provider: Valider références de lieux et graphe
    alt Contexte valide
        Provider-->>Main: Contexte normalisé
        Main->>DB: Sauver le snapshot dans sessions.context
        Main-->>Client: Session active
    else Centre inconnu
        Main-->>Client: 404
    else Dépendance indisponible ou contenu invalide
        Main-->>Client: 503
    end
```

Le contrat REST attendu est `GET /api/v1/centres/{center_id}/context`. Le provider local lit le fichier `fixtures/centres/{center_id}.json`. Le passage au provider REST se fait par configuration ; aucune réécriture des contrôleurs n’est nécessaire.

Le contexte rassemble des lieux et un graphe. Chaque nœud référence un lieu existant ; chaque arête référence deux nœuds existants et porte une distance positive ou nulle. Les identifiants de lieu et de nœud sont uniques dans leurs collections. Le centre retourné doit correspondre au centre demandé et la version de schéma doit être `1.0`.

```mermaid
flowchart LR
    Welcome[Lieu accueil] -. référence .-> N1[Nœud accueil]
    Hall[Lieu couloir] -. référence .-> N2[Nœud couloir]
    Exit[Lieu sortie] -. référence .-> N3[Nœud sortie]
    N1 -->|Arête et distance| N2
    N2 -->|Arête et accessibilité| N3
```

Ce graphe donne un contexte métier. Le backend livré n’en déduit pas encore la position des lunettes, un trajet piéton ou une instruction de navigation. Ajouter des lieux en base ne suffit pas à localiser une image dans le bâtiment.

## Scénarios de recette partagée

| Scénario | Attendu côté backend | À vérifier côté client |
| --- | --- | --- |
| Fixture proche | `frame.analyzed`, puis `alert.proximity` | TTS de l’alerte et indication de simulation |
| Deux nouvelles images proches | Une alerte pour chaque image | Pas de perte d’événement |
| Même upload répété | `200`, même image logique, pas de nouvelle analyse | Réutilisation de l’identifiant |
| Même UUID, contenu modifié | `409` | Journaliser le conflit et corriger le producteur |
| Fixture lointaine | Analyse classée `far`, sans alerte proche | Afficher ou vocaliser selon le parcours retenu |
| Image invalide | Refus avant analyse | Gestion de l’erreur et prochaine capture |
| Coupure du mobile pendant l’analyse | Événements conservés en base | Reprise avec `after`, déduplication |
| Redémarrage backend pendant un calcul | Reprise des images interrompues | Reconnexion du mobile |
| Question | Événement `answer` avec le même `request_id` | Corrélation et TTS |
| Centre indisponible | Échec explicite de création de session | Ne pas présenter une session fictivement connectée |
| Session clôturée | Rejet de nouveaux uploads/questions ; historique lisible | Arrêt de capture et fin du parcours |

## Mesure de bout en bout

```mermaid
flowchart LR
    T0[T0 capture Pi] --> T1[T1 réception backend]
    T1 --> T2[T2 début inférence]
    T2 --> T3[T3 commit résultat]
    T3 --> T4[T4 événement reçu iPhone]
    T4 --> T5[T5 début TTS]
    T5 --> T6[T6 son dans écouteurs]
```

Relever les durées de capture/upload, attente, inférence, livraison WebSocket et démarrage audio. Les champs backend ne mesurent pas la latence des écouteurs. Pour comparer des horloges Pi/serveur/iPhone, vérifier leur synchronisation ou compléter les mesures par des durées monotones locales.

Mesurer également le nombre d’images acceptées, refusées pour saturation, expirées avant analyse et échouées au modèle, ainsi que les coupures WebSocket. Une moyenne de latence seule peut masquer une accumulation d’images périmées.
