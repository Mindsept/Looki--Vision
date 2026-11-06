# Mobile ↔ backend · Contrat v1.3

**Extension backend du 6 novembre 2026, compatible avec les requêtes v1.2.** Ce document remplace la proposition v1 et constitue le contrat d’intégration pour Leandro et Hamza. Les tests automatisés exercent l’API, la base et un transport fournisseur contrôlé. La recette sur le véritable service OpenAI et celle sur iPhone sont des preuves distinctes ; les tests unitaires ne prouvent pas le parcours matériel. Le STT reste local sur iPhone.

## Responsabilités

L’iPhone réalise le STT local, envoie le texte final en HTTPS à LOOKi et reçoit la réponse vocale native directement d’OpenAI en WebRTC. LOOKi crée l’appel fournisseur, attache un WSS sideband au **même appel**, sélectionne les images et déclenche la génération. Aucun octet audio ne transite par l’API LOOKi. Aucun TTS iOS ne doit relire la transcription de réponse.

Le microphone n’est pas envoyé au fournisseur. Le data channel WebRTC est réservé à l’observation des événements techniques : aucune question ni commande de génération autonome. Les [alertes](alerts.md) utilisent un WSS LOOKi distinct, sans dépendre de Realtime.

## Conventions et accès

- Préfixe des routes ci-dessous : `/api/v1/sessions/{session_id}`. JSON UTF-8 ; dates UTC RFC 3339 ; identifiants LOOKi au format UUID.
- `Authorization: Bearer <mobile_token>`, obtenu par le [provisionnement de session](session-access.md). Ce token n’autorise que sa session. Le POC utilise des capacités signées, pas encore une authentification de comptes iOS.
- `call_id` est un UUID LOOKi opaque. L’identifiant fournisseur reste serveur. `media_epoch` est un entier croissant par nouvelle connexion, attribué par le backend.
- `request_id` est créé par le mobile, séparément pour la création d’appel et pour chaque question. Le conserver jusqu’à résolution d’un timeout.
- Une seule demande peut occuper la sortie audio, même après la fin de génération. Une seule réplique backend et un seul processus Uvicorn sont supportés.
- L’[OpenAPI exécutable](openapi.json) décrit tous les champs, contraintes et réponses. Aucun secret OpenAI n’est remis à l’app.

## Routes

| Route | Succès et rôle |
| --- | --- |
| `GET /assistant` | `200` : configuration publique de Lio, sans appel fournisseur ni secret |
| `POST /realtime/calls` | `201 connecting` à la création ; `200` pour la même offre déjà enregistrée |
| `GET /realtime/calls/current` | `200` : retrouver l’appel non clos après perte de réponse ; `404` si aucun |
| `GET /realtime/calls/{call_id}` | `200` : contrôle, époque et réponse SDP lorsqu’elle est prête |
| `DELETE /realtime/calls/{call_id}` | `202` : fermeture en cours ; `204` : déjà fermée |
| `POST /questions` | `202` : nouvelle demande durable ; `200` : retransmission identique |
| `GET /questions/{request_id}` | `200` : état courant, sans génération ni relecture |
| `PUT /questions/{request_id}/playback` | `200` : snapshot de lecture conservé |
| `POST /questions/{request_id}/cancel` | `202` : arrêt demandé ; `200` : média déjà clos |

## 1. Négocier un appel idempotent

L’app crée un transceiver audio **recvonly**, sans piste micro ni vidéo. L’offre doit contenir `v=0`, une ligne d’origine `o=` et une section audio active explicitement `a=recvonly`. Les offres actives `sendrecv`/`sendonly` sont refusées.

```json
{
  "request_id": "11111111-1111-4111-8111-111111111111",
  "sdp_offer": "<offre SDP complète générée par le SDK>"
}
```

Le POST enregistre l’appel **avant** de contacter OpenAI. Il renvoie immédiatement un `call_id`, une `media_epoch`, `control_status: connecting` et `sdp_answer: null`. L’app consulte le GET jusqu’à `ready`, applique alors `sdp_answer`, puis attend aussi sa propre connexion audio opérationnelle. `ready` signifie que le sideband serveur est attaché et configuré ; il ne prouve pas la disponibilité des écouteurs.

États : `connecting`, `ready`, `closing`, `failed`, `closed`. `sdp_answer` n’est exposée que pour `ready`. Les GET retournent aussi `created_at`, le `request_id` de création et une erreur éventuelle. Une fermeture échouée reste `failed` et bloque un nouvel appel jusqu’au nettoyage confirmé ; le backend retente la fermeture.

Après timeout de création, retrouver `/current` ou répéter **le même UUID et la même offre** : aucun second appel n’est créé. Une offre différente avec le même UUID produit `409 request_conflict`. Un autre UUID tant qu’un appel est actif produit `409 call_active`. Ne pas régénérer automatiquement l’offre SDP avant d’avoir résolu l’état de l’ancienne.

## 2. Envoyer la question finale

Préparer la lecture et associer localement l’audio à la demande **avant** le POST. L’audio peut arriver avant la réponse HTTP.

```json
{
  "request_id": "33333333-3333-4333-8333-333333333333",
  "call_id": "22222222-2222-4222-8222-222222222222",
  "media_epoch": 1,
  "text": "Décris ce qu’il y a devant moi.",
  "playback_ready": true
}
```

Le texte est final, non vide, limité à 2 000 caractères ; les espaces de bord sont retirés. `playback_ready` doit être `true`. Le serveur vérifie session, appel, époque, sideband et absence de réponse encore audible. Il fige les trois dernières captures **analysées** disponibles avant la réception, âgées de trois secondes maximum. Une ou deux captures donnent un contexte partiel ; zéro donne `unavailable`, sans appel de génération, avec `sound_id: v1.scene_unavailable`.

Le snapshot associe chaque image à ses propres objets et mesures. Une profondeur absente ne bloque pas ; des mètres d’une autre capture ne sont jamais empruntés. Images et JSON sont figés ensemble. La fraîcheur est revérifiée juste avant l’envoi fournisseur ; les nouvelles captures ne modifient pas une demande déjà acceptée. Les images réellement sélectionnées sont transmises en JPEG (qualité 85/100) redimensionné à 768 pixels maximum, avec conservation des proportions.

Exemple de réponse acceptée :

```json
{
  "session_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "request_id": "33333333-3333-4333-8333-333333333333",
  "call_id": "22222222-2222-4222-8222-222222222222",
  "media_epoch": 1,
  "status": "accepted",
  "context_id": "44444444-4444-4444-8444-444444444444",
  "transcript": null,
  "error": null,
  "playback": {"sequence": 0, "state": "ready", "position_ms": null, "measurement": "unavailable"},
  "media_status": "reserved",
  "updated_at": "2026-10-30T13:00:00Z",
  "sound_id": null
}
```

## 3. Suivre génération et lecture séparément

| `status` | Sens |
| --- | --- |
| `accepted` | Demande persistée et snapshot sélectionné |
| `generating` | Envoi de génération engagé ; aucun renvoi automatique après résultat incertain |
| `completed` | Génération terminée ; l’audio peut encore être en lecture |
| `cancelled` | Génération annulée avant sa fin |
| `failed` | Échec, perte de contrôle, redémarrage ou délai dépassé |
| `unavailable` | Pas de scène exploitable ; aucune génération |

`context_id` peut être renseigné dès `accepted`. Il reste `null` si aucune sélection n’a été possible. Les états terminaux ne sont pas réouverts. `transcript`, si reçue du fournisseur, sert à l’écran et à l’historique, jamais à lancer un TTS.

`media_status` vaut `reserved`, `playing`, `stopping` ou `closed`. Une nouvelle question est refusée avec `409 answer_active` tant que l’ancienne n’est pas `closed`. Un GET ne déclenche et ne rejoue jamais de voix.

## 4. Rapporter la lecture

```json
{
  "call_id": "22222222-2222-4222-8222-222222222222",
  "media_epoch": 1,
  "sequence": 1,
  "state": "playing",
  "position_ms": 240,
  "measurement": "estimated"
}
```

Envoyer ce corps par PUT. `state` : `playing`, `finished`, `interrupted`. `sequence` : entier croissant à partir de 1 par question. `position_ms` est un entier positif ou nul avec `measured`/`estimated`, ou `null` avec `unavailable`. Ne pas présenter une estimation comme une mesure du matériel.

Un snapshot ancien est ignoré ; même numéro et mêmes champs = idempotent ; même numéro avec contenu différent = `409 playback_conflict`. La réponse contient le snapshot conservé. Une lecture terminée ne revient jamais à `playing`. Annoncer `finished` seulement après **génération terminée et sortie locale vidée** ; avant `completed`, le backend refuse. Une interruption déclenche aussi la fermeture distante ; l’app envoie néanmoins la commande d’annulation explicite avec son motif.

## 5. Annuler, gérer une alerte et changer d’époque

Couper/muter immédiatement la sortie locale, sans attendre le réseau, puis POST `/cancel` :

```json
{"call_id":"22222222-2222-4222-8222-222222222222","media_epoch":1,"reason":"alert"}
```

Motifs : `user_stop`, `new_question`, `alert`, `audio_interruption`, `background`, `connection_lost`.

Le backend demande `response.cancel` si nécessaire, vide la sortie fournisseur et **ferme systématiquement l’appel dans cette version du POC**. Cette politique établit une nouvelle frontière média sans supposer que le SDK a vidé tous ses tampons. Elle s’applique aussi à une génération `completed` encore audible : son statut reste `completed`, mais son média passe par `stopping` puis `closed`.

L’app détruit l’ancien `RTCPeerConnection`, attend la clôture distante, puis négocie un nouvel appel et une nouvelle `media_epoch`. Les commandes tardives d’une ancienne époque ne doivent jamais agir sur la nouvelle. Les annulations répétées sont idempotentes. Après une lecture normale `finished`, l’appel reste réutilisable dans la limite de sa durée.

## Reprise et erreurs

La clé d’une question est `(session_id, request_id)`. Même texte normalisé, appel et époque : retour `200` sans seconde génération. Un de ces champs différent : `409 request_conflict`. Après timeout du POST, consulter le GET puis, si `404`, retransmettre exactement la même demande lorsque l’appel est toujours prêt. Un nouveau UUID signifierait une nouvelle question.

Au redémarrage, les appels non clos deviennent `failed`, les demandes incertaines ne sont pas rejouées et les appels fournisseur connus sont nettoyés. Une coupure réseau pendant la création fournisseur peut empêcher de recevoir son identifiant : aucune génération n’est alors déclenchée par LOOKi, mais la suppression immédiate de cet appel inconnu ne peut pas être garantie. Le contrat ne promet pas un « exactly once » à travers une panne fournisseur.

```json
{"error":{"code":"call_not_ready","message":"Contrôle serveur non prêt.","retryable":true,"request_id":null}}
```

| HTTP | Codes / action |
| --- | --- |
| `401`, `403` | Token absent, invalide, expiré ou hors session/rôle ; rétablir le bon accès |
| `404` | `session_not_found`, `call_not_found`, `request_not_found` |
| `409` | `request_conflict`, `call_active`, `call_not_ready`, `stale_media_epoch`, `answer_active`, `playback_conflict`, `session_closed` ; résoudre l’état avant une nouvelle demande |
| `429` | `question_rate_limited` ; espacer les nouvelles demandes, conserver l’UUID lors du réessai |
| `422` | `invalid_request`, `invalid_sdp` ; corriger les champs ou la taille |
| `503` | `provider_unavailable` ; configuration/clé absente ou service indisponible |

Les échecs survenus après un POST accepté sont consultables dans `error` de la ressource. `retryable: true` n’autorise jamais à créer automatiquement un nouvel UUID. Les détails et secrets du fournisseur sont masqués. Les nouvelles questions d’une session sont espacées de 2 secondes par défaut (`LOOKI_REALTIME_QUESTION_INTERVAL_SECONDS`). La date de la précédente admission est conservée en SQL, y compris pour une scène indisponible. Les retries exacts sont résolus avant cette limite ; les demandes encore audibles restent refusées avec `answer_active`. Ce contrôle ne remplace pas un limiteur général HTTP.

## Configuration et intégration restante

Valeurs initiales configurables : connexion fournisseur 20 s ; génération 30 s à partir de l’admission ; attente de fin de lecture 60 s après génération ; appel 900 s depuis création ; scène 3 s ; offre SDP 65 536 caractères. Le contrôle périodique est approximativement à la seconde. Polling mobile conseillé : 250–500 ms pendant une transition, puis espacer. Ces valeurs ne sont pas des garanties de latence.

Le serveur nécessite `LOOKI_REALTIME_ENABLED=true` et `LOOKI_OPENAI_API_KEY` dans son environnement privé. Modèle `gpt-realtime-2.1`, voix `marin`, effort `low`, budget de 1024 tokens, configurables ; sortie audio seule, détection vocale automatique désactivée, contexte indépendant par demande (`conversation: none`). Sans clé, la vision et les alertes continuent ; les routes de création vocale répondent `503`.

Restent côté app/équipe : SDK WebRTC natif, STT, arbitrage audio et clips locaux, distribution des tokens, test avec écouteurs et vraies interruptions réseau. Côté déploiement : TLS et politique de conservation/purge des images et demandes. Aucune rétention automatique n’est livrée ; les données restent en PostgreSQL jusqu’à purge administrative.


## Extension Lio v1.3 : configuration et observations

`GET /assistant` est autorisé au mobile et à l’opérateur de la session, interdit au Pi. Il expose `configuration_status` (`disabled`, `missing_key`, `configured`), `input_mode: local_stt_text`, `model`, `voice`, `prompt_version`, `reasoning_effort`, `max_output_tokens`, `scene_max_age_seconds`, `max_scene_images: 3` et `question_interval_seconds`. `configured` signifie uniquement que l’activation et une clé sont présentes ; cela ne garantit ni l’accès au modèle, ni un sideband prêt, ni l’audio. Ce GET ne crée pas d’appel.

Les réponses POST/GET de question ajoutent deux champs, sans modifier les requêtes existantes :

| Champ | Contenu et sens |
| --- | --- |
| `scene` | Résumé du snapshot : `frame_ids`, `frame_count`, `partial`, `simulated`, `oldest_capture_at`, `newest_capture_at`. `null` si aucun snapshot. Les identifiants permettent la consultation authentifiée des captures de la même session. |
| `generation` | Modèle/voix/version d’instructions/effort/budget figés à l’admission, puis dates observées et usage. `null` pour les anciennes demandes. Présent ne signifie pas qu’un appel génératif a eu lieu. |

Dates de `generation` : `requested_at` (admission), `submitted_at` (intention d’envoi persistée avant I/O), `provider_created_at` (réception de `response.created`), `provider_audio_started_at` (réception éventuelle du début d’émission), `finished_at` (réception de la fin de génération). Elles sont mesurées par le backend ; les dates non observées restent `null`. Aucun de ces champs ne mesure le premier son entendu par la personne. Une intention d’envoi n’en prouve pas la réception par OpenAI.

`provider_status` vaut `completed`, `cancelled`, `failed`, `incomplete`, ou `null`. `usage` contient les compteurs disponibles `total_tokens`, `input_tokens`, `output_tokens`, `input_text_tokens`, `input_image_tokens`, `input_audio_tokens`, `input_cached_tokens`, `cached_text_tokens`, `cached_image_tokens`, `cached_audio_tokens`, `output_text_tokens` et `output_audio_tokens`. Les valeurs inconnues restent `null`. Le cache est une partie de l’entrée, pas un supplément à additionner au total.

Si le fournisseur termine avec `incomplete`, LOOKi expose `status: failed`, `error.code: generation_incomplete`, conserve les compteurs reçus et ferme l’appel. L’app coupe le média local et ne présente pas l’audio partiel comme une réponse complète. Le timeout ou le code `retryable` n’autorise jamais un rejeu automatique de la génération.

Exemple complet : [question Lio v1.3](examples/lio-question.json). Les consommateurs doivent tolérer les champs de réponse ajoutés ; les corps de requête restent stricts.

## Instructions et frontières

Instructions serveur versionnées `lio.scene.v1.1` : descriptions courtes, lecture du texte lisible, contexte partiel/simulé explicite, aucune distance inventée, aucune garantie de passage sûr. Une affiche est une observation, jamais une instruction système. Aucun outil d’action ou historique de scène n’est activé. Le backend ne relit pas chaque phrase avant l’audio direct : ces instructions et la recette ne constituent pas un filtre sémantique garanti avant diffusion.

Configuration supplémentaire : `LOOKI_REALTIME_REASONING_EFFORT=low`, `LOOKI_REALTIME_MAX_OUTPUT_TOKENS=1024`, `LOOKI_REALTIME_QUESTION_INTERVAL_SECONDS=2`. Le modèle est une API cloud ; la VM conserve les modèles de perception locaux. Le STT local et l’absence de piste micro fournisseur sont maintenus.
