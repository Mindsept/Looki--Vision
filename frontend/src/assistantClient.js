// Diagnostic client: HTTP commands only, observe-only Realtime data channel.
export const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function freshAlert(event, session, after, now = Date.now()) {
  return event.session_id === session && Number.isInteger(event.id) && event.id > after
    && event.type?.startsWith("alert.") && Date.parse(event.payload?.expires_at) > now;
}

export function exportQuestion(question) {
  // Diagnostics exclude question text, transcript, SDP, tokens and provider identifiers.
  const { request_id, call_id, media_epoch, status, media_status, scene, generation, error } = question;
  return { request_id, call_id, media_epoch, status, media_status, scene, generation, error_code: error?.code ?? null };
}

export class AssistantClient {
  constructor(audio, changed, fetcher = globalThis.fetch.bind(globalThis)) {
    this.audio = audio;
    this.changed = changed;
    this.fetcher = fetcher;
    this.cursor = 0;
    this.state = {};
    this.disposed = false;
    this.sequence = 0;
  }
  update(values) { this.state = { ...this.state, ...values }; this.changed(this.state); }
  async api(path, method = "GET", body) {
    const response = await this.fetcher(`/api/v1/sessions/${this.session}${path}`, {
      method, headers: { Authorization: `Bearer ${this.token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(10000), cache: "no-store",
    });
    if (response.status === 204) return null;
    const content = await response.text();
    const result = content ? JSON.parse(content) : null;
    if (!response.ok) {
      const error = new Error(result?.error?.message || `HTTP ${response.status}`);
      error.status = response.status;
      error.code = result?.error?.code;
      throw error;
    }
    return result;
  }
  async attach(session, token) {
    if (!/^[0-9a-f-]{36}$/i.test(session)) throw new Error("Identifiant de session invalide.");
    this.session = session;
    this.token = token;
    const config = await this.api("/assistant");
    this.config = config;
    this.update({ config });
    if (config.configuration_status !== "configured") throw new Error(`Service vocal : ${config.configuration_status}.`);
    await this.alertSocket();
    this.attached = true;
    // Existing calls must be resolved before generating a new SDP.
    try {
      const current = await this.api("/realtime/calls/current");
      this.call = current;
      this.update({ call: current });
      throw new Error("Un appel existe déjà. Fermer cet appel avant de préparer une nouvelle connexion.");
    } catch (e) { if (e.status !== 404) throw e; }
  }
  async alertSocket() {
    const { ticket } = await this.api("/ws-ticket", "POST");
    const url = new URL(`/api/v1/sessions/${this.session}/ws`, location.origin);
    url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("ticket", ticket);
    url.searchParams.set("after", this.cursor);
    const ws = new WebSocket(url);
    this.alerts = ws;
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { ws.close(); reject(new Error("Canal d’alertes indisponible.")); }, 10000);
      ws.onopen = () => { clearTimeout(timeout); this.update({ alertsConnected: true }); resolve(); };
      ws.onerror = () => { clearTimeout(timeout); reject(new Error("Connexion d’alertes impossible.")); };
      ws.onmessage = ({ data }) => {
        const event = JSON.parse(data);
        if (event.session_id !== this.session) return;
        const fresh = freshAlert(event, this.session, this.cursor);
        if (Number.isInteger(event.id)) this.cursor = Math.max(this.cursor, event.id);
        if (fresh) {
          this.update({ alert: event });
          if (this.pending && this.state.question?.media_status !== "closed") {
            void this.stop("alert").catch((e) => this.update({ error: e.message }));
          }
        }
      };
      ws.onclose = () => {
        clearTimeout(timeout);
        this.update({ alertsConnected: false });
        if (!this.disposed) {
          void this.stop("connection_lost").catch((e) => this.update({ error: e.message }));
          this.reconnect = setTimeout(() => this.alertSocket().catch((e) => this.update({ error: e.message })), 1000);
        }
      };
    });
  }
  async open() {
    if (this.config?.configuration_status !== "configured" || !this.state.alertsConnected) throw new Error("Configuration ou canal d’alertes indisponible.");
    if (this.offer || this.call) throw new Error("Résoudre ou fermer l’appel précédent avant de recréer une offre.");
    const pc = new RTCPeerConnection();
    this.pc = pc;
    this.audio.muted = false;
    pc.addTransceiver("audio", { direction: "recvonly" });
    const channel = pc.createDataChannel("oai-events");
    channel.onmessage = ({ data }) => {
      const event = JSON.parse(data);
      if (pc !== this.pc || !this.pending) return;
      if (event.type === "response.created") {
        if (event.response?.metadata?.request_id !== this.pending.request_id) {
          void this.stop("connection_lost").catch((e) => this.update({ error: e.message }));
          return;
        }
        this.responseId = event.response.id;
      }
      if (event.response_id !== this.responseId) return;
      if (event.type === "output_audio_buffer.started") {
        this.update({ providerStartedMs: performance.now() - this.requestStarted });
      }
      if (event.type === "output_audio_buffer.stopped") this.update({ providerDrained: true });
    };
    // No data-channel send: only LOOKi's authenticated HTTP API commands generation.
    pc.ontrack = ({ streams, track }) => {
      if (pc !== this.pc) return;
      this.audio.srcObject = streams[0] || new MediaStream([track]);
      this.audio.play().catch(() => {
        this.update({ error: "Lecture audio bloquée par le navigateur. Repréparer l’appel par un clic." });
        void this.stop("audio_interruption").catch(() => {});
      });
    };
    pc.onconnectionstatechange = () => {
      if (pc !== this.pc) return;
      this.update({ connection: pc.connectionState });
      if (["failed", "disconnected"].includes(pc.connectionState)) {
        void this.stop("connection_lost").catch((e) => this.update({ error: e.message }));
      }
    };
    await pc.setLocalDescription(await pc.createOffer());
    this.offer = { request_id: crypto.randomUUID(), sdp_offer: pc.localDescription.sdp };
    try {
      this.call = await this.api("/realtime/calls", "POST", this.offer);
      this.update({ call: this.call });
      if (pc !== this.pc) { await this.closeRemote("user_stop"); return; }
      await this.resolveCall();
    } catch (e) {
      this.update({ error: `${e.message} Retrouver l’appel ou le fermer avant un nouvel essai.` });
      throw e;
    }
  }
  async resolveCall() {
    if (!this.call) this.call = await this.api("/realtime/calls/current");
    if (!this.offer || this.call.request_id !== this.offer.request_id || !this.pc) {
      this.update({ call: this.call });
      throw new Error("Cet appel ne possède plus sa connexion locale ; fermer puis recréer.");
    }
    const end = Date.now() + 30000;
    while (Date.now() < end) {
      const current = await this.api(`/realtime/calls/${this.call.call_id}`);
      this.call = current;
      this.update({ call: current });
      if (current.control_status === "ready") {
        if (!this.pc) throw new Error("Connexion locale fermée ; fermer aussi l’appel distant.");
        if (!this.pc.currentRemoteDescription) await this.pc.setRemoteDescription({ type: "answer", sdp: current.sdp_answer });
        return;
      }
      if (["failed", "closed", "closing"].includes(current.control_status)) throw new Error(`Appel ${current.control_status}.`);
      await delay(300);
    }
    throw new Error("Appel toujours en préparation. Utiliser Retrouver l’appel.");
  }
  async ask(text) {
    if (this.pc?.connectionState !== "connected" || !this.state.alertsConnected) throw new Error("Préparer l’audio et le canal d’alertes.");
    if (this.pending && this.state.question?.media_status !== "closed") throw new Error("Résoudre la demande précédente ou confirmer la fin de lecture.");
    this.pending = {
      request_id: crypto.randomUUID(), call_id: this.call.call_id,
      media_epoch: this.call.media_epoch, text, playback_ready: true,
    };
    this.sequence = 0;
    this.responseId = null;
    this.requestStarted = performance.now();
    this.update({ question: null, providerDrained: false, providerStartedMs: null, error: null });
    // Keep the exact payload on every error, including an uncertain HTTP timeout.
    await this.sendPending();
  }
  async sendPending() {
    if (!this.pending) throw new Error("Aucune demande à retrouver.");
    const result = await this.api("/questions", "POST", this.pending);
    this.update({ question: result });
    this.watchQuestion();
    return result;
  }
  async resolveQuestion() {
    if (!this.pending) throw new Error("Aucune demande à retrouver.");
    const requestId = this.pending.request_id;
    const question = await this.api(`/questions/${requestId}`);
    if (this.pending?.request_id !== requestId) return question;
    this.update({ question });
    if (["failed", "cancelled"].includes(question.status)) this.mute();
    return question;
  }
  watchQuestion() {
    clearInterval(this.poll);
    const requestId = this.pending.request_id;
    let reading = false;
    this.poll = setInterval(async () => {
      if (reading || this.disposed || this.pending?.request_id !== requestId) return;
      reading = true;
      try {
        const q = await this.resolveQuestion();
        if (this.pending?.request_id !== requestId) return;
        if (["completed", "failed", "cancelled", "unavailable"].includes(q.status)) clearInterval(this.poll);
      } catch (e) {
        if (this.pending?.request_id !== requestId) return;
        this.update({ error: e.message });
        // A lost control channel must never leave unbounded speech playing.
        await this.stop("connection_lost").catch(() => {});
      } finally { reading = false; }
    }, 400);
  }
  async finish() {
    if (this.state.question?.status !== "completed" || !this.state.providerDrained) throw new Error("Attendre la génération et la fin d’émission, puis confirmer la lecture locale.");
    const { call_id, media_epoch, request_id } = this.pending;
    await this.api(`/questions/${request_id}/playback`, "PUT", {
      call_id, media_epoch, sequence: ++this.sequence, state: "finished",
      position_ms: null, measurement: "unavailable",
    });
    await this.resolveQuestion();
  }
  mute() {
    this.audio.muted = true;
    this.audio.pause();
    this.audio.srcObject = null;
    const pc = this.pc;
    this.pc = null;
    pc?.close();
    this.update({ connection: "closed" });
  }
  async stop(reason = "user_stop") {
    this.mute(); // Synchronous local cutoff always precedes any network await.
    clearInterval(this.poll);
    if (this.stopping) return this.stopping;
    this.stopping = this.closeRemote(reason).finally(() => { this.stopping = null; });
    return this.stopping;
  }
  async closeRemote(reason) {
    let call = this.call;
    if (!call) {
      try { call = await this.api("/realtime/calls/current"); }
      catch (e) { if (e.status !== 404) throw e; }
    }
    if (call) {
      if (this.pending) {
        const { request_id, call_id, media_epoch } = this.pending;
        try { await this.api(`/questions/${request_id}/cancel`, "POST", { call_id, media_epoch, reason }); }
        catch (e) { if (e.status !== 404) throw e; }
      }
      await this.api(`/realtime/calls/${call.call_id}`, "DELETE");
      const end = Date.now() + 30000;
      let closed = false;
      while (Date.now() < end) {
        const current = await this.api(`/realtime/calls/${call.call_id}`);
        this.call = current;
        this.update({ call: current });
        if (current.control_status === "closed") { closed = true; break; }
        await delay(400);
      }
      if (!closed) throw new Error("Clôture distante non confirmée ; réessayer Fermer.");
    }
    if (this.pending) {
      try { await this.resolveQuestion(); }
      catch (e) { if (e.status !== 404) throw e; }
    }
    this.call = null;
    this.offer = null;
    this.pending = null;
  }
  dispose() {
    this.disposed = true;
    clearTimeout(this.reconnect);
    this.alerts?.close();
    return this.stop("background");
  }
}
