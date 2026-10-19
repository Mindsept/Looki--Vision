import React from "react";
import { useLiveVideo } from "./hooks/useLiveVideo.js";
import {
  Play,
  Pause,
  RotateCcw,
  Upload,
  Download,
  ArrowUpRight,
  ChevronDown,
  Radio,
  Scan,
  TriangleAlert,
  Activity,
  Settings2,
  CircleCheck,
  LoaderCircle,
} from "lucide-react";
import {
  percentile,
  resultAge,
  formatTime,
  distanceText,
  objectRisk,
  riskLabel,
  shortTrack,
  reasonText,
  motionLabel,
  filterAlerts,
} from "./metrics.js";
import "./style.css";

const names = {
  person: "Personne",
  bicycle: "Vélo",
  car: "Voiture",
  motorcycle: "Moto",
  bus: "Bus",
  truck: "Camion",
  "traffic light": "Feu",
  "stop sign": "Panneau stop",
  backpack: "Sac à dos",
  handbag: "Sac",
  suitcase: "Valise",
  chair: "Chaise",
  bench: "Banc",
  dog: "Chien",
  umbrella: "Parapluie",
};
const number = (n, digits = 0) =>
  Number.isFinite(n) ? n.toFixed(digits) : "—";
export default function App() {
  const [alertFilter, setAlertFilter] = React.useState("all");
  const {
    video,
    apiKey,
    run,
    view,
    config,
    phase,
    connected,
    source,
    sourceName,
    duration,
    currentTime,
    result,
    lastDepth,
    inspectDepth,
    frameUrl,
    alerts,
    error,
    raw,
    selected,
    settings,
    now,
    setSettings,
    initialize,
    setError,
    setSelected,
    setDuration,
    setCurrentTime,
    changePhase,
    setVideo,
    start,
    pause,
    reset,
    download,
    inspectAlert,
  } = useLiveVideo();
  const filteredAlerts = filterAlerts(alerts, alertFilter);
  const payload = result?.payload,
    objects = payload?.detections ?? [];
  const nearest = objects
    .filter((o) => o.distance_m != null)
    .reduce((a, o) => Math.min(a, o.distance_m), Infinity);
  const near = objects.some((o) => o.proximity === "near");
  const possible = objects.some((o) => objectRisk(o) === "possible");
  const visualOnly =
    config?.decoupled_depth && result?.type !== "frame.depth_analyzed";
  const approaching = objects.some(
    (o) => o.perception?.motion === "approaching",
  );
  const lastTiming = run.current.timings.at(-1);
  const age = payload ? resultAge(payload.captured_at, now) : null;
  const phaseNames = {
    loading: "Connexion au serveur",
    ready: "Prêt à analyser",
    connecting: "Ouverture de session",
    running: "Analyse en cours",
    paused: "Lecture en pause",
    draining: "Dernières images en cours",
    finished: "Analyse terminée",
    error: "Connexion à vérifier",
  };
  const busy = ["loading", "connecting"].includes(phase);
  return (
    <div className="app-shell">
      <header className="topbar">
        <a className="brand" href="/live/" aria-label="LOOKi accueil">
          LOOK<span>i</span>
          <i />
        </a>
        <span className="product-label">
          VISION LAB <span>/</span> 01
        </span>
        <div className="topbar-right">
          <span className={`connection ${connected ? "on" : ""}`}>
            <i />
            {connected
              ? "WebSocket connecté"
              : run.current.session
                ? "WebSocket déconnecté"
                : config
                  ? "Backend disponible"
                  : "Backend hors ligne"}
          </span>
          <button
            className="icon-button"
            aria-label="Paramètres de connexion"
            onClick={() => setSettings(!settings)}
          >
            <Settings2 size={18} />
          </button>
        </div>
      </header>
      <main>
        <section className="heading">
          <div>
            <div className="eyebrow">
              <span className="tiny-line" /> OBSERVER · MESURER · ALERTER
            </div>
            <h1>
              La rue, image par image<span>.</span>
            </h1>
            <p>
              YOLO26 Nano segmente. ByteTrack suit les objets.{" "}
              {config?.depth_backend === "depth_anything"
                ? "Depth Anything"
                : "UniDepth"}{" "}
              estime la profondeur ponctuellement. Personnes, vélos et voitures : les alertes visuelles
              continuent entre les mesures.
            </p>
          </div>
          <div className="hardware">
            <span className="hardware-dot" />
            <div>
              <strong>{config?.cpu_threads ?? 2} threads / modèle</strong>
              <span>Profil VM · 4 vCPU / 32 Go prévus</span>
            </div>
          </div>
        </section>
        {settings && (
          <section className="settings-panel">
            <label>
              Clé API{" "}
              <input
                type="password"
                disabled={run.current.session != null}
                defaultValue={apiKey.current}
                onChange={(e) => {
                  apiKey.current = e.target.value;
                }}
                autoComplete="off"
              />
            </label>
            <button onClick={initialize} disabled={run.current.session != null}>
              Reconnecter
            </button>
            <span>
              La clé reste dans cet onglet. Même origine pour l’API et
              l’interface.
            </span>
          </section>
        )}
        {error && (
          <div className="notice" role="alert">
            <TriangleAlert size={17} />
            <span>{error}</span>
            <button aria-label="Fermer le message" onClick={() => setError("")}>
              ×
            </button>
          </div>
        )}
        <section className="metrics" aria-label="Mesures du flux">
          <Metric
            label="CADENCE CIBLE"
            value="15"
            unit="ips"
            detail="JPEG envoyés depuis la vidéo"
          />
          <Metric
            label="DÉBIT ENVOYÉ"
            value={number(view.inputRate, 1)}
            unit="ips"
            detail={`${view.accepted} images acceptées`}
            accent
          />
          <Metric
            label="DÉBIT ANALYSÉ"
            value={number(view.outputRate, 1)}
            unit="ips"
            detail={`${view.processed} images réellement calculées`}
          />
          <Metric
            label="DERNIÈRE ANALYSE"
            value={number(lastTiming?.total_vision_ms)}
            unit="ms"
            detail={`YOLO ${number(lastTiming?.detection_total_ms)} · suivi ${number(lastTiming?.tracking_risk_ms, 1)} ms`}
          />
          <Metric
            label="ALERTES BACKEND"
            value={view.alerts.toString().padStart(2, "0")}
            unit=""
            detail={`${view.metricAlerts} métriques · ${view.possibleAlerts} proximité possible`}
            warning={view.alerts > 0}
          />
        </section>
        {config?.decoupled_depth && (
          <section
            className="depth-sample-panel"
            aria-label="Profondeur indépendante"
          >
            <div>
              <strong>PROFONDEUR PONCTUELLE</strong>
              <p>
                Au plus une capture toutes les {config.depth_interval_seconds} s
                · {view.depthSamples} calculs terminés · {view.depthFailures}{" "}
                indisponibles
              </p>
            </div>
            <div>
              {lastDepth ? (
                <>
                  <strong>
                    Capture {formatTime(lastDepth.media?.time)} ·{" "}
                    {number(lastDepth.payload.timing_ms?.depth_job_total_ms)} ms
                  </strong>
                  <p>
                    Mesure âgée de{" "}
                    {number(resultAge(lastDepth.payload.captured_at, now), 1)} s
                    · liée à cette capture uniquement
                  </p>
                </>
              ) : (
                <p>
                  Le suivi visuel fonctionne pendant le calcul de profondeur.
                </p>
              )}
            </div>
            <button
              className="secondary"
              disabled={!lastDepth}
              onClick={() => inspectDepth(lastDepth)}
            >
              Voir cette capture
            </button>
          </section>
        )}
        <div className="workspace">
          <section className="viewer-panel" aria-label="Résultat de l’analyse">
            <div className="panel-heading">
              <div className="panel-title">
                <Scan size={16} />
                <h2>Ce que voit LOOKi</h2>
              </div>
              <span
                className={`state-chip ${phase === "running" ? "active" : ""}`}
              >
                {busy ? <LoaderCircle size={12} /> : <i />}
                {phaseNames[phase]}
              </span>
            </div>
            <div
              className={`scene ${near || possible ? "near-scene" : ""}`}
              style={
                result?.media?.width
                  ? {
                      aspectRatio: `${result.media.width} / ${result.media.height}`,
                    }
                  : undefined
              }
            >
              {frameUrl ? (
                <>
                  <img
                    src={frameUrl}
                    alt="Capture exacte associée au résultat affiché"
                  />
                  <svg
                    className="instance-masks"
                    viewBox="0 0 1 1"
                    preserveAspectRatio="none"
                    aria-label="Masques des objets détectés"
                  >
                    {objects.map((o, i) => (
                      <path
                        key={i}
                        className={`${objectRisk(o)} ${selected === i ? "selected" : ""}`}
                        fillRule="evenodd"
                        d={(o.perception?.polygons || [])
                          .map(
                            (poly) =>
                              `M ${poly.map((p) => p.join(" ")).join(" L ")} Z`,
                          )
                          .join(" ")}
                      />
                    ))}
                  </svg>
                  {objects.map((o, i) => (
                    <button
                      key={i}
                      onClick={() => setSelected(i)}
                      aria-label={`${names[o.label] || o.label}, ${distanceText(o.distance_m)}`}
                      className={`detection-box ${objectRisk(o)} ${selected === i ? "selected" : ""}`}
                      style={{
                        left: `${o.bbox[0] * 100}%`,
                        top: `${o.bbox[1] * 100}%`,
                        width: `${(o.bbox[2] - o.bbox[0]) * 100}%`,
                        height: `${(o.bbox[3] - o.bbox[1]) * 100}%`,
                      }}
                    >
                      <span
                        className="box-label"
                        style={
                          o.bbox[2] > 0.85
                            ? { left: "auto", right: -1 }
                            : undefined
                        }
                      >
                        <b>
                          #{i + 1} ·{" "}
                          {visualOnly
                            ? objectRisk(o) === "possible"
                              ? "Vigilance"
                              : motionLabel(o)
                            : distanceText(o.distance_m)}
                          {objectRisk(o) === "possible" ? " ?" : ""}
                        </b>
                        {objectRisk(o) === "possible" && (
                          <small>Proximité possible</small>
                        )}
                      </span>
                    </button>
                  ))}
                </>
              ) : (
                <div className="scene-empty">
                  <Scan size={46} strokeWidth={1} />
                  <h3>Une scène réelle. Des résultats visibles.</h3>
                  <p>
                    Lance l’analyse pour afficher les objets, leur mouvement
                    apparent et les alertes sur l’image traitée.
                  </p>
                  <span>
                    YOLO26n-seg + ByteTrack +{" "}
                    {config?.depth_backend === "depth_anything"
                      ? "Depth Anything"
                      : "UniDepthV2"}{" "}
                    · aucun résultat simulé
                  </span>
                </div>
              )}
              <div className="scene-corner top-left">
                <span className={frameUrl ? "signal-dot" : ""} />
                {frameUrl
                  ? run.current.reviewing
                    ? result?.type === "frame.depth_analyzed"
                      ? "PROFONDEUR CONSULTÉE"
                      : "ALERTE CONSULTÉE"
                    : "IMAGE ANALYSÉE"
                  : "EN ATTENTE"}
              </div>
              {payload && (
                <>
                  <div className="scene-corner top-right">
                    {formatTime(result.media?.time)} · capture vidéo
                  </div>
                  <div className="scene-bottom">
                    <span>{objects.length} objets détectés</span>
                    <span>Résultat âgé de {number(age, 1)} s</span>
                  </div>
                </>
              )}
            </div>
            <div className="viewer-caption">
              <span>
                <i className="legend-dot" /> Objet détecté
              </span>
              <span>
                <i className="legend-dot orange" /> Proche / proximité possible
              </span>
              <span className="alignment-note">
                Masques et suivi sur cette capture · profondeur consultable
                séparément
              </span>
            </div>
            <div className="source-controls">
              <div className="source-preview">
                <video
                  ref={video}
                  src={source || undefined}
                  playsInline
                  muted
                  preload="auto"
                  onLoadedMetadata={() => setDuration(video.current.duration)}
                  onTimeUpdate={() => setCurrentTime(video.current.currentTime)}
                  onEnded={() => {
                    run.current.active = false;
                    changePhase("draining");
                  }}
                />
                <span>SOURCE</span>
              </div>
              <div className="source-main">
                <div className="source-title">
                  <strong>{sourceName}</strong>
                  <span>
                    {formatTime(currentTime)} <em>/ {formatTime(duration)}</em>
                  </span>
                </div>
                <input
                  aria-label="Position dans la vidéo"
                  type="range"
                  min="0"
                  max={duration || 1}
                  step="0.1"
                  value={currentTime}
                  disabled={run.current.session != null}
                  title="Recommencer pour changer de position et réinitialiser le suivi"
                  onChange={(e) => {
                    video.current.currentTime = +e.target.value;
                    setCurrentTime(+e.target.value);
                  }}
                />
                <div className="playback-actions">
                  <button
                    className="primary"
                    onClick={phase === "running" ? pause : start}
                    disabled={
                      !source ||
                      busy ||
                      !["yolo_objects", "yolo_seg_track"].includes(
                        config?.vision_provider,
                      )
                    }
                  >
                    {phase === "running" ? (
                      <Pause size={15} />
                    ) : (
                      <Play size={15} />
                    )}{" "}
                    {phase === "running"
                      ? "Pause"
                      : phase === "paused"
                        ? "Reprendre"
                        : "Lancer l’analyse"}
                  </button>
                  <button className="secondary" onClick={reset} disabled={busy}>
                    <RotateCcw size={15} /> Recommencer
                  </button>
                  <label
                    className={`file-button ${run.current.session ? "disabled" : ""}`}
                  >
                    <Upload size={15} />
                    <span>Autre vidéo</span>
                    <input
                      aria-label="Choisir une vidéo"
                      type="file"
                      accept="video/*"
                      disabled={run.current.session != null}
                      onChange={(e) => {
                        if (e.target.files?.[0])
                          setVideo(e.target.files[0], e.target.files[0].name);
                      }}
                    />
                  </label>
                </div>
              </div>
            </div>
            <div className="queue-note">
              <Activity size={14} />
              <span>
                {view.superseded} images remplacées avant analyse ·{" "}
                {view.pending} résultats attendus · {view.clientSkipped}{" "}
                captures non envoyées · {view.rejected} refus
              </span>
            </div>
          </section>
          <aside className="objects-panel">
            <div className="panel-heading">
              <h2>Objets dans l’image</h2>
              <span className="count-badge">
                {objects.length.toString().padStart(2, "0")}
              </span>
            </div>
            <div className={`nearest ${near || possible ? "warning" : ""}`}>
              <span>
                {visualOnly
                  ? "PROXIMITÉ VISUELLE"
                  : "DISTANCE ESTIMÉE SUR CETTE CAPTURE"}
              </span>
              <strong>
                {visualOnly
                  ? possible
                    ? "Vigilance"
                    : approaching
                      ? "En approche"
                      : "Observation"
                  : distanceText(Number.isFinite(nearest) ? nearest : null)}
              </strong>
              <p>
                {possible
                  ? "Proximité possible · alerte visuelle autonome"
                  : near
                    ? "Au moins un objet sous le seuil de proximité"
                    : objects.length
                      ? "Selon les objets détectés dans cette image"
                      : "En attente d’une première analyse"}
              </p>
            </div>
            <div className="object-list">
              {objects.length ? (
                objects.map((o, i) => (
                  <button
                    key={i}
                    className={`object-row ${selected === i ? "selected" : ""}`}
                    onClick={() => setSelected(i)}
                  >
                    <span className={`object-index ${objectRisk(o)}`}>
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span className="object-name">
                      <strong>{names[o.label] || o.label}</strong>
                      <small>
                        {shortTrack(o)} · YOLO {Math.round(o.confidence * 100)}{" "}
                        % {o.perception?.detection_status === "tentative" && " · à confirmer"}
                      </small>
                    </span>
                    <span className={`object-distance ${objectRisk(o)}`}>
                      {visualOnly
                        ? `${number((o.perception?.mask_area_ratio || 0) * 100, 1)} %`
                        : distanceText(o.distance_m)}
                      <small>{riskLabel(o)}</small>
                    </span>
                  </button>
                ))
              ) : (
                <div className="empty-list">
                  <Radio size={26} />
                  <p>
                    Les objets apparaîtront ici
                    <br />
                    au fil des analyses.
                  </p>
                </div>
              )}
            </div>
            {selected != null && objects[selected]?.perception && (
              <div className="evidence-panel">
                <strong>{riskLabel(objects[selected])}</strong>
                <p>
                  {shortTrack(objects[selected])} ·{" "}
                  {objects[selected].perception.track_observations || 0}{" "}
                  observations
                </p>
                {(objects[selected].perception.reason_codes || []).map(
                  (code) => (
                    <p key={code}>{reasonText(code)}</p>
                  ),
                )}
                <p>
                  Mouvement : {motionLabel(objects[selected])}
                  {objects[selected].perception.visual_profile &&
                    ` · profil ${names[objects[selected].perception.visual_profile] || objects[selected].perception.visual_profile}`}
                </p>
                <small>
                  Largeur : {number((objects[selected].perception.evidence?.bbox_width_ratio || 0) * 100, 1)} % ·{" "}
                  Surface du masque :{" "}
                  {number(
                    (objects[selected].perception.mask_area_ratio || 0) * 100,
                    1,
                  )}{" "}
                  % · taille relative dans l’image
                </small>
              </div>
            )}
            <div className="distance-note">
              <span>À propos des mètres</span>
              <p>
                Profondeur monoculaire estimée
                {config?.camera_calibrated
                  ? " avec calibration fournie"
                  : ", sans calibration de cette caméra"}
                . Le score YOLO ne mesure pas la précision des distances.
              </p>
            </div>
          </aside>
        </div>
        <section className="events-panel">
          <div className="panel-heading">
            <div className="panel-title">
              <Radio size={16} />
              <h2>Le backend prend la parole</h2>
              <span className="event-source">
                Événements persistés · WebSocket
              </span>
            </div>
            <button
              className="text-button"
              onClick={download}
              disabled={!run.current.events.length}
            >
              <Download size={14} /> Exporter la session
            </button>
          </div>
          <div className="alert-filters" role="group" aria-label="Filtrer les alertes par classe">
            {[["all", "Toutes"], ["person", "Personnes"], ["bicycle", "Vélos"], ["car", "Voitures"]].map(([label, title]) => (
              <button key={label} aria-pressed={alertFilter === label} onClick={() => setAlertFilter(label)}>
                {title} · {filterAlerts(alerts, label).length}
              </button>
            ))}
          </div>
          {filteredAlerts.length ? (
            <div className="alert-list">
              {filteredAlerts.slice(0, 6).map((event) => (
                <button
                  key={event.id}
                  className="alert-row"
                  onClick={() => inspectAlert(event)}
                >
                  <span className="alert-icon">
                    <TriangleAlert size={19} />
                  </span>
                  <div>
                    <span className="alert-type">
                      {event.type}{" "}
                      <small>
                        #{event.id} · vidéo {formatTime(event.media?.time)}
                      </small>
                    </span>
                    <strong>{event.payload.text}</strong>
                  </div>
                  <span className="alert-meter">
                    {event.type === "alert.possible_proximity"
                      ? "Distance incertaine"
                      : distanceText(event.payload.nearest_distance_m)}
                    <ArrowUpRight size={16} />
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="events-empty">
              <CircleCheck size={20} />
              <div>
                <strong>{alertFilter === "all" ? "Aucune alerte de proximité reçue" : "Aucune alerte pour cette classe"}</strong>
                <p>
                  Alerte métrique à {config?.near_threshold_m ?? 1.5} m ou
                  moins, ou « proximité possible » selon la taille et
                  l’évolution des silhouettes.
                </p>
              </div>
            </div>
          )}
          <details className="technical">
            <summary>
              Mesures et événement brut <ChevronDown size={14} />
            </summary>
            <div className="technical-grid">
              <div>
                <p>
                  Vision p50 / p95 :{" "}
                  <b>
                    {number(
                      percentile(
                        run.current.timings.map((t) => t.total_vision_ms),
                        0.5,
                      ),
                    )}{" "}
                    /{" "}
                    {number(
                      percentile(
                        run.current.timings.map((t) => t.total_vision_ms),
                        0.95,
                      ),
                    )}{" "}
                    ms
                  </b>
                </p>
                <p>
                  Échecs d’analyse : <b>{view.failed}</b> · Remplacements :{" "}
                  <b>{view.superseded}</b>
                </p>
                <p>
                  Session : <code>{run.current.session || "Non ouverte"}</code>
                </p>
                <p>
                  Échantillonnage demandé : 15 ips. Le débit traité dépend du
                  CPU et des deux modèles.
                </p>
              </div>
              <pre>
                {JSON.stringify(
                  raw ||
                    result || { message: "En attente d’un événement réel" },
                  null,
                  2,
                )}
              </pre>
            </div>
          </details>
        </section>
        <footer>
          <span>
            LOOKi <b>/</b> Vision lab
          </span>
          <p>Images réelles · inférence locale · aucune distance inventée</p>
          <span>POC · {config?.depth_backend || "UniDepthV2"}</span>
        </footer>
      </main>
    </div>
  );
}
function Metric({ label, value, unit, detail, accent, warning }) {
  return (
    <div
      className={`metric ${accent ? "accent" : ""} ${warning ? "warning" : ""}`}
    >
      <span>{label}</span>
      <div>
        <strong>{value}</strong>
        <small>{unit}</small>
      </div>
      <p>{detail}</p>
    </div>
  );
}
