# 08 — Test du modèle sur photographies réelles

## Objet du test

Cette procédure vérifie la chaîne réelle `JPEG → API → Depth Anything → proximité → événement WebSocket`. Elle utilise le checkpoint `depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf` sur CPU et des photographies sourcées, sans réponses simulées.

Les images sont des fichiers statiques, pas un flux capturé par les lunettes. Pour cet essai, le client fournit un nouvel UUID et un horodatage courant à chaque soumission ; `captured_at` représente donc l’instant de test, pas la date originale de prise de vue.

Le modèle estime la profondeur dans trois régions fixes gauche, centre et droite. Le seuil proche reste `1.5 m`, le seuil loin `3.0 m` et le score minimal `0.5`. L’alerte attendue dépend des estimations réellement produites : une image qui ne contient aucune région classée `near` ne doit pas recevoir d’alerte de proximité.

Ce test ne dispose pas d’une mesure de référence des distances photographiées. Il permet de vérifier l’exécution du modèle et la cohérence du protocole ; il ne permet pas de calculer une erreur en mètres, une précision de détection ou une garantie d’évitement d’obstacle.

## Résultats du 15 octobre 2026

La campagne exécutée à **16 h 26, heure de Paris**, contre le backend Docker avec PostgreSQL a réussi pour **5 photographies sur 5**. Les cinq analyses sont terminées, toutes déclarent `simulated: false` et aucune violation de contrat n’est relevée. Deux images produisent une alerte, les trois autres n’en produisent pas, conformément aux classements du modèle avec le seuil inchangé de `1.5 m`.

| Photographie | Plus petite distance estimée parmi les régions | Alerte persistée et reçue par WS | Traitement serveur | Envoi HTTP → analyse reçue par WS |
| --- | ---: | --- | ---: | ---: |
| Table et chaises | `1.912 m` | Non | `355.6 ms` | `612.1 ms` |
| Récipients et vaisselle | `2.053 m` | Non | `320.2 ms` | `605.3 ms` |
| Verrerie | `0.636 m` | Oui | `412.1 ms` | `602.6 ms` |
| Hall avec escalier | `6.233 m` | Non | `333.8 ms` | `393.7 ms` |
| Chats sur canapé | `1.363 m` | Oui | `317.8 ms` | `526.0 ms` |

Les deux alertes sont enregistrées dans PostgreSQL et leurs événements reçus par WebSocket correspondent aux événements persistés. Elles portent les identifiants `15` et `18` dans la session `289ea814-5bbe-402d-8eb5-8520b4524ce5`. Les trois régions de chacune des deux photos concernées sont classées `near`, mais une seule alerte est émise par image, comme prévu par le contrat.

Le [rapport HTML](../output/real-depth-evaluation/report.html) permet d’examiner les photos, les régions et les événements. Le [résultat JSON complet](../output/real-depth-evaluation/results.json) conserve les preuves HTTP/WebSocket et les mesures non arrondies. « 5 sur 5 » signifie que les vérifications d’intégration ont réussi ; cela ne signifie pas que les cinq distances estimées sont physiquement exactes.

### Environnement observé

Le relevé [runtime.json](../output/real-depth-evaluation/runtime.json), obtenu dans le conteneur actif, précise la configuration serveur indépendamment des indications du `.env` local :

| Élément | Valeur observée |
| --- | --- |
| Système / architecture | Linux `aarch64` |
| Python | `3.12.15` |
| PyTorch | `2.14.1+cpu`, sans build CUDA |
| Transformers | `4.57.6` |
| Périphérique | CPU, `18` threads PyTorch |
| Checkpoint | `depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf` |
| Révision du cache | `8078d68a9c75a972131914f6afd0c1723be0da7f` |
| Profondeur | `metric`, maximum configuré `20 m` |
| Seuils | Proche `1.5 m`, loin `3.0 m` |
| Fraîcheur maximale d’une capture | `30 s` |

Les temps correspondent à une seule campagne sur cette configuration. Ils ne constituent pas un benchmark statistique ni une mesure caméra → écouteurs. Les cinq photographies sélectionnées ont toutes été traitées ; aucune image n’a été retirée après lecture de sa prédiction.

## Reproduire l’essai

Dans le `.env` à la racine, activer :

```dotenv
LOOKI_VISION_PROVIDER=depth_anything_metric
INSTALL_VISION=true
LOOKI_VISION_DEVICE=cpu
LOOKI_NEAR_THRESHOLD_M=1.5
LOOKI_FAR_THRESHOLD_M=3.0
LOOKI_MIN_CONFIDENCE=0.5
```

Reconstruire et attendre la disponibilité du modèle :

```bash
docker compose up --build -d
curl --fail http://localhost:8000/health/ready
```

La disponibilité doit indiquer `vision_provider: depth_anything_metric`. Le premier démarrage télécharge les poids si le cache est vide. Le guide [d’exploitation](05-installation-et-exploitation.md#activer-la-profondeur-métrique) décrit la construction CPU et les caches.

Depuis la racine, après installation de l’environnement Python local du backend :

```bash
.venv/bin/python scripts/test_real_images.py \
  --base-url http://localhost:8000 \
  --output-dir output/real-depth-evaluation
```

Le backend réalise l’inférence. Le script local sert de client de test HTTP et WebSocket ; il n’a pas besoin de charger un deuxième modèle. Le script `demo.py` continue de cibler le mode mock et ses attentes déterministes, et ne remplace pas cet essai.

La clé est lue dans l’environnement `LOOKI_API_KEY`, ou peut être fournie avec `--api-key`; la valeur par défaut est `looki-local-dev-key`. Le script ne récupère pas le secret dans `.env`. `--images-dir` permet de choisir un autre dossier et `--timeout` règle l’attente, fixée à `120 s` par défaut.

## Corpus et provenance

Les cinq photographies ont été retenues visuellement avant l’inférence. Quatre représentent des scènes intérieures parmi les exemples du dépôt officiel Depth Anything V2 ; la cinquième est une photographie COCO montrant des chats sur un canapé. Les cinq résultats sont conservés, qu’une alerte soit produite ou non.

| Fichier évalué | Scène | Source |
| --- | --- | --- |
| `room-table-chairs.jpg` | Pièce avec table et chaises | [Depth Anything V2 — demo10](https://github.com/DepthAnything/Depth-Anything-V2/blob/main/assets/examples/demo10.jpg) |
| `museum-vessels.jpg` | Vitrine de récipients et vaisselle | [Depth Anything V2 — demo13](https://github.com/DepthAnything/Depth-Anything-V2/blob/main/assets/examples/demo13.jpg) |
| `glassware.jpg` | Verres et verrerie en vitrine | [Depth Anything V2 — demo14](https://github.com/DepthAnything/Depth-Anything-V2/blob/main/assets/examples/demo14.jpg) |
| `staircase-hall.jpg` | Grand hall intérieur avec escalier | [Depth Anything V2 — demo15](https://github.com/DepthAnything/Depth-Anything-V2/blob/main/assets/examples/demo15.jpg) |
| `coco-cats-sofa.jpg` | Deux chats sur un canapé | [Miroir de documentation Hugging Face, image COCO 39769](https://huggingface.co/datasets/huggingface/documentation-images/blob/main/coco_sample.png) |

Les images entières sont converties en RGB, orientées selon leur EXIF et redimensionnées en conservant les proportions, sans recadrage ni agrandissement, avec un bord maximal de `1280` pixels. Elles sont enregistrées en JPEG qualité `95`. Les dimensions, empreintes SHA-256, originaux et détails de transformation sont conservés dans le [manifeste](../fixtures/real-images/manifest.json).

La licence propre à chaque photographie et son auteur original ne sont pas vérifiés ; les champs correspondants sont explicitement nuls dans le manifeste. La licence du code du dépôt ou celle des annotations COCO n’est pas présentée comme une autorisation de réutilisation de ces photos. Les sources sont conservées pour ce test ; leur statut doit être vérifié avant une redistribution publique. Les métadonnées Commons également présentes dans le dossier concernent des sources explorées mais non évaluées.

## Artefacts et lecture des résultats

| Artefact | Usage |
| --- | --- |
| `fixtures/real-images/manifest.json` | Liste des photographies retenues, sources et droits d’utilisation |
| `output/real-depth-evaluation/results.json` | Résultats de la campagne, distances et événements reçus |
| `output/real-depth-evaluation/report.html` | Présentation des photos et des régions observées |
| `output/real-depth-evaluation/runtime.json` | Relevé complémentaire de la configuration et des versions dans le conteneur |

Le JSON de résultats conserve notamment le résultat de disponibilité, les indications de configuration locale, l’identifiant de session et, pour chaque image, le résultat HTTP `frame`, les événements persistés et reçus, les durées et les éventuelles violations du contrat. Les indications `.env` ne prouvent pas seules la configuration du serveur ; le relevé `runtime.json` complète cette vérification pour la campagne ci-dessus et n’est pas généré automatiquement par le client de test. `analysis_latency_ms` mesure le traitement rapporté par le backend ; `http_to_ws_analysis_ms` mesure le délai observé par le client entre l’envoi HTTP et la réception de l’analyse sur le socket. Ces deux mesures n’incluent pas le TTS de l’iPhone.

Dans chaque résultat, vérifier que `provider` vaut `depth_anything_metric`, que `simulated` vaut `false` et que la source des distances est `metric_model`. Le champ `confidence` représente la proportion de pixels de profondeur valides dans la région ; il ne mesure pas l’exactitude de la distance.

Les rectangles superposés au rapport représentent les trois régions d’échantillonnage définies par le provider. Ils ne délimitent pas des objets reconnus. Une distance courte peut provenir du sol, d’une table ou d’une autre surface proche dans la région, sans que le backend sache nommer cette surface.

Le journal doit contenir un `frame.analyzed` pour chaque analyse achevée et un `alert.proximity` pour chaque image dont au moins une région est `near`. Les identifiants et horodatages permettent de rattacher une alerte à la bonne capture. Les images et les résultats restent persistés dans PostgreSQL après le test.

## Portée et prochaine mesure

Les photographies publiques ne reproduisent pas nécessairement la hauteur, l’optique ou l’orientation des lunettes. Leur sélection ne forme pas un échantillon statistique représentatif. Une durée relevée pendant cet essai dépend de la machine, de son état et de la charge du serveur ; elle ne représente pas une latence garantie en usage réel.

Pour évaluer l’utilité du système, refaire la campagne avec la caméra finale et des obstacles dont les distances sont mesurées. Inclure le sol, les murs, les passages libres, les obstacles fins et les changements de lumière. Mesurer séparément l’erreur de distance, les alertes manquées, les alertes injustifiées et la latence complète jusqu’aux écouteurs. Le protocole de cette campagne figure dans le chapitre [vision](04-vision-et-proximite.md#validation-physique-à-réaliser).
