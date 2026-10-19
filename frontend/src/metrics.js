export const TARGET_FPS = 15;
export const emptyCounts = () => ({
  sent: 0,
  accepted: 0,
  processed: 0,
  superseded: 0,
  rejected: 0,
  clientSkipped: 0,
  failed: 0,
  alerts: 0,
  depthSamples: 0,
  depthFailures: 0,
  metricAlerts: 0,
  possibleAlerts: 0,
});
export function percentile(values, p) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = (sorted.length - 1) * p;
  return (
    sorted[Math.floor(index)] +
    (sorted[Math.ceil(index)] - sorted[Math.floor(index)]) * (index % 1)
  );
}
export function windowRate(times, now, started) {
  const duration = Math.min(5, (now - started) / 1000);
  return duration > 0
    ? times.filter((t) => t > now - 5000).length / duration
    : 0;
}
export function resultAge(captured, now) {
  return Math.max(0, (now - Date.parse(captured)) / 1000);
}
export const formatTime = (n) =>
  `${Math.floor((n || 0) / 60)
    .toString()
    .padStart(2, "0")}:${Math.floor((n || 0) % 60)
    .toString()
    .padStart(2, "0")}`;
export const distanceText = (value) =>
  Number.isFinite(value) ? `${value.toFixed(2)} m` : "Inconnue";

export const objectRisk = (object) =>
  ["possible", "approaching"].includes(object.perception?.risk)
    ? object.perception.risk
    : object.proximity;
export const riskLabel = (object) =>
  ({
    possible: "Proximité possible",
    approaching: "En approche",
    near: "Proche (estimé)",
    medium: "Intermédiaire (estimé)",
    far: "Loin (estimé)",
    unknown:
      object.perception?.motion === "receding"
        ? "Éloignement probable"
        : "Distance non mesurée",
  })[objectRisk(object)];
export const shortTrack = (object) =>
  object.perception?.track_id?.split(":").slice(-2).join(" #") ||
  "Suivi en attente";
export const reasonText = (code) =>
  ({
    large_object_mask: "Objet imposant selon le profil de sa classe",
    large_persistent_object: "Objet durablement très présent, même sans mouvement",
    growing_object_mask: "Taille apparente et surface en croissance",
    recent_proximity_hold:
      "Vigilance conservée brièvement après le rapprochement",
    partial_visibility:
      "Visibilité partielle : le rétrécissement ne prouve pas l’éloignement",
    large_persistent_person:
      "Silhouette durablement très présente, même sans mouvement",
    strong_mask_at_edge:
      "Silhouette très présente et coupée par le bord : vigilance immédiate",
    large_person_mask: "Personne imposante dans l’image",
    persistent_track: "Observée sur plusieurs images",
    mask_touches_image_edge: "Silhouette au bord de l’image",
    growing_person_mask: "Silhouette en croissance",
    depth_visual_disagreement:
      "Indices visuels en désaccord avec la profondeur",
    depth_unavailable: "Profondeur indisponible",
  })[code] || code;

export const motionLabel = (object) =>
  ({
    approaching: "En approche",
    receding: "Éloignement probable",
    stable: "Taille stable",
    unknown: "Tendance incertaine",
  })[object.perception?.motion] || "Suivi en cours";


export const filterAlerts = (alerts, label) =>
  label === "all" ? alerts : alerts.filter((event) =>
    event.payload.detections?.some((object) => object.label === label));
