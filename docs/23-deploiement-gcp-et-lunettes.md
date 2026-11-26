# GCP, HTTPS et mode lunettes

Le déploiement est automatisé dans [Looki-PoC-IaC/ansible](https://github.com/Mindsept/Looki-PoC-IaC/tree/main/ansible). L'IaC crée une VM Ubuntu 24.04 `e2-standard-8` (8 vCPU, 32 Go), un disque persistant de 50 Go, une IP fixe et les règles réseau. Ansible déploie un commit précis du backend et Caddy, sans copier les clés Git ou le `.env` du développeur.

```mermaid
flowchart LR
    Pi[Pi · capture JPEG] -->|HTTPS 443 · pi_token| Edge[Caddy · certificat public]
    Browser[Navigateur · mode Pi] -->|HTTPS / WSS · mobile_token| Edge
    Edge -->|réseau Docker · backend:8000| API[FastAPI · un processus]
    API --> DB[(PostgreSQL · volume persistant)]
    API --> YOLO[YOLO26n-seg + ByteTrack]
    API --> Depth[UniDepthV2 · cadence indépendante]
    Operator[Ansible · poste opérateur] -->|SSH via IAP et OS Login| VM[VM GCP]
```

## Déployer

Depuis le dépôt infrastructure :

```bash
tofu -chdir=GCP plan -out=https.tfplan
tofu -chdir=GCP apply https.tfplan
./ansible/deploy.sh /chemin/vers/backend COMMIT_SHA
python3 ansible/provision_pi.py
```

Le playbook attend le bootstrap Docker, installe une archive Git, le `.env` privé et le proxy, puis lance le projet Compose `looki-vision`. Alembic applique les migrations. Il vérifie `/health/ready` localement puis en HTTPS depuis le poste opérateur, avec validation du certificat. Les modèles sont téléchargés au premier démarrage : la vérification de readiness autorise jusqu'à 30 minutes. Les volumes PostgreSQL, modèles et certificats persistent ; ne pas utiliser `down -v` pour terminer une session.

Le profil réserve 4 vCPU et 6 Go au backend, 2 threads par modèle, YOLO à 768 px et une demande de profondeur au plus toutes les 2 s. Le deuxième backend doit avoir un projet Compose et des volumes distincts, avec un autre port local si nécessaire. PostgreSQL n'est pas public. L'API est exposée sur loopback côté hôte ; Caddy la rejoint par le réseau Docker.

Le déploiement initial laisse `LOOKI_REALTIME_ENABLED=false` : la vision ne nécessite aucune clé OpenAI. Activer séparément Lio avec une clé projet privée si nécessaire.

## Connecter les lunettes

`provision_pi.py` crée une session et des jetons de 8 heures par défaut. Il écrit des fichiers privés exclus de Git sous `ansible/.local/pi-<session>/` :

| Fichier | Destinataire | Contenu |
| --- | --- | --- |
| `pi.env` | Raspberry Pi | URL HTTPS, UUID de session, `pi_token` |
| `viewer.json` | Navigateur | Même URL et session, `mobile_token`, expiration |

Transférer uniquement `pi.env` sur le Pi. Depuis le dossier contenant `capture_sender.py` :

```bash
chmod 600 /chemin/pi.env
. /chemin/pi.env
python3 capture_sender.py --source picamera2 \
  --base-url "$LOOKI_BASE_URL" --session-id "$LOOKI_SESSION_ID" \
  --fps 2 --width 960 --height 540 --jpeg-quality 85
```

Synchroniser l'horloge du Pi : les nouvelles captures de plus de 30 s ou de plus de 5 s dans le futur sont refusées. Les réessais conservent UUID, horodatage et octets JPEG. `latest_only=true` évite une file d'analyse qui grossit.

Avec Picamera2 `RGB888`, le tableau est déjà BGR pour OpenCV : une seconde conversion RGB→BGR avant `imencode` inverse rouge et bleu. [Formats Picamera2 officiels](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf).

## Observer depuis le navigateur

Ouvrir la sortie OpenTofu `live_url` (`/live/?mode=pi`), importer `viewer.json`, puis cliquer **Se connecter aux lunettes**. La saisie manuelle de la session et du jeton mobile fonctionne aussi. Le fichier doit correspondre à l'origine HTTPS de la page. Les secrets restent dans la mémoire de l'onglet, sans URL, localStorage ou export de diagnostic.

```mermaid
sequenceDiagram
    participant V as Viewer
    participant B as Backend
    participant P as Pi
    V->>B: GET /sessions/{id}/live · mobile_token
    B-->>V: runtime, session, cursor, 100 événements récents, dernières analyses
    V->>B: POST /sessions/{id}/ws-ticket
    B-->>V: ticket temporaire à usage unique
    V->>B: WSS /sessions/{id}/ws?after=cursor&ticket=...
    P->>B: POST /sessions/{id}/frames · JPEG + pi_token
    B-->>P: 202 accepté
    B-->>V: frame.analyzed / alert.possible_proximity
    B-->>V: frame.depth_analyzed / alert.proximity
    V->>B: GET /sessions/{id}/frames/{frame_id}/image
    B-->>V: JPEG exact de l'analyse
```

Le snapshot est en lecture seule, accessible au mobile/opérateur de cette session. Il capture un état cohérent avec le curseur, puis le WSS reprend après ce curseur. La dernière image et la dernière profondeur restent disponibles même si elles sont antérieures aux 100 événements récents. Le viewer se reconnecte automatiquement et ignore doublons et événements d'une autre session.

Les dimensions de l'image, les masques, pistes et alertes correspondent au même `frame_id`. La profondeur reste attachée à sa capture, consultable séparément. Une bannière signale une image âgée de plus de 3 s. Figer l'affichage ou consulter une alerte ne stoppe pas le Pi. **Déconnecter le viewer ou fermer la page ne ferme jamais la session des lunettes.**

Les statistiques Pi décrivent les résultats reçus, pas la cadence d'émission inconnue du navigateur. Le journal/export conserve au plus 2 000 événements sur cette connexion, avec 100 événements récents au démarrage. L'historique complet reste dans l'API. Quinze captures/s ne garantissent pas quinze analyses/s sur CPU.

## Valider et exploiter

1. Vérifier HTTPS et `/health/ready`, puis les `202` du Pi.
2. Observer un JPEG analysé avec le provider réel et les événements WSS.
3. Tester un rapprochement, consulter motifs visuels et capture de profondeur ; les mètres restent non calibrés.
4. Reconnecter le viewer puis couper brièvement le réseau du Pi. Vérifier la reprise sans changer de session.
5. Augmenter progressivement la cadence en surveillant latence, remplacements, CPU, RAM et disque.

Renouveler les accès avec `python3 ansible/provision_pi.py --session-id UUID`, recopier `pi.env`, réimporter `viewer.json`, reconnecter les clients. Ne pas transmettre la clé opérateur aux appareils.

Les JPEG, même remplacés avant analyse, restent en base. La limite de file ne limite pas le stockage. Fermer les anciennes sessions et utiliser le script de purge documenté, backend arrêté. Le disque et l'IP réservée restent alloués quand la VM est arrêtée.
