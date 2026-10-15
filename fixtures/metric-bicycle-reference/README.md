# Middlebury Bicycle1 — Référence vélo séparée

Une image réelle couleur de vélo, acquise en intérieur, est conservée avec sa disparité de référence obtenue par lumière structurée et sa calibration. Elle complète les six scènes extérieures ETH3D sans en modifier la sélection. [Dataset officiel](https://vision.middlebury.edu/stereo/data/scenes2014/datasets/Bicycle1-perfect/), [méthode d’acquisition](https://www.cs.middlebury.edu/~schar/papers/datasets-gcpr2014.pdf).

La grille reste native : `2988 × 2008`, sans recadrage ni redimensionnement. Les `5 856 704` pixels valides ont une profondeur comprise entre **2,940 m et 4,084 m**. La médiane sur l’image vaut **3,802 m**. Il n’y a **aucun pixel de référence à ≤ 1,5 m** : cette image vérifie l’échelle du vélo, pas le rappel des alertes proches. Un grand objet dans le cadre ne suffit pas à établir sa distance.

Le [manifeste](manifest.json) décrit les intrinsèques, les sources, les empreintes et les fichiers NPY. La profondeur est un `float32` en mètres selon l’axe `z`; le masque booléen retient les disparités finies, `disparity + doffs > 0` et au moins un échantillon contribuant à la reconstruction (`disp0-n.pgm > 0`). Il s’agit du masque de validité de lumière structurée, distinct du masque ETH3D visible dans les deux vues.

La formule officielle est `z_m = (baseline_mm / 1000) × fx_px / (disparity_px + doffs_px)`. Ici `fx = 5299,313 px`, `baseline = 177,288 mm` et `doffs = 174,186 px`. Le fichier PFM utilise un marqueur `-0.003922` : **seul son signe détermine l’ordre des octets**. L’amplitude n’est pas un facteur à appliquer aux disparités. [Calibration](https://vision.middlebury.edu/stereo/data/scenes2014/), [règle PFM officielle](https://vision.middlebury.edu/stereo/submit3/upload-format.html).

Pour reproduire depuis la racine :

```bash
backend/.venv/bin/python backend/scripts/prepare_bicycle_reference.py \
  --cache-dir /tmp/looki-middlebury-bicycle
```

Attribution : **Daniel Scharstein et al., Middlebury Stereo Datasets, GCPR 2014**. Aucune licence de réutilisation explicite n’a été identifiée sur la page officielle consultée ; le manifeste conserve `license: null`. La disponibilité publique ne constitue pas à elle seule une autorisation générale de redistribution. Les originaux restent disponibles localement avec les transformations documentées.
