# 17 — Recette Lio du 6 novembre 2026

Cette campagne valide le backend et son banc navigateur avec **le vrai fournisseur OpenAI**. Le STT reste local sur iPhone ; le banc saisit le texte final pour reproduire son entrée. Elle complète les tests de protocole avec transport contrôlé. Voir le [guide Lio](16-lio-openai.md) et le [contrat mobile v1.3](../contracts/mobile-backend.md).

## Environnement et périmètre

Docker Desktop local, PostgreSQL 16 temporaire, backend limité à **6 CPU / 8 Gio**, trois threads de calcul, un worker Uvicorn. Vision réelle **YOLO26n-seg + ByteTrack + UniDepthV2**, poids déjà présents dans le volume de cache. Aucun résultat de cette page n’est un benchmark de la VM partagée 8 vCPU/32 Go.

Le client est Chrome headless piloté par Playwright, avec autoplay autorisé pour le test. Il négocie un WebRTC `recvonly`, reçoit directement les paquets audio OpenAI et utilise les routes HTTP et WSS LOOKi. Aucun microphone, faux transcript, faux audio ou événement d’alerte injecté côté navigateur. Les JPEG sont des fichiers réels, horodatés au rejeu ; il ne s’agit pas de captures actuelles du Pi.

Modèle **`gpt-realtime-2.1`**, voix `marin`, effort `low`, budget 1024 tokens, instructions **`lio.scene.v1.1`**. La clé est privée côté backend. Le contrôle des métadonnées du modèle a réussi, puis les appels réels ont confirmé la négociation, le sideband et la génération. Mini n’a pas été comparé dans cette campagne.

## Réponses complètes observées

Une exécution par cas ci-dessous : ce sont des observations, pas des percentiles ni des garanties. Le début audio est la réception serveur de `output_audio_buffer.started`, mesurée depuis l’admission HTTP de la question. La fin est celle de la génération observée, **pas celle de la lecture dans les écouteurs**.

| Photo / question | Début audio observé | Fin de génération | Tokens totaux fournis | Résultat relu dans la transcription |
| --- | ---: | ---: | ---: | --- |
| `room-table-chairs.jpg` / décrire devant soi | 1,244 s | 3,515 s | 2024 | Commence par « Vue partielle », décrit fenêtres, arbres et mobilier |
| `parked-bicycles.jpg` / distance des vélos | 1,255 s | 3,826 s | 1863 | Contexte partiel, distance incertaine, aucun mètre inventé |
| `pedestrian-signal.jpg` / traverser sans danger ? | 1,047 s | 4,013 s | 1919 | Contexte partiel, main rouge visible, refus de garantir la sécurité |

Les trois générations ont terminé en `completed`. Le navigateur a reçu respectivement **19 369, 22 219 et 27 763 octets RTP**, avec une énergie audio mesurée positive ; les compteurs de pertes étaient 0, 1 et 0 paquets lors du relevé. Aucun `pageerror` observé. Ces compteurs sont relevés à la fin de génération, pas après une écoute humaine complète. Le flux audio n’a pas été enregistré ni écouté sur iPhone.

Extrait du cas vélos : « la distance des vélos est incertaine, car aucune mesure en mètres n’est disponible ». Le snapshot de cette demande ne contenait pas encore de mesure métrique exploitable ; la génération n’a pas attendu le worker de profondeur.

## Interruption et reprise

Deux essais séparés démarrent une réponse sur la photo de pièce, attendent le début d’émission fournisseur et un échantillon RTP avec énergie positive, puis interrompent :

| Déclencheur | Preuve observée |
| --- | --- |
| Bouton utilisateur | Audio local muet et source détachée ; question `cancelled`, média `closed` ; nouvel appel connecté, `media_epoch` 1 → 2 |
| Alerte vision réelle | Rejeu de `Bicycle1-perfect.jpg` pendant la réponse ; événement `alert.possible_proximity`, `sound_id=v1.bicycle_front` ; même clôture et nouvelle époque |

L’alerte obtenue est « Proximité possible : vélo devant. Vigilance visuelle, distance incertaine. ». Les règles de perception et les seuils n’ont pas été modifiés pour ce test. Le premier essai avec `bus-pedestrians.jpg` n’avait pas produit d’alerte et ne constitue donc pas un test réussi d’interruption. Le vélo occupant une grande partie de l’image permet d’exercer la branche réelle.

La coupure locale est aussi couverte par un test frontend qui vérifie son ordre avant le premier appel réseau. La campagne navigateur confirme l’état local et distant, sans mesurer une latence acoustique de coupure. Le clip de vigilance reste à jouer et arbitrer dans l’app iOS. L’interruption précoce peut laisser usage final et transcription à `null` ; cela ne signifie pas un coût nul.

## Contrôles logiciels

| Contrôle | Résultat |
| --- | --- |
| Suite Python | **369 tests passent** : droits, idempotence, cycle média, snapshots, mesures, limites, nettoyage et vision |
| Suite frontend | **14 tests passent** : coupure, retries, fin de lecture, réponses tardives, export et métriques vision |
| Ruff et build Vite | Passent |
| PostgreSQL / Alembic | Upgrade complet, absence de dérive, downgrade de la nouvelle migration puis upgrade réussis |
| Contrats | OpenAPI exporté, exemple `QuestionRead` validé ; copies synchronisées et épinglées dans le dépôt documentaire |
| Mermaid | Huit sources nouvelles/modifiées rendues avec Mermaid CLI 11.17.1 |

Les essais ont corrigé trois problèmes observés : fermeture HTTP `202` à corps vide mal interprétée par le banc, contexte partiel insuffisamment annoncé par le premier prompt, état local encore `generating` après interruption. Un test supplémentaire protège contre le GET tardif d’une ancienne question qui écraserait l’état de la suivante.

## Reproduire et compléter

API démarrée, depuis la racine du backend :

```sh
uv run --with playwright python scripts/test_lio_browser.py \
  --chrome '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
  --image fixtures/real-images/room-table-chairs.jpg \
  --alert-image fixtures/metric-bicycle-reference/Bicycle1-perfect.jpg \
  --output-dir output/lio-browser-alert
```

Pour l’arrêt manuel, remplacer `--alert-image …` par `--interrupt-after-audio`. Ajouter `--base-url` si le port diffère. Sur Linux, utiliser Chromium installé avec Playwright ou fournir son exécutable. Les appels sont réels et facturables. Le script clôture sa session dans un `finally` ; les données restent en base jusqu’à purge.

Les résultats JSON et captures expurgées sont locaux et ignorés par Git sous `output/lio-browser-*`. Ils contiennent la transcription de la scène de test : ils ne constituent pas une télémétrie de production. Les secrets, SDP et événements bruts ne sont pas publiés. Les conteneurs et la base temporaires de cette campagne sont arrêtés et supprimés après les vérifications ; le cache de modèles existant est conservé.

Restent à effectuer : recette sur iPhone/écouteurs et réseau mobile, STT réel, clips prioritaires, chronométrage du son entendu, scènes avec trois captures et mouvements, texte lisible/flou, instructions visibles hostiles, contradictions de profondeur, ainsi qu’une campagne statistique sur la VM finale. Les tests contrôlés couvrent plusieurs de ces branches de protocole ; ils ne valident pas le comportement sémantique du modèle sur tout ce corpus. Le backend ne filtre pas chaque phrase avant diffusion audio directe.
