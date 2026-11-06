import React from "react";
import { AssistantClient, exportQuestion } from "./assistantClient.js";
import "./assistant.css";

export default function AssistantLab() {
  const audio = React.useRef(null);
  const client = React.useRef(null);
  const [session, setSession] = React.useState("");
  const [token, setToken] = React.useState("");
  const [text, setText] = React.useState("Décris ce qu’il y a devant moi.");
  const [state, setState] = React.useState({});
  const [busy, setBusy] = React.useState(false);
  const [records, setRecords] = React.useState([]);
  const [pictures, setPictures] = React.useState([]);
  const [piToken, setPiToken] = React.useState("");
  const [capture, setCapture] = React.useState(null);
  const run = async (fn) => {
    setBusy(true);
    setState((s) => ({ ...s, error: null }));
    try { await fn(); } catch (e) { setState((s) => ({ ...s, error: e.message })); }
    finally { setBusy(false); }
  };
  React.useEffect(() => {
    const visibility = () => { if (document.hidden) void client.current?.stop("background").catch(() => {}); };
    document.addEventListener("visibilitychange", visibility);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      void client.current?.dispose().catch(() => {});
    };
  }, []);
  const ids = state.question?.scene?.frame_ids?.join(",") || "";
  React.useEffect(() => {
    let alive = true;
    const urls = [];
    setPictures([]);
    (async () => {
      if (!ids) return;
      for (const frame of ids.split(",")) {
        const response = await fetch(`/api/v1/sessions/${session}/frames/${frame}/image`, {
          headers: { Authorization: `Bearer ${token}` }, cache: "no-store",
        });
        if (!response.ok) continue;
        const url = URL.createObjectURL(await response.blob());
        if (!alive) { URL.revokeObjectURL(url); return; }
        urls.push(url);
        setPictures([...urls]);
      }
    })().catch(() => {});
    return () => { alive = false; urls.forEach(URL.revokeObjectURL); };
  }, [ids, session, token]);
  React.useEffect(() => {
    const q = state.question;
    if (!q || !["completed", "failed", "cancelled", "unavailable"].includes(q.status)) return;
    setRecords((previous) => [...previous.filter((r) => r.request_id !== q.request_id), exportQuestion(q)]);
  }, [state.question]);
  const connect = () => run(async () => {
    if (!client.current) {
      client.current = new AssistantClient(audio.current, setState);
    }
    if (!client.current.attached) {
      await client.current.attach(session, token);
    }
    await client.current.open();
  });
  const replayCapture = async () => {
    if (!capture) return;
    if (!piToken) throw new Error("Le rejeu d’image nécessite le jeton Pi de cette session.");
    const id = crypto.randomUUID();
    const form = new FormData();
    form.set("frame_id", id);
    form.set("timestamp", new Date().toISOString());
    form.set("latest_only", "true");
    form.set("image", capture, "replay.jpg");
    const response = await fetch(`/api/v1/sessions/${session}/frames`, {
      method: "POST", headers: { Authorization: `Bearer ${piToken}` }, body: form,
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error(`Capture refusée : HTTP ${response.status}.`);
    const end = Date.now() + 20000;
    while (Date.now() < end) {
      const frame = await client.current.api(`/frames/${id}`);
      if (frame.status === "completed") return;
      if (frame.status === "failed") throw new Error("Analyse de la capture échouée.");
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    throw new Error("Analyse non terminée. Aucun appel génératif déclenché.");
  };
  const ask = () => run(async () => { await replayCapture(); await client.current.ask(text); });
  const exportRun = () => {
    const blob = new Blob([JSON.stringify({ schema_version: "looki.lio-eval.v1", measurements: records }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = "lio-measurements.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <main className="assistant-lab">
    <header><a href="/live/">← Vision lab</a><span>LOOKi / Lio · diagnostic réel</span></header>
    <h1>Une scène. Une question. Une réponse vocale.</h1>
    <p>Texte saisi pour reproduire la sortie du STT local iPhone. Aucun microphone envoyé depuis ce banc. La voix est générée par OpenAI.</p>
    <audio ref={audio} autoPlay />
    <section aria-label="Connexion">
      <h2>Session et audio</h2>
      <label>Session LOOKi<input value={session} onChange={(e) => setSession(e.target.value)} disabled={!!client.current} placeholder="UUID de session" /></label>
      <label>Jeton mobile<input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} disabled={!!client.current} /></label>
      <p>Jeton conservé en mémoire uniquement. La clé OpenAI reste dans l’environnement du backend.</p>
      <div className="actions">
        <button disabled={busy || !session || !token || state.connection === "connected"} onClick={connect}>Préparer l’audio</button>
        <button disabled={busy || !client.current} onClick={() => run(() => client.current.resolveCall())}>Retrouver l’appel</button>
        <button onClick={() => run(() => client.current?.stop())} disabled={!client.current}>Arrêter / fermer l’appel</button>
      </div>
      <p role="status">Contrôle : {state.call?.control_status || "non ouvert"} · Audio : {state.connection || "non connecté"} · Alertes : {state.alertsConnected ? "connectées" : "déconnectées"}</p>
      {state.config && <p>{state.config.model} · voix {state.config.voice} · {state.config.prompt_version} · {state.config.configuration_status}</p>}
    </section>
    <section aria-label="Question">
      <h2>Interroger la scène</h2>
      <details><summary>Rejouer un fichier JPEG réel pour cet essai</summary>
        <p>Option de diagnostic : le fichier est horodaté au rejeu. Ce test ne mesure pas la fraîcheur d’une caméra physique. Sans fichier, utiliser le flux Pi de la session.</p>
        <label>Jeton Pi<input type="password" autoComplete="off" value={piToken} onChange={(e) => setPiToken(e.target.value)} /></label>
        <input type="file" accept="image/jpeg" onChange={(e) => setCapture(e.target.files[0] || null)} />
      </details>
      <label htmlFor="lio-question">Question finale</label>
      <textarea id="lio-question" maxLength={2000} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="actions">
        <button disabled={busy || state.connection !== "connected" || !text.trim()} onClick={ask}>Poser la question</button>
        <button disabled={busy || !client.current?.pending} onClick={() => run(() => client.current.resolveQuestion())}>Retrouver la demande</button>
        <button disabled={busy || !client.current?.pending} onClick={() => run(() => client.current.sendPending())}>Répéter exactement le POST</button>
      </div>
      <p>Un timeout ne crée jamais un nouvel UUID. Retrouver d’abord la demande, puis répéter le POST identique si elle est absente.</p>
      <div className="pictures">{pictures.map((url, i) => <figure key={url}><img src={url} alt={`Capture ${i + 1} sélectionnée pour cette question`} /><figcaption>Capture {i + 1}</figcaption></figure>)}</div>
      {state.question && <>
        <p>Génération : <strong>{state.question.status}</strong> · Média : {state.question.media_status}</p>
        {state.question.error && <p role="alert">{state.question.error.message} ({state.question.error.code})</p>}
        <p>{state.question.scene?.partial && "Contexte partiel. "}{state.question.scene?.simulated && "Perception simulée. "}{state.question.sound_id && `Retour local demandé : ${state.question.sound_id}`}</p>
        <blockquote>{state.question.transcript || "Transcription non disponible."}</blockquote>
        <button disabled={busy || state.question.status !== "completed" || !state.providerDrained} onClick={() => run(() => client.current.finish())}>Je confirme que la lecture est terminée</button>
        <p>Confirmation manuelle du banc après écoute. L’événement de fin d’émission fournisseur ne prouve pas que les écouteurs ont terminé.</p>
      </>}
    </section>
    {state.error && <p role="alert" className="error">{state.error}</p>}
    {state.alert && <section className="warning"><h2>Alerte backend</h2><p>{state.alert.payload.text}</p><code>{state.alert.payload.sound_id}</code><p>Le banc coupe la réponse. Les clips vocaux préenregistrés restent à intégrer et vérifier sur l’iPhone.</p></section>}
    <section><h2>Mesures et recette</h2>
      <p>Début d’émission observé côté navigateur : {Number.isFinite(state.providerStartedMs) ? `${Math.round(state.providerStartedMs)} ms` : "non disponible"}. Cette valeur ne mesure pas le premier son entendu.</p>
      <pre>{JSON.stringify(state.question?.generation || {}, null, 2)}</pre>
      <button onClick={exportRun} disabled={!records.length}>Exporter les mesures ({records.length})</button>
      <p>L’export contient les identifiants, états et mesures, sans texte, transcription, images, SDP ni jetons. Annoter séparément les erreurs de contenu après écoute.</p>
    </section>
  </main>;
}
