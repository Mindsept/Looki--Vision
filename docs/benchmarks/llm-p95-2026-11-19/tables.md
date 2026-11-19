## Résultats mesurés

**1800 demandes mesurées**, 100 par combinaison. 1 échec(s). Les 2 demandes de mise en route sont exclues des percentiles.

Durées en **secondes**. Le p50 porte sur les réponses complètes ; le p95 porte sur toutes les tentatives (échec = +∞).

| JPEG | Images | Realtime premier texte p50 / p95 | Luna premier texte p50 / p95 | Realtime fin p50 / p95 | Luna fin p50 / p95 | Réussites RT / Luna |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 45 | 1 | 0,779 / 1,104 | 1,346 / 3,010 | 1,357 / 1,704 | 2,009 / 3,997 | 100/100 · 100/100 |
| 45 | 3 | 0,937 / 1,349 | 1,479 / 2,699 | 1,554 / 2,004 | 2,263 / 3,563 | 100/100 · 100/100 |
| 45 | 5 | 1,117 / 1,654 | 1,571 / 2,758 | 1,702 / 2,367 | 2,371 / 3,916 | 100/100 · 100/100 |
| 65 | 1 | 0,813 / 1,026 | 1,462 / 2,627 | 1,359 / 1,718 | 2,194 / 3,446 | 100/100 · 100/100 |
| 65 | 3 | 0,957 / 1,358 | 1,488 / 2,481 | 1,550 / 1,979 | 2,160 / 3,375 | 100/100 · 100/100 |
| 65 | 5 | 1,125 / 1,619 | 1,549 / 2,944 | 1,734 / 2,244 | 2,258 / 3,651 | 99/100 · 100/100 |
| 85 | 1 | 0,807 / 1,154 | 1,502 / 2,637 | 1,391 / 1,926 | 2,155 / 3,562 | 100/100 · 100/100 |
| 85 | 3 | 1,013 / 1,663 | 1,597 / 2,980 | 1,607 / 2,237 | 2,335 / 3,750 | 100/100 · 100/100 |
| 85 | 5 | 1,201 / 1,630 | 1,576 / 2,878 | 1,823 / 2,231 | 2,366 / 3,601 | 100/100 · 100/100 |

### Taille des entrées

Moyenne par demande, somme des JPEG avant base64, en **kio** (1 024 octets). Les deux modèles reçoivent les mêmes contenus. Le base64 ajoute environ un tiers, puis viennent le JSON et les instructions.

| Qualité | 1 image | 3 images | 5 images |
| --- | ---: | ---: | ---: |
| 45 | 32,0 | 95,7 | 160,5 |
| 65 | 41,6 | 124,7 | 209,4 |
| 85 | 64,7 | 194,4 | 326,2 |

### Cache et consommation

Tokens agrégés sur les seules demandes mesurées. Les tokens de sortie incluent le raisonnement compté par le fournisseur. Les compteurs de cache documentent une campagne répétée ; ils ne prouvent pas un démarrage à froid.

| Modèle | Entrée | Entrée en cache | Sortie | Demandes avec usage |
| --- | ---: | ---: | ---: | ---: |
| `gpt-realtime-2.1` | 1629422 | 1478035 | 82427 | 899/900 |
| `gpt-6-luna` | 1789050 | 1731504 | 43909 | 900/900 |

Enveloppe tarifaire prudente calculée : **10.383 USD** pour les 1802 appels de cette campagne, mise en route incluse, essai préliminaire exclu. Usage/coût manquant pour 1 demande(s). Ce montant sans remise de cache n’est pas une facture OpenAI.
