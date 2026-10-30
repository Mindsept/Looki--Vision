# Alertes backend → mobile · Contrat WSS v1.1

**Implémenté le 30 octobre 2026.** Le POC conserve le WebSocket et le journal existants pour les alertes. Les questions audio suivent le [contrat HTTP mobile](mobile-backend.md) ; OpenAI n’intervient ni dans leur détection ni dans leur livraison.

## Abonnement et authentification

Connexion native : `wss://<hôte>/api/v1/sessions/{session_id}/ws?after=0`, avec `Authorization: Bearer <mobile_token>`. Le jeton Pi est refusé. Aucun jeton permanent dans l’URL.

Pour un client navigateur ne pouvant pas fournir ce header, `POST /api/v1/sessions/{session_id}/ws-ticket` authentifié retourne `ticket` et `expires_in_seconds: 30`. Le ticket est lié à la session, à usage unique ; connexion `.../ws?after=<curseur>&ticket=<ticket>`. Masquer les query strings de tickets dans les logs proxy. Les tickets deviennent invalides après redémarrage.

L’état initial `session.state` possède `id: null`. Ensuite les événements durables de la session, d’identifiant strictement supérieur à `after`, arrivent par ordre croissant. Il peut y avoir des trous dans les identifiants. Le socket livre aussi des résultats de perception : le client filtre les types dont il a besoin, mais avance son curseur après chaque événement traité.

Client → backend : `{"type":"ping"}` ; réponse `pong` non persistée, `id: null`. Les questions WSS historiques restent réservées au diagnostic opérateur. Un jeton mobile reçoit `http_question_required` pour ce parcours ancien.

## Événements à vocaliser

| Type | Sens | Champs particuliers |
| --- | --- | --- |
| `alert.possible_proximity` | Indices visuels de proximité ; mètres incertains | `ruleset: urban-visual-v4`, observations et motifs |
| `alert.proximity` | Profondeur estimée sous le seuil | `threshold_m`, `nearest_distance_m`, observations proches |

Les deux conservent `frame_id`, `captured_at`, `provider`, `simulated`, `severity: warning`, `text` et `detections`, et ajoutent **`expires_at`** et **`sound_id`**. Les champs de chaque observation sont décrits dans [le schéma WSS](websocket.schema.json). Les distances peuvent être inconnues ; une vigilance visuelle ne doit pas être annulée parce qu’une estimation métrique est classée loin.

Extrait d’un événement illustratif (les observations complètes sont omises ici) :

```json
{
  "id": 84,
  "type": "alert.possible_proximity",
  "session_id": "11111111-1111-4111-8111-111111111111",
  "timestamp": "2026-10-30T13:00:04.350Z",
  "payload": {
    "frame_id": "22222222-2222-4222-8222-222222222222",
    "captured_at": "2026-10-30T13:00:04.300Z",
    "expires_at": "2026-10-30T13:00:07.300Z",
    "sound_id": "v1.person_front",
    "severity": "warning",
    "text": "Proximité possible : personne devant. Distance incertaine.",
    "provider": "yolo_seg_track",
    "simulated": false,
    "ruleset": "urban-visual-v4"
  }
}
```

L’expiration vaut `captured_at + LOOKI_ALERT_TTL_SECONDS`, **3 secondes par défaut**, et ne repart pas à la réception ni au rejeu. Une alerte déjà expirée peut rester dans le journal, mais ne doit pas être jouée comme danger actuel. Un événement historique sans expiration reste consultable sans vocalisation automatique. Le texte est destiné à l’écran/diagnostic ; le mobile ne doit pas lancer de TTS pour le lire.

## Catalogue de clips v1

| Identifiant | Clip à embarquer côté mobile |
| --- | --- |
| `v1.person_left`, `v1.person_front`, `v1.person_right` | « Attention, personne à gauche / devant / à droite. » |
| `v1.bicycle_left`, `v1.bicycle_front`, `v1.bicycle_right` | « Attention, vélo à gauche / devant / à droite. » |
| `v1.car_left`, `v1.car_front`, `v1.car_right` | « Attention, voiture à gauche / devant / à droite. » |
| `v1.proximity_generic` | « Attention, obstacle à proximité. » |
| `v1.scene_unavailable` | « La scène n’est pas disponible pour le moment. » — repli d’une question HTTP, pas une alerte de proximité |

Si toutes les observations concernées donnent la même classe et direction, le backend choisit le clip spécifique. Sinon il utilise le clip générique. Les classes non couvertes utilisent aussi le générique. Un identifiant inconnu doit tomber sur le générique déjà embarqué. **Le contrat des identifiants est livré ; les fichiers audio restent à produire/intégrer par l’équipe mobile.** Le scénario simulé doit rester clairement identifié.

## Priorité, reprise et interruption

1. Dédupliquer par `id`, vérifier la session et `expires_at`, sans prolonger la fraîcheur.
2. Couper immédiatement la sortie WebRTC locale, puis jouer le clip d’alerte prioritaire ; ne pas attendre une réponse HTTP.
3. Si une demande occupe encore le média, envoyer `POST /questions/{request_id}/cancel` avec `reason: alert`, le `call_id` et son `media_epoch`.
4. Le backend demande cancellation/vidage et ferme l’appel. Dans ce POC, **détruire l’ancienne connexion WebRTC et négocier une nouvelle époque** avant la prochaine question. Aucun vieux son ne doit être repris.
5. Après coupure WSS, reconnecter avec `after=<dernier id traité>` ; garder le maximum du curseur et ignorer les doublons/alertes expirées. Le GET `/events?after=...&limit=100` permet aussi le diagnostic et le rattrapage paginé, sans lecture audio implicite.

Une répétition visuelle sur une même piste est limitée à environ deux secondes tant que son risque persiste. Les alertes métriques restent émises par capture proche. La politique de regroupement audio mobile complète ces règles ; elle doit être vérifiée sur iPhone. Une absence de message, une déconnexion ou une expiration ne prouve pas l’absence d’obstacle.
