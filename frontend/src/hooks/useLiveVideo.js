import { useEffect, useRef, useState } from "react";
import { TARGET_FPS, emptyCounts, windowRate } from "../metrics.js";

export function useLiveVideo() {
  const video = useRef(null),
    canvas = useRef(document.createElement("canvas"));
  const apiKey = useRef("looki-local-dev-key");
  const run = useRef({
    active: false,
    reviewing: false,
    session: null,
    socket: null,
    cursor: 0,
    inflight: false,
    pending: new Set(),
    depthPending: new Set(),
    metadata: new Map(),
    events: [],
    sentTimes: [],
    processedTimes: [],
    timings: [],
    started: 0,
    nextMedia: 0,
    reconnect: null,
    displayGeneration: 0,
  });
  const counts = useRef(emptyCounts());
  const [view, setView] = useState({
    ...emptyCounts(),
    inputRate: 0,
    outputRate: 0,
    pending: 0,
  });
  const [config, setConfig] = useState(null),
    [phase, setPhase] = useState("loading"),
    [connected, setConnected] = useState(false);
  const [source, setSource] = useState(null),
    [sourceName, setSourceName] = useState("Vidéo locale"),
    [duration, setDuration] = useState(0),
    [currentTime, setCurrentTime] = useState(0);
  const [lastDepth, setLastDepth] = useState(null);
  const [result, setResult] = useState(null),
    [frameUrl, setFrameUrl] = useState(null),
    [alerts, setAlerts] = useState([]),
    [error, setError] = useState(""),
    [raw, setRaw] = useState(null),
    [selected, setSelected] = useState(null),
    [settings, setSettings] = useState(false),
    [now, setNow] = useState(Date.now());
  const sourceUrlRef = useRef(null),
    frameUrlRef = useRef(null),
    captureCallback = useRef(null),
    mounted = useRef(true),
    phaseRef = useRef("loading");
  function changePhase(value) {
    phaseRef.current = value;
    setPhase(value);
  }
  async function api(path, options = {}) {
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      headers: {
        Authorization: `Bearer ${apiKey.current}`,
        ...options.headers,
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(
        body.error?.message || body.detail || `HTTP ${response.status}`,
      );
    }
    return response;
  }
  function setVideo(blob, name) {
    if (sourceUrlRef.current) URL.revokeObjectURL(sourceUrlRef.current);
    const url = URL.createObjectURL(blob);
    sourceUrlRef.current = url;
    setSource(url);
    setSourceName(name);
    setCurrentTime(0);
  }
  async function initialize() {
    setError("");
    changePhase("loading");
    try {
      const cfg = await (await api("/runtime")).json();
      setConfig(cfg);
      if (cfg.demo_video_available && !sourceUrlRef.current)
        setVideo(
          await (await api("/demo-video")).blob(),
          "New York · 8th Avenue",
        );
      changePhase("ready");
      if (!["yolo_objects", "yolo_seg_track"].includes(cfg.vision_provider))
        setError(
          `Le serveur utilise ${cfg.vision_provider}. Activer yolo_seg_track pour cette analyse réelle.`,
        );
    } catch (e) {
      setError(e.message);
      changePhase("error");
    }
  }
  async function showFrame(event, sid) {
    const generation = ++run.current.displayGeneration;
    try {
      const blob = await (
        await api(`/sessions/${sid}/frames/${event.payload.frame_id}/image`)
      ).blob();
      if (
        !mounted.current ||
        run.current.session !== sid ||
        generation !== run.current.displayGeneration
      )
        return;
      const url = URL.createObjectURL(blob),
        previous = frameUrlRef.current;
      frameUrlRef.current = url;
      setFrameUrl(url);
      setResult({
        ...event,
        media: run.current.metadata.get(event.payload.frame_id),
      });
      setSelected(null);
      if (previous) URL.revokeObjectURL(previous);
    } catch (e) {
      if (mounted.current)
        setError(`Image analysée indisponible : ${e.message}`);
    }
  }
  function handleEvent(event, sid) {
    const r = run.current;
    if (r.session !== sid || event.id == null || event.id <= r.cursor) return;
    r.cursor = event.id;
    r.events.push(event);
    const type = event.type,
      payload = event.payload;
    if (type === "frame.analyzed") {
      r.pending.delete(payload.frame_id);
      if (
        payload.simulated !== false ||
        !["yolo_objects", "yolo_seg_track"].includes(payload.provider)
      ) {
        setError("Résultat simulé ou provider inattendu : analyse arrêtée.");
        pause();
        return;
      }
      if (payload.metadata?.depth_sample_status === "scheduled")
        r.depthPending.add(payload.frame_id);
      counts.current.processed++;
      r.processedTimes.push(performance.now());
      r.timings.push(payload.timing_ms);
      if (!r.reviewing) void showFrame(event, sid);
    } else if (type === "frame.depth_analyzed") {
      r.depthPending.delete(payload.frame_id);
      counts.current.depthSamples++;
      setLastDepth({ ...event, media: r.metadata.get(payload.frame_id) });
    } else if (type === "depth.failed") {
      r.depthPending.delete(payload.frame_id);
      counts.current.depthFailures++;
    } else if (type === "analysis.failed") {
      r.pending.delete(payload.frame_id);
      if (payload.code === "superseded_frame") counts.current.superseded++;
      else {
        counts.current.failed++;
        setError(`${payload.code} : ${payload.text}`);
      }
    } else if (["alert.proximity", "alert.possible_proximity"].includes(type)) {
      counts.current.alerts++;
      if (type === "alert.possible_proximity") counts.current.possibleAlerts++;
      else counts.current.metricAlerts++;
      setAlerts((previous) =>
        [
          { ...event, media: r.metadata.get(payload.frame_id) },
          ...previous,
        ].slice(0, 100),
      );
    }
  }
  async function inspectDepth(event) {
    if (!event) return;
    pause();
    run.current.reviewing = true;
    setRaw(event);
    await showFrame(event, run.current.session);
  }
  async function inspectAlert(event) {
    const type =
      event.type === "alert.proximity" && config?.decoupled_depth
        ? "frame.depth_analyzed"
        : "frame.analyzed";
    const analyzed = run.current.events.find(
      (e) => e.type === type && e.payload.frame_id === event.payload.frame_id,
    );
    if (!analyzed) return;
    await inspectDepth(analyzed);
    setRaw(event);
  }
  function scheduleReconnect(sid) {
    clearTimeout(run.current.reconnect);
    run.current.reconnect = setTimeout(() => {
      if (!mounted.current || run.current.session !== sid) return;
      connectSocket(sid).catch((e) => {
        if (!mounted.current || run.current.session !== sid) return;
        setError(`Reconnexion en cours : ${e.message}`);
        scheduleReconnect(sid);
      });
    }, 1500);
  }
  async function connectSocket(sid) {
    clearTimeout(run.current.reconnect);
    const { ticket } = await (
      await api(`/sessions/${sid}/ws-ticket`, { method: "POST" })
    ).json();
    if (run.current.session !== sid) return;
    const url = `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/v1/sessions/${sid}/ws?after=${run.current.cursor}&ticket=${encodeURIComponent(ticket)}`;
    await new Promise((resolve, reject) => {
      const ws = new WebSocket(url);
      run.current.socket = ws;
      let opened = false;
      ws.onopen = () => {
        opened = true;
        setConnected(true);
        setError("");
        resolve();
      };
      ws.onmessage = ({ data }) => {
        try {
          handleEvent(JSON.parse(data), sid);
        } catch (e) {
          setError(`Événement invalide : ${e.message}`);
        }
      };
      ws.onerror = () => {
        if (!opened) reject(new Error("Connexion WebSocket impossible"));
      };
      ws.onclose = () => {
        if (
          !mounted.current ||
          run.current.session !== sid ||
          run.current.socket !== ws
        )
          return;
        setConnected(false);
        if (!opened) reject(new Error("Connexion WebSocket refusée"));
        if (run.current.active) {
          pause();
          setError(
            "Connexion interrompue. Reprise du journal en cours ; relancer la lecture une fois connecté.",
          );
        }
        scheduleReconnect(sid);
      };
    });
  }
  async function closeSession() {
    const r = run.current,
      sid = r.session;
    r.active = false;
    r.session = null;
    clearTimeout(r.reconnect);
    r.socket?.close();
    r.socket = null;
    setConnected(false);
    if (sid) await api(`/sessions/${sid}/close`, { method: "POST" });
  }
  async function start() {
    if (
      !source ||
      !["yolo_objects", "yolo_seg_track"].includes(config?.vision_provider) ||
      phaseRef.current === "connecting"
    )
      return;
    setError("");
    run.current.reviewing = false;
    changePhase("connecting");
    try {
      if (!run.current.session || video.current.ended || phase === "finished") {
        await closeSession();
        const session = await (
          await api("/sessions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              center_id: "demo-centre",
              user_id: "live-video-review",
            }),
          })
        ).json();
        const r = run.current;
        r.session = session.id;
        r.cursor = 0;
        r.pending.clear();
        r.depthPending.clear();
        setLastDepth(null);
        r.metadata.clear();
        r.events = [];
        r.sentTimes = [];
        r.processedTimes = [];
        r.timings = [];
        r.started = performance.now();
        counts.current = emptyCounts();
        setAlerts([]);
        setResult(null);
        setFrameUrl(null);
        setRaw(null);
        if (video.current.ended) video.current.currentTime = 0;
        await connectSocket(session.id);
      } else if (run.current.socket?.readyState !== WebSocket.OPEN)
        await connectSocket(run.current.session);
      run.current.active = true;
      run.current.nextMedia = video.current.currentTime;
      await video.current.play();
      changePhase("running");
    } catch (e) {
      run.current.active = false;
      setError(e.message);
      changePhase("error");
    }
  }
  function pause() {
    run.current.active = false;
    video.current?.pause();
    changePhase("paused");
  }
  async function reset() {
    pause();
    run.current.reviewing = false;
    setError("");
    try {
      await closeSession();
      video.current.currentTime = 0;
      setResult(null);
      setFrameUrl(null);
      setAlerts([]);
      setRaw(null);
      counts.current = emptyCounts();
      run.current.pending.clear();
      run.current.depthPending.clear();
      setLastDepth(null);
      run.current.metadata.clear();
      run.current.events = [];
      run.current.cursor = 0;
      run.current.displayGeneration++;
      run.current.sentTimes = [];
      run.current.processedTimes = [];
      run.current.timings = [];
      changePhase("ready");
    } catch (e) {
      setError(e.message);
    }
  }
  async function capture(mediaTime) {
    const r = run.current,
      sid = r.session;
    if (!r.active || !sid) return;
    if (r.inflight) {
      counts.current.clientSkipped++;
      return;
    }
    r.inflight = true;
    const id = crypto.randomUUID(),
      timestamp = new Date().toISOString();
    const v = video.current,
      c = canvas.current;
    c.width = 960;
    c.height = Math.round((960 * v.videoHeight) / v.videoWidth);
    c.getContext("2d", { alpha: false }).drawImage(v, 0, 0, c.width, c.height);
    try {
      const blob = await new Promise((resolve) =>
        c.toBlob(resolve, "image/jpeg", 0.82),
      );
      if (!blob) throw new Error("Capture JPEG impossible");
      if (run.current.session !== sid || !r.active) return;
      const body = new FormData();
      body.set("image", blob, "frame.jpg");
      body.set("frame_id", id);
      body.set("timestamp", timestamp);
      body.set("latest_only", "true");
      r.metadata.set(id, {
        time: mediaTime,
        sent: performance.now(),
        width: c.width,
        height: c.height,
      });
      r.pending.add(id);
      counts.current.sent++;
      r.sentTimes.push(performance.now());
      await api(`/sessions/${sid}/frames`, { method: "POST", body });
      if (run.current.session === sid) counts.current.accepted++;
    } catch (e) {
      r.pending.delete(id);
      if (run.current.session === sid) {
        counts.current.rejected++;
        setError(`Envoi refusé : ${e.message}`);
      }
    } finally {
      r.inflight = false;
    }
  }
  function download() {
    const r = run.current;
    const data = {
      exported_at: new Date().toISOString(),
      session_id: r.session,
      source: sourceName,
      target_fps: TARGET_FPS,
      runtime: config,
      counts: counts.current,
      timing_samples: r.timings,
      captures: Object.fromEntries(r.metadata),
      events: r.events,
    };
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "looki-live-results.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  useEffect(() => {
    mounted.current = true;
    void initialize();
    const ticker = setInterval(() => {
      const r = run.current,
        t = performance.now();
      setNow(Date.now());
      r.sentTimes = r.sentTimes.filter((v) => v > t - 5000);
      r.processedTimes = r.processedTimes.filter((v) => v > t - 5000);
      setView({
        ...counts.current,
        inputRate: windowRate(r.sentTimes, t, r.started),
        outputRate: windowRate(r.processedTimes, t, r.started),
        pending: r.pending.size,
        depthPending: r.depthPending.size,
      });
      if (
        phaseRef.current === "draining" &&
        !r.pending.size &&
        !r.depthPending.size &&
        !r.inflight
      )
        changePhase("finished");
    }, 200);
    const visibility = () => {
      if (document.hidden && run.current.active) pause();
    };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      mounted.current = false;
      clearInterval(ticker);
      document.removeEventListener("visibilitychange", visibility);
      const r = run.current;
      r.active = false;
      r.session = null;
      clearTimeout(r.reconnect);
      r.socket?.close();
      if (sourceUrlRef.current) URL.revokeObjectURL(sourceUrlRef.current);
      if (frameUrlRef.current) URL.revokeObjectURL(frameUrlRef.current);
    };
  }, []);
  useEffect(() => {
    const v = video.current;
    if (!v) return;
    const tick = (_, metadata) => {
      const time = metadata?.mediaTime ?? v.currentTime;
      if (
        run.current.active &&
        !v.paused &&
        time + 1e-5 >= run.current.nextMedia
      ) {
        run.current.nextMedia = Math.max(
          run.current.nextMedia + 1 / TARGET_FPS,
          time + 1 / TARGET_FPS - 0.008,
        );
        void capture(time);
      }
      captureCallback.current = v.requestVideoFrameCallback
        ? v.requestVideoFrameCallback(tick)
        : requestAnimationFrame(tick);
    };
    captureCallback.current = v.requestVideoFrameCallback
      ? v.requestVideoFrameCallback(tick)
      : requestAnimationFrame(tick);
    return () => {
      if (v.cancelVideoFrameCallback)
        v.cancelVideoFrameCallback(captureCallback.current);
      else cancelAnimationFrame(captureCallback.current);
    };
  }, [source]);

  return {
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
    setRaw,
    inspectAlert,
  };
}
