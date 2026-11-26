# Session et accès des appareils · Contrat POC v1

**Implémenté le 30 octobre 2026.** Le provisionnement est manuel pour une paire de lunettes. Il complète les contrats [mobile](mobile-backend.md) et [Pi JPEG](raspberry-backend.md). Aucun appairage automatique ou compte utilisateur n’est annoncé livré.

## Créer et partager la même session

Un opérateur de confiance utilise `Authorization: Bearer <LOOKI_API_KEY>` :

1. `POST /api/v1/sessions`, corps `{"center_id":"demo-centre","user_id":"demo-user"}` → `201`, champ `id` UUID. Le contexte du centre est figé ; `user_id` est une donnée métier, pas une preuve d’identité.
2. `POST /api/v1/sessions/{id}/access-tokens` → `200`, corps ci-dessous, `Cache-Control: no-store`.
3. Fournir au mobile et au Pi la même URL et le même UUID, chacun avec **son propre jeton** par un canal de provisionnement de confiance. Aucun transfert applicatif direct Pi/iPhone n’est requis.

```json
{
  "session_id": "11111111-1111-4111-8111-111111111111",
  "mobile_token": "<jeton opaque mobile>",
  "pi_token": "<jeton opaque pi>",
  "token_type": "Bearer",
  "expires_in_seconds": 28800
}
```

Les identifiants des exemples ne servent pas à créer une nouvelle session réelle. La route d’émission exige la clé opérateur ; un jeton d’appareil ne permet pas d’émettre d’autres jetons ni de créer une session.

| Identité technique | Accès |
| --- | --- |
| Opérateur, clé `LOOKI_API_KEY` | Provisionnement et diagnostic de toutes les sessions |
| Jeton `mobile` | Lecture de sa session, état/captures/événements, appels Realtime, questions, lecture, arrêt, ticket WSS, clôture ; pas d’upload caméra |
| Jeton `pi` | Upload et lecture des captures de sa session, lecture de son état ; pas de questions, WSS ou clôture |

Les jetons opaques sont signés, limités à un rôle, une session et une expiration (8 heures par défaut). Modifier le rôle ou la session invalide la signature. Un jeton valide pour une autre session reçoit `403 session_forbidden` ; un jeton invalide ou expiré reçoit `401 unauthenticated`.

Ce sont des **capacités techniques**, pas une authentification nominative des personnes. Le détenteur d’un jeton possède son accès ; garder les jetons dans le stockage sécurisé des appareils. La clé partagée historique devient une clé opérateur : ne pas la distribuer au mobile/Pi ni la mettre dans une URL. Changer cette clé invalide les jetons pour les prochaines requêtes et ouvertures de connexion. Les requêtes HTTP revalident le jeton ; le WSS le vérifie à l’ouverture, sans coupure automatique d’une connexion existante à son expiration. Une révocation individuelle, un renouvellement automatique et un SSO sont hors de cette version.

## Lire et clôturer

- `GET /api/v1/sessions/{id}` → `200`, état `active` ou `closed`, dates et contexte.
- `POST /api/v1/sessions/{id}/close` → `200`, idempotent, réservé au mobile/opérateur.
- La clôture bloque les nouvelles captures, questions et ouvertures d’appel ; les captures déjà admises peuvent finir leur traitement.
- Elle lance aussi l’arrêt Realtime. Si un appel existe, attendre son `control_status: closed` avant de conclure à sa fermeture distante ; l’iPhone coupe et détruit son média local immédiatement.
- Les GET et répétitions exactes déjà admises restent accessibles pendant la validité du jeton. Une clôture ne supprime pas l’historique ni les données PostgreSQL.

## Réseau et secrets

Le viewer Pi utilise le `mobile_token` pour `GET /api/v1/sessions/{id}/live` (snapshot en lecture seule), les tickets WSS et les images de cette session. Le snapshot contient `session`, `runtime`, `cursor`, `recent_events` (100 maximum), `latest_frame` et `latest_depth` (événements ou `null`). Reprendre le WSS après `cursor`. Le Pi n'a pas accès à ce snapshot. Déconnecter le viewer ne ferme pas la session. Le provisionnement de fichiers distincts `pi.env` et `viewer.json` est automatisé dans le dépôt infrastructure : [guide](../docs/23-deploiement-gcp-et-lunettes.md).

Exposer le service derrière HTTPS/WSS pour les appareils physiques. Le Compose local publie sur `127.0.0.1:8000` ; il faut configurer le reverse proxy de la VM. Les bases ne sont pas exposées. La clé OpenAI ne quitte jamais le backend et n’est ni le jeton mobile ni le jeton Pi.

Le provisionnement et l’accès sont testés côté serveur avec plusieurs sessions ; la distribution effective des jetons et la politique de conservation restent à intégrer dans les outils d’équipe.
