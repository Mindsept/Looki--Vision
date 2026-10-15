# Calibration de la caméra Raspberry Pi

Ce dossier est monté en lecture seule dans `/app/calibration` par Compose.
Il ne contient aucune focale supposée ou calibration inventée.

Avec `LOOKI_VISION_PROVIDER=urban_objects` et `LOOKI_DEPTH_BACKEND=unidepth_v2`,
définir `LOOKI_CAMERA_CALIBRATION_PATH=/app/calibration/camera.json` pour utiliser
une calibration réelle au lieu d'estimer la caméra depuis chaque image.

Le JSON décrit une caméra pinhole **rectifiée** : `width`, `height` (résolution
de calibration), `fx`, `fy`, `cx`, `cy` (pixels à cette résolution). Obtenir ces
valeurs par calibration de la caméra utilisée, avec son objectif et son mode de
capture. Les images doivent être rectifiées avant envoi si l'objectif présente
une distorsion. Ne pas utiliser les paramètres d'une autre photo ou caméra.

Un redimensionnement conservant le rapport largeur/hauteur ajuste la matrice
automatiquement. Un recadrage ou changement de mode de capture demande une
calibration correspondante ; un rapport de dimensions incompatible est refusé.
Le backend conserve dans les métadonnées la matrice utilisée et sa provenance.

La calibration ne garantit pas la justesse du modèle : voir
[les mesures et limites](../../docs/10-metric-depth-comparison.md).
