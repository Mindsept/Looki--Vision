# Schémas Mermaid du backend LOOKi

Ces fichiers `.mmd` contiennent directement du Mermaid, sans balises Markdown. Ils décrivent le code présent dans ce dépôt ; les chapitres liés expliquent les contraintes et les choix. Ouvrir un fichier dans un éditeur ou visualiseur compatible Mermaid pour le rendre, le modifier ou l’exporter.

| Fichier | Type | Contenu et lecture associée |
| --- | --- | --- |
| [01 — Architecture](01-architecture.mmd) | Composants | Pi, iPhone, backend, modèles, PostgreSQL et centres ; [frontières](../01-architecture.md) |
| [02 — Couches](02-couches.mmd) | Dépendances | Contrôleurs, schémas, services, repositories et providers ; [responsabilités](../01-architecture.md#organisation-par-responsabilité) |
| [03 — Classes métier](03-classes-services.mmd) | Classes | Composition du Runtime, services et repositories ; [cycle de vie](../01-architecture.md#classes-et-composition-du-runtime) |
| [04 — Base de données](04-base-de-donnees.mmd) | Entités / relations | Les cinq tables métier, colonnes, clés et cardinalités ; [dictionnaire complet](../02-base-de-donnees.md) |
| [05 — Classes vision](05-classes-vision.mmd) | Classes | Contrat VisionProvider, résultats, modèles et calibration ; [providers](../04-vision-et-proximite.md) |
| [06 — Ingestion et alerte](06-ingestion-et-alerte.mmd) | Séquence | Upload, admission, worker, transactions et livraison mobile ; [protocole](../03-contrats-et-flux.md) |
| [07 — Questions vocales](07-questions-websocket.mmd) | Séquence | Diagnostic opérateur historique, question WS, idempotence et réponse déterministe ; [questions](../03-contrats-et-flux.md#questions-et-idempotence) |
| [08 — Reconnexion](08-reconnexion-websocket.mmd) | Séquence | Curseur, rejeu après rupture, événements au moins une fois ; [reprise](../03-contrats-et-flux.md#connexion-et-reprise-websocket) |
| [09 — Cycle d’une image](09-cycle-image.mmd) | États | pending, processing, completed, failed et reprise après interruption ; [persistance](../02-base-de-donnees.md) |
| [10 — Vision urbaine](10-vision-urbaine.mmd) | Flux de données | Profondeur et objets, fusion et provenance des estimations ; [vision](../04-vision-et-proximite.md) |
| [11 — Règle de proximité](11-proximite.mmd) | Décision | Distance/score exploitables, seuils inclusifs, alerte par image ; [règles](../04-vision-et-proximite.md) |
| [12 — Compose](12-deploiement-compose.mmd) | Déploiement | Build local, migrations, conteneurs, volumes, calibration et ports ; [exploitation](../05-installation-et-exploitation.md) |
| [13 — Vidéo live](13-video-live.mmd) | Flux de données | Capture à 15 ips, file bornée, YOLO26n, UniDepth et interface ; [mesures CPU](../11-video-live-yolo.md) |
| [14 — Segmentation et suivi](14-segmentation-suivi.mmd) | Décision | Masques réels, ByteTrack, proximité possible et indices ; [règles et tests](../12-segmentation-suivi.md) |
| [15 — Profondeur indépendante](15-profondeur-independante.mmd) | Flux de données | Suivi rapide, file durable de profondeur et consultation de la capture source ; [architecture actuelle](../13-profondeur-independante.md) |
| [16 — Proximité urbaine](16-proximite-urbaine.mmd) | Décision | Profils par classe, suivi des observations faibles et alerte ; [règles v4](../14-proximite-urbaine.md) |

## Conventions importantes

- Les classes correspondent aux classes Python ; les contrôleurs FastAPI sont des modules avec des fonctions de routes, pas des classes artificielles.
- Dans l’ERD, `||` signifie exactement un, `o|` zéro ou un et `o{` zéro à plusieurs. Les clés étrangères utilisent `frames.id`, distinct du `frame_id` transmis par le Pi. Les clés étrangères sont en suppression `CASCADE`. Les unicités composées, index et contraintes sont détaillés dans le chapitre BDD.
- `center_id` et `user_id` sont des identifiants externes. Le contexte est un snapshot JSON ; aucune table centres/utilisateurs n’est créée ici. Les questions audio sont conservées dans `audio_questions`, les appels dans `realtime_calls` et le diagnostic historique dans `events`. Alembic ajoute sa table technique `alembic_version`, absente de l’ERD métier.
- Les schémas de flux expriment les dépendances des données. Les calculs restent séquentiels pour `urban_objects` et `yolo_objects`. En `yolo_seg_track`, le suivi et la profondeur sont indépendants par défaut ; les schémas 13/14 décrivent la version synchrone historique, le schéma 15 le flux courant.
- Compose expose HTTP/WS sur localhost. HTTPS/WSS et l’accès distant demandent une configuration de déploiement supplémentaire.
- Le STT appartient à l’iPhone, la voix de réponse vient directement de Realtime et les alertes sont des clips locaux. Le diagnostic opérateur WSS reste déterministe. Une estimation monoculaire n’est pas une mesure garantie.

## Maintenir les schémas

Vérifier les entités contre `app/models/entities.py` et les migrations, les classes contre `app/services/`, `app/repositories/` et `app/vision/`, les messages contre `contracts/`, et le déploiement contre `compose.yaml` et `Dockerfile`.

Lors d’un changement de code, mettre à jour le `.mmd` concerné et les explications ou schémas intégrés au chapitre correspondant. Les commandes de tous les guides supposent la racine Git `backend/`, où se trouvent aussi `README.md`, `compose.yaml` et `.env.example`.

- [17 — Cycle Realtime](17-mobile-realtime.mmd) et [18 — Classes Realtime](18-classes-realtime.mmd) : [chapitre 15](../15-mobile-realtime-et-pi.md).

- [20 — Génération Lio](20-lio-generation.mmd) et [21 — Mesures](21-lio-mesures.mmd) : [configuration et recette OpenAI](../16-lio-openai.md).
