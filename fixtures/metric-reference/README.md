# ETH3D — Référence métrique extérieure

Six images réelles sont figées avant les prédictions, avec profondeur de référence issue du scanner laser ETH3D et calibration caméra. La sélection comprend deux scènes ayant des pixels valides à `≤ 1,5 m`, ainsi que quatre scènes sans pixel de référence sous ce seuil. Aucun modèle n’a produit ces références.

| Scène | Dimensions natives | Pixels valides | Pixels à ≤ 1,5 m | Minimum de référence |
| --- | ---: | ---: | ---: | ---: |
| playground_3l | 941 × 490 | 171 079 | 27 185 | 1,090 m |
| forest_1s | 715 × 441 | 122 711 | 4 648 | 0,693 m |
| facade_1s | 707 × 425 | 196 669 | 0 | 1,921 m |
| terrace_2s | 713 × 434 | 160 672 | 0 | 2,449 m |
| electro_1l | 927 × 489 | 369 772 | 0 | 2,725 m |
| playground_1l | 941 × 490 | 216 801 | 0 | 1,614 m |

Les JPEG conservent la grille native des PNG monochromes, répliquée en RGB, sans recadrage ni redimensionnement. `originals/` conserve les deux images stéréo, les paramètres caméra, la disparité PFM et le masque officiels. Le [manifeste](manifest.json) relie les fichiers et leurs SHA-256.

`ground-truth/*_depth.npy` contient une matrice `float32` de profondeur axiale `z` en mètres. `*_valid.npy` contient un masque booléen. Les pixels hors masque valent `NaN` dans la carte de profondeur. Le masque retient `mask0nocc == 255`, une disparité finie et un dénominateur positif. Les pixels `128`, observés uniquement par la vue gauche, sont exclus de cette comparaison conservatrice. [Documentation ETH3D](https://www.eth3d.net/documentation).

La conversion utilise `z_m = (baseline_mm / 1000) × fx_px / (disparity_px + doffs_px)`. Les lignes PFM sont retournées verticalement et le signe du marqueur indique l’ordre des octets. La disparité est déjà en pixels ; aucune normalisation n’est appliquée. [Convention de calibration Middlebury utilisée par ETH3D](https://vision.middlebury.edu/stereo/data/scenes2014/), [format PFM](https://vision.middlebury.edu/stereo/submit3/upload-format.html).

Les sources sont les archives officielles [`two_view_training.7z`](https://www.eth3d.net/data/two_view_training.7z) et [`two_view_training_gt.7z`](https://www.eth3d.net/data/two_view_training_gt.7z), environ `29 Mo` au total. Pour reproduire depuis la racine, avec NumPy, Pillow et `bsdtar` ou `7z` :

```bash
backend/.venv/bin/python backend/scripts/prepare_metric_reference.py \
  --cache-dir /tmp/looki-eth3d-reference
```

Attribution : **Thomas Schöps et al., ETH3D, “A Multi-View Stereo Benchmark with High-Resolution Images and Multi-Camera Videos”, CVPR 2017**. Les données ETH3D sont publiées sous [CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/), comme indiqué sur le [site officiel](https://www.eth3d.net/). Les adaptations locales sont la conversion PNG → JPEG et disparité → profondeur ; elles ne changent pas la grille ni l’échelle physique.

Ce sous-ensemble de diagnostic ne constitue pas le benchmark complet. Les images monochromes, les vues du même terrain de jeux et les surfaces proches sans classe COCO limitent son pouvoir de généralisation. Les résultats proches par pixel et les alertes émises sur des objets détectés sont deux mesures différentes.
