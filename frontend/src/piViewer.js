export function parsePiCredentials(text, origin) {
  const value = JSON.parse(text);
  if (new URL(value.backend_url).origin !== origin)
    throw new Error("Ce fichier appartient à un autre backend. Ouvrir son URL HTTPS.");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.session_id))
    throw new Error("Identifiant de session invalide.");
  if (typeof value.mobile_token !== "string" || !value.mobile_token.startsWith("looki1."))
    throw new Error("Le fichier doit contenir le jeton mobile, pas le jeton Pi.");
  if (value.expires_at && Date.parse(value.expires_at) <= Date.now())
    throw new Error("Jeton expiré : renouveler les accès à cette session.");
  return value;
}

export const realAnalysis = (event) => event?.payload?.simulated === false &&
  ["yolo_objects", "yolo_seg_track"].includes(event.payload.provider);
