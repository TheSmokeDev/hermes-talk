/**
 * hermes-talk — Dashboard Plugin
 *
 * A live Realtime voice session in the browser. The backend at
 * /api/plugins/hermes-talk/ mints an EPHEMERAL client secret; this page dials
 * OpenAI directly with it over WebRTC (audio on the media track, events on the
 * `oai-events` data channel), relays every model function call back to
 * /tool, and polls /runs so a background run is spoken when it lands.
 *
 * Plain IIFE, no build step — same shape as the in-tree kanban and
 * achievements plugins. Uses window.__HERMES_PLUGIN_SDK__ for React and the
 * shadcn primitives so nothing is bundled twice.
 *
 * The minted client secret lives in the transport instance only. It is never
 * logged, never persisted, and never re-sent anywhere but OpenAI's offer URL.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useRef, useCallback } = SDK.hooks;
  const C = SDK.components;

  const API = "/api/plugins/hermes-talk";
  /** Tab-scoped, not localStorage: the token dies with the tab, like a session. */
  const TOKEN_KEY = "hermes-talk-dashboard-token";
  const OFFER_TIMEOUT_MS = 30000;
  const RUN_POLL_MS = 5000;
  const IDLE_POLL_MS = 20000;
  /** How long to watch each run kind before letting go. The work continues. */
  const RUN_POLL_CAPS_MS = { agent: 2700000, skill: 600000 };
  const DEFAULT_RUN_CAP_MS = 600000;
  /** Matches talk_runs.started_sentinel — the contract for "poll me". */
  const WORK_STARTED_RE = /WORK_STARTED #(\d+) kind=(\w+)/;

  // -- api ------------------------------------------------------------------

  function readToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function writeToken(value) {
    try {
      if (value) window.sessionStorage.setItem(TOKEN_KEY, value);
      else window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (e) {
      /* private mode — the token just does not persist across a reload */
    }
  }

  /**
   * One call to the plugin backend. SDK.fetchJSON carries the DASHBOARD's own
   * auth; the x-talk-token header carries hermes-talk's second gate, which is
   * what TALK_DASHBOARD_TOKEN checks.
   */
  function apiCall(path, init) {
    const opts = Object.assign({}, init || {});
    const headers = Object.assign({}, opts.headers || {});
    const token = readToken();
    if (token) headers["x-talk-token"] = token;
    if (opts.body) headers["content-type"] = "application/json";
    opts.headers = headers;
    return SDK.fetchJSON(API + path, opts);
  }

  function apiPost(path, body) {
    return apiCall(path, { method: "POST", body: JSON.stringify(body || {}) });
  }

  /** fetchJSON throws Error("<status>: <body>") — the gate's refusals look like this. */
  function isAuthError(err) {
    return /^(401|403)\b/.test(String((err && err.message) || ""));
  }

  function errorText(err) {
    const raw = String((err && err.message) || err || "unknown error");
    return raw.length > 400 ? raw.slice(0, 400) + "…" : raw;
  }

  function describeSource(source) {
    if (source === "configured") return "TALK_OPENAI_API_KEY";
    if (source === "env") return "OPENAI_API_KEY";
    if (source === "codex-oauth") return "Codex OAuth (ChatGPT sign-in)";
    return "not configured";
  }

  // -- transport ------------------------------------------------------------

  /**
   * WebRTC Realtime transport. Audio arrives on the media track and plays in
   * real time, so there is no local playback queue: the CLI's drain + truncate
   * barge-in has no browser analogue and none is faked here. What IS mirrored
   * from talk_relay is the response.cancel gate — cancelling while the model
   * is idle earns a "no active response" error on every operator turn.
   */
  class TalkTransport {
    constructor(session, callbacks) {
      this.session = session;
      this.cb = callbacks;
      this.peer = null;
      this.channel = null;
      this.media = null;
      this.audio = null;
      this.offerAbort = null;
      this.closed = false;
      this.responseActive = false;
    }

    async start() {
      if (typeof RTCPeerConnection === "undefined" || !navigator.mediaDevices) {
        throw new Error("This browser has no WebRTC or microphone access.");
      }
      this.closed = false;
      const peer = new RTCPeerConnection();
      this.peer = peer;

      this.audio = document.createElement("audio");
      this.audio.autoplay = true;
      this.audio.style.display = "none";
      document.body.appendChild(this.audio);
      peer.addEventListener("track", (event) => {
        const stream = event.streams[0];
        if (this.audio && stream) this.audio.srcObject = stream;
      });

      const media = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (this.closed) {
        media.getTracks().forEach((track) => track.stop());
        return;
      }
      this.media = media;
      media.getAudioTracks().forEach((track) => peer.addTrack(track, media));

      const channel = peer.createDataChannel("oai-events");
      this.channel = channel;
      channel.addEventListener("open", () => this.cb.onStatus("Listening…"));
      channel.addEventListener("message", (event) => this.handleEvent(event.data));
      peer.addEventListener("connectionstatechange", () => {
        if (this.closed) return;
        if (peer.connectionState === "failed" || peer.connectionState === "closed") {
          this.cb.onError("Realtime connection closed.");
          this.stop();
        }
      });

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      const answer = await this.postOffer(offer);
      if (this.closed) return;
      await peer.setRemoteDescription({ type: "answer", sdp: answer });
    }

    async postOffer(offer) {
      const controller = new AbortController();
      this.offerAbort = controller;
      const timer = window.setTimeout(() => controller.abort(), OFFER_TIMEOUT_MS);
      try {
        const res = await fetch(this.session.offerUrl, {
          method: "POST",
          body: offer.sdp,
          headers: {
            // The EPHEMERAL secret. The credential that minted it never left
            // the Hermes process.
            Authorization: "Bearer " + this.session.clientSecret,
            "Content-Type": "application/sdp",
          },
          signal: controller.signal,
        });
        if (!res.ok) throw new Error("Realtime WebRTC setup failed (" + res.status + ")");
        return await res.text();
      } finally {
        window.clearTimeout(timer);
        if (this.offerAbort === controller) this.offerAbort = null;
      }
    }

    stop() {
      this.closed = true;
      if (this.offerAbort) this.offerAbort.abort();
      this.offerAbort = null;
      if (this.channel) this.channel.close();
      this.channel = null;
      if (this.peer) this.peer.close();
      this.peer = null;
      if (this.media) this.media.getTracks().forEach((track) => track.stop());
      this.media = null;
      if (this.audio) this.audio.remove();
      this.audio = null;
    }

    send(payload) {
      if (this.channel && this.channel.readyState === "open") {
        this.channel.send(JSON.stringify(payload));
      }
    }

    handleEvent(data) {
      if (this.closed) return;
      let event;
      try {
        event = JSON.parse(String(data));
      } catch (e) {
        return;
      }
      switch (event.type) {
        case "conversation.item.input_audio_transcription.completed":
          if (event.transcript) this.cb.onTranscript("user", event.transcript, true);
          return;
        case "response.output_audio_transcript.delta":
          if (event.delta) this.cb.onTranscript("assistant", event.delta, false);
          return;
        case "response.output_audio_transcript.done":
          if (event.transcript) this.cb.onTranscript("assistant", event.transcript, true);
          return;
        case "response.created":
          this.responseActive = true;
          this.cb.onStatus("Thinking…");
          return;
        case "response.done":
          this.responseActive = false;
          this.cb.onStatus("Listening…");
          return;
        case "input_audio_buffer.speech_started":
          this.cb.onStatus("Listening…");
          // Barge-in. Server VAD already interrupts; the explicit cancel is the
          // relay's contract, and the gate is why it does not error every turn.
          if (this.responseActive) this.send({ type: "response.cancel" });
          return;
        case "input_audio_buffer.speech_stopped":
          this.cb.onStatus("Processing…");
          return;
        case "response.function_call_arguments.done":
          void this.handleFunctionCall(event);
          return;
        case "error":
          this.handleError(event.error);
          return;
        default:
          return;
      }
    }

    handleError(error) {
      let detail = "";
      if (error && typeof error === "object") {
        detail = String(error.message || error.code || error.type || "");
      } else if (typeof error === "string") {
        detail = error;
      }
      // A cancel that lost the race with response.done is not actionable —
      // same suppression talk_relay applies.
      if (detail.toLowerCase().indexOf("no active response") !== -1) return;
      this.cb.onError(detail ? "Realtime error: " + detail : "Realtime error.");
    }

    /**
     * Relay one function call to the Python tool surface and feed the result
     * back. The session survives a tool failure — the model speaks the error
     * text instead of the call dying.
     */
    async handleFunctionCall(event) {
      const callId = typeof event.call_id === "string" ? event.call_id : "";
      const name = typeof event.name === "string" ? event.name : "";
      if (!callId || !name) return;
      let args = {};
      try {
        const parsed = JSON.parse(event.arguments || "{}");
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) args = parsed;
      } catch (e) {
        /* a malformed arguments blob executes with {} */
      }
      this.cb.onStatus("Using " + name + "…");
      let output;
      try {
        const res = await apiPost("/tool", { name: name, arguments: args });
        output = res && res.output ? String(res.output) : "(no output)";
      } catch (err) {
        output = name + " failed: " + errorText(err);
      }
      this.send({
        type: "conversation.item.create",
        item: { type: "function_call_output", call_id: callId, output: output },
      });
      this.send({ type: "response.create" });
      this.watchForRun(output);
    }

    /** Start polling if this text announced background work. */
    watchForRun(text) {
      const started = WORK_STARTED_RE.exec(String(text || ""));
      if (started) this.pollRun(Number(started[1]), started[2]);
    }

    /**
     * The run finished somewhere else; say so. Injected as a user-role note so
     * the model speaks the result the moment it lands, unprompted. A finished
     * run can announce a follow-on run, so results are re-scanned.
     */
    pollRun(runId, kind) {
      const startedAt = Date.now();
      const cap = RUN_POLL_CAPS_MS[kind] || DEFAULT_RUN_CAP_MS;
      const tick = async () => {
        if (this.closed || Date.now() - startedAt > cap) return;
        let run = null;
        try {
          const res = await apiCall("/runs");
          const runs = (res && res.runs) || [];
          for (let i = 0; i < runs.length; i++) {
            if (Number(runs[i].runId) === runId) {
              run = runs[i];
              break;
            }
          }
        } catch (e) {
          window.setTimeout(tick, RUN_POLL_MS);
          return;
        }
        if (!run || run.status === "running") {
          window.setTimeout(tick, RUN_POLL_MS);
          return;
        }
        const result = run.output || "(no output)";
        this.send({
          type: "conversation.item.create",
          item: {
            type: "message",
            role: "user",
            content: [
              {
                type: "input_text",
                text:
                  "Work run #" + runId + " (" + (run.kind || kind) + ") finished with " +
                  "status '" + run.status + "'. Result: " + result + "\n\n" +
                  "Summarize this aloud in one to three spoken sentences.",
              },
            ],
          },
        });
        this.send({ type: "response.create" });
        this.watchForRun(result);
      };
      void tick();
    }
  }

  // -- page -----------------------------------------------------------------

  function TalkPage() {
    const [status, setStatus] = useState(null);
    const [loading, setLoading] = useState(true);
    const [voice, setVoice] = useState("");
    const [phase, setPhase] = useState("idle"); // idle | starting | active
    const [live, setLive] = useState("");
    const [transcript, setTranscript] = useState([]);
    const [runs, setRuns] = useState([]);
    const [error, setError] = useState("");
    const [needsToken, setNeedsToken] = useState(false);
    const [tokenDraft, setTokenDraft] = useState("");

    const transportRef = useRef(null);
    const rowId = useRef(1);
    const phaseRef = useRef("idle");
    phaseRef.current = phase;

    const handleError = useCallback((err) => {
      if (isAuthError(err)) setNeedsToken(true);
      setError(errorText(err));
    }, []);

    const refresh = useCallback(async () => {
      setLoading(true);
      try {
        const res = await apiCall("/status");
        setStatus(res);
        setVoice((current) => current || res.voice || "");
        setNeedsToken(false);
        setError("");
      } catch (err) {
        setStatus(null);
        handleError(err);
      } finally {
        setLoading(false);
      }
    }, [handleError]);

    const refreshRuns = useCallback(async () => {
      try {
        const res = await apiCall("/runs");
        setRuns((res && res.runs) || []);
      } catch (e) {
        /* the runs panel is a status board — a failed poll is not a page error */
      }
    }, []);

    useEffect(() => {
      void refresh();
      return () => {
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
      };
    }, [refresh]);

    useEffect(() => {
      let cancelled = false;
      let timer = 0;
      const loop = async () => {
        if (cancelled) return;
        await refreshRuns();
        if (cancelled) return;
        timer = window.setTimeout(loop, phaseRef.current === "active" ? RUN_POLL_MS : IDLE_POLL_MS);
      };
      void loop();
      return () => {
        cancelled = true;
        window.clearTimeout(timer);
      };
    }, [refreshRuns]);

    const appendTranscript = useCallback((role, text, final) => {
      setTranscript((prev) => {
        const last = prev[prev.length - 1];
        if (role === "assistant" && !final) {
          if (last && last.role === "assistant" && !last.final) {
            const merged = Object.assign({}, last, { text: last.text + text });
            return prev.slice(0, -1).concat([merged]);
          }
          return prev.concat([{ id: rowId.current++, role: role, text: text, final: false }]);
        }
        if (role === "assistant" && last && last.role === "assistant" && !last.final) {
          const done = Object.assign({}, last, { text: text, final: true });
          return prev.slice(0, -1).concat([done]);
        }
        return prev.concat([{ id: rowId.current++, role: role, text: text, final: true }]);
      });
    }, []);

    async function startTalk() {
      setError("");
      if (typeof RTCPeerConnection === "undefined" || !navigator.mediaDevices) {
        setError("Talk needs a browser with WebRTC and microphone access.");
        return;
      }
      setPhase("starting");
      setLive("");
      setTranscript([]);
      try {
        const session = await apiPost("/session", voice ? { voice: voice } : {});
        const transport = new TalkTransport(session, {
          onStatus: setLive,
          onTranscript: appendTranscript,
          onError: (message) => setError(message),
        });
        transportRef.current = transport;
        await transport.start();
        setPhase("active");
      } catch (err) {
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
        setPhase("idle");
        setLive("");
        handleError(err);
      }
    }

    function stopTalk() {
      if (transportRef.current) transportRef.current.stop();
      transportRef.current = null;
      setPhase("idle");
      setLive("");
    }

    function saveToken() {
      writeToken(tokenDraft.trim());
      setTokenDraft("");
      void refresh();
    }

    const ready = Boolean(status && status.configured);
    const active = phase === "active";
    const starting = phase === "starting";

    return h("div", { className: "ht-page" },
      h("div", { className: "ht-head" },
        h("div", null,
          h("h1", { className: "ht-title" }, "Talk"),
          h("div", { className: "ht-sub" },
            loading ? "Checking readiness…"
              : ready ? "Ready via " + describeSource(status.source)
              : (status && status.detail) || "Not configured"
          )
        ),
        h("div", { className: "ht-actions" },
          active || starting
            ? h(C.Button, { onClick: stopTalk, disabled: starting },
                starting ? "Connecting…" : "Stop")
            : h(C.Button, { onClick: () => void startTalk(), disabled: !ready || loading },
                "Start")
        )
      ),

      needsToken && h("div", { className: "ht-note ht-note-warn" },
        h("div", { className: "ht-note-title" }, "This dashboard needs the hermes-talk token"),
        h("div", null,
          "TALK_DASHBOARD_TOKEN is set on the server, or this browser is not on " +
          "loopback. Paste the token to use it in this tab."),
        h("div", { className: "ht-token-row" },
          h(C.Input, {
            type: "password",
            value: tokenDraft,
            placeholder: "TALK_DASHBOARD_TOKEN",
            onChange: (e) => setTokenDraft(e.target.value),
          }),
          h(C.Button, { onClick: saveToken }, "Use token")
        )
      ),

      h("div", { className: "ht-metrics" },
        h(Metric, { label: "Auth", value: status ? describeSource(status.source) : "…" }),
        h(Metric, { label: "Model", value: (status && status.model) || "…" }),
        h(Metric, {
          label: "Session",
          value: active ? "live" : starting ? "connecting" : "idle",
        }),
        h(Metric, {
          label: "Agent loop",
          value: status && status.agentLoop ? "attached" : "out of process",
        })
      ),

      h("label", { className: "ht-field" }, "Voice",
        h("select", {
          className: "ht-select",
          value: voice,
          disabled: !ready || active || starting,
          onChange: (e) => setVoice(e.target.value),
        }, ((status && status.voices) || []).map((name) =>
          h("option", { key: name, value: name }, name)))
      ),

      live && h("div", { className: "ht-live" }, live),
      error && h("div", { className: "ht-error" }, error),

      h("div", { className: "ht-grid" },
        h("section", { className: "ht-card" },
          h("div", { className: "ht-card-head" }, "Transcript"),
          transcript.length === 0
            ? h("div", { className: "ht-empty" }, "No transcript yet. Start a session and speak.")
            : transcript.map((row) =>
                h("div", { key: row.id, className: "ht-row" },
                  h("div", { className: "ht-role" },
                    (row.role === "user" ? "You" : "Hermes") + (row.final ? "" : " …")),
                  h("div", { className: "ht-text" }, row.text)))
        ),
        h("section", { className: "ht-card" },
          h("div", { className: "ht-card-head" }, "Background runs"),
          runs.length === 0
            ? h("div", { className: "ht-empty" }, "Nothing running.")
            : runs.map((run) =>
                h("div", { key: run.runId, className: "ht-row" },
                  h("div", { className: "ht-role" },
                    h("span", { className: "ht-status ht-status-" + run.status }, run.status),
                    " run " + run.runId + " · " + (run.kind || "?")),
                  h("div", { className: "ht-text" }, run.label || ""),
                  run.output && h("div", { className: "ht-out" }, run.output)))
        )
      )
    );
  }

  function Metric(props) {
    return h("div", { className: "ht-metric" },
      h("div", { className: "ht-metric-label" }, props.label),
      h("div", { className: "ht-metric-value" }, props.value));
  }

  window.__HERMES_PLUGINS__.register("hermes-talk", TalkPage);
})();
