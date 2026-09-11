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
  const TOOL_TIMEOUT_MS = 6500;
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
  async function apiCall(path, init, timeoutMs) {
    const opts = Object.assign({}, init || {});
    const headers = Object.assign({}, opts.headers || {});
    const token = readToken();
    if (token) headers["x-talk-token"] = token;
    if (opts.body) headers["content-type"] = "application/json";
    opts.headers = headers;
    if (!timeoutMs) return SDK.fetchJSON(API + path, opts);
    const controller = new AbortController();
    opts.signal = controller.signal;
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await SDK.fetchJSON(API + path, opts);
    } finally {
      window.clearTimeout(timer);
    }
  }

  function apiPost(path, body, timeoutMs) {
    return apiCall(path, { method: "POST", body: JSON.stringify(body || {}) }, timeoutMs);
  }

  function clientId(prefix) {
    return prefix + window.crypto.randomUUID().replace(/-/g, "");
  }

  function taskTabId() {
    const key = "hermes-talk-task-tab";
    try {
      let id = window.sessionStorage.getItem(key);
      if (!id) { id = clientId("tab_"); window.sessionStorage.setItem(key, id); }
      return id;
    } catch (e) { return clientId("tab_"); }
  }

  /** A bound connection owns every request and every provider identity below. */
  class TaskContinuity {
    constructor(transport, task) {
      this.transport = transport;
      this.context = Object.freeze({ connection_id: task.connection_id, generation: task.generation });
      this.closed = false;
      this.controllers = new Set();
      this.inputs = new Map();
      this.committed = new Set();
      this.requests = new Map();
      this.responses = new Map();
      this.completedGroups = [];
      this.toolTail = Promise.resolve();
      this.stateTail = null;
    }

    async request(path, body, method) {
      if (this.closed) throw new Error("Task connection closed.");
      const controller = new AbortController();
      this.controllers.add(controller);
      const timer = window.setTimeout(() => controller.abort(), OFFER_TIMEOUT_MS);
      try {
        const opts = { method: method || "POST", signal: controller.signal };
        if (opts.method === "POST") opts.body = JSON.stringify(Object.assign({}, body, this.context));
        const result = await apiCall(path, opts);
        if (this.closed || controller.signal.aborted) throw new Error("Task connection closed or request cancelled.");
        return result;
      } finally {
        window.clearTimeout(timer);
        this.controllers.delete(controller);
      }
    }

    report(message) {
      if (!this.closed) this.transport.cb.onError(message);
    }

    refresh() {
      if (this.closed) return Promise.resolve();
      if (this.stateTail) return this.stateTail;
      this.stateTail = this.request("/state", {}).then((state) => {
        if (!this.closed && this.transport.cb.onTaskState) this.transport.cb.onTaskState(state);
      }).catch((err) => this.report(errorText(err))).finally(() => { this.stateTail = null; });
      return this.stateTail;
    }

    async result(runId) {
      const query = "?connection_id=" + encodeURIComponent(this.context.connection_id) +
        "&generation=" + encodeURIComponent(this.context.generation) + "&run_id=" + encodeURIComponent(runId);
      // Results are inert UI data. Never inject them into the provider conversation.
      return this.request("/result" + query, null, "GET");
    }

    notice(row, state) {
      if (!this.closed && this.transport.cb.onTaskStage) {
        this.transport.cb.onTaskStage({ input_id: row.input_id, text: row.text, state: state });
      }
    }

    event(row, body) {
      const next = row.tail.then(async () => {
        const receipt = await row.ready;
        if (!receipt || this.closed) return null;
        return this.request("/event", Object.assign({}, body, { interaction_id: receipt.interaction_id }));
      });
      row.tail = next.catch((err) => {
        row.incomplete = true;
        this.notice(row, "incomplete");
        this.report(errorText(err));
      });
      return next;
    }

    incomplete(row, reason) {
      if (this.closed || row.incomplete) return;
      row.incomplete = true;
      this.notice(row, "incomplete: " + reason);
      void this.event(row, { kind: "interaction.incomplete", reason: reason }).then(() => this.refresh()).catch(() => {});
    }

    stage(inputId, type, text) {
      if (this.closed) return null;
      if (!inputId || !text) {
        this.report("Input is unlinked: a stable provider item ID and finalized text are required.");
        return null;
      }
      if (this.inputs.has(inputId)) {
        const existing = this.inputs.get(inputId);
        if (existing.text !== text || existing.type !== type) this.incomplete(existing, "linkage_ambiguous");
        return existing;
      }
      const row = { input_id: inputId, type: type, text: text, tail: Promise.resolve(),
        requested: false, incomplete: false, items: [inputId] };
      this.inputs.set(inputId, row);
      this.notice(row, "staging");
      row.ready = this.request("/event", {
        kind: "input.final", input_id: inputId, input_type: type, text: text,
      }).then((receipt) => {
        if (!receipt || !receipt.interaction_id || receipt.input_id !== inputId) throw new Error("Invalid input stage receipt.");
        row.receipt = receipt;
        this.notice(row, receipt.state || "staged");
        void this.refresh();
        return receipt;
      }).catch((err) => {
        row.incomplete = true;
        this.notice(row, "incomplete: input_stage_failed");
        this.report(errorText(err));
        return null;
      });
      return row;
    }

    async typed(text) {
      if (typeof text !== "string" || !text.trim()) return false;
      const row = this.stage(clientId("item_"), "typed", text);
      if (!row) return false;
      const receipt = await row.ready;
      if (!receipt || this.closed || row.incomplete) return false;
      this.transport.send({ type: "conversation.item.create", item: {
        id: row.input_id, type: "message", role: "user", content: [{ type: "input_text", text: row.text }],
      } });
      this.requestResponse(row);
      return true;
    }

    voice(event) {
      const row = this.stage(event.item_id, "voice", event.transcript);
      if (row) void this.respondToAudio(row);
    }

    async respondToAudio(row) {
      const receipt = await row.ready;
      if (receipt && !this.closed && !row.incomplete && this.committed.has(row.input_id) && !row.requested) {
        this.requestResponse(row);
      }
    }

    requestResponse(row, previous) {
      if (this.closed || row.incomplete) return;
      if (!previous && row.requested) return;
      row.requested = true;
      const token = clientId("req_");
      const metadata = { talk_request_id: token, talk_interaction_id: row.receipt.interaction_id,
        talk_input_id: row.input_id, talk_previous_response_id: previous || "" };
      this.requests.set(token, { row: row, previous: previous || "", claimed: false });
      // Explicit input references keep overlapping ASR completions from selecting
      // whichever utterance happens to be last in the provider conversation.
      const prior = this.completedGroups.filter((group) => !group.row.incomplete && group.row !== row)
        .flatMap((group) => group.items);
      this.transport.send({ type: "response.create", response: {
        metadata: metadata, input: prior.concat(row.items).map((id) => ({ type: "item_reference", id: id })),
      } });
    }

    rememberCompleted(row) {
      if (this.closed || row.incomplete || row.remembered) return;
      row.remembered = true;
      this.completedGroups.push({ row: row, items: row.items.slice() });
      // Drop whole interactions, including oversized groups, so a retained
      // function output never loses its matching input/call context.
      let count = this.completedGroups.reduce((total, group) => total + group.items.length, 0);
      while (count > 64) count -= this.completedGroups.shift().items.length;
    }

    created(response) {
      const meta = response.metadata || {};
      const request = this.requests.get(meta.talk_request_id);
      if (!response.id || !request || meta.talk_interaction_id !== request.row.receipt.interaction_id ||
          meta.talk_input_id !== request.row.input_id || meta.talk_previous_response_id !== request.previous) {
        this.report("Response is unlinked: provider response metadata did not identify a staged input.");
        return;
      }
      if (this.responses.has(response.id)) return;
      if (request.claimed) { this.incomplete(request.row, "linkage_ambiguous"); return; }
      request.claimed = true;
      const current = { id: response.id, row: request.row, previous: request.previous,
        calls: new Map(), declared: null, done: false, finished: false };
      this.responses.set(response.id, current);
      const body = { kind: "response.started", response_id: response.id };
      if (request.previous) body.previous_response_id = request.previous;
      void this.event(current.row, body).catch(() => {});
    }

    final(event, text) {
      const response = this.responses.get(event.response_id);
      if (!response) { this.report("Final response is unlinked; it has not been saved."); return; }
      if (!event.item_id) { this.incomplete(response.row, "linkage_ambiguous"); return; }
      if (!response.row.items.includes(event.item_id)) response.row.items.push(event.item_id);
      void this.event(response.row, { kind: "response.final", response_id: response.id,
        output_item_id: event.item_id, text: text }).catch(() => {});
    }

    tool(event) {
      const response = this.responses.get(event.response_id);
      if (!response || !event.call_id || !event.name) {
        this.report("Tool call refused: original input, response and call IDs must be linked.");
        return;
      }
      if (response.done && !response.declared.includes(event.call_id)) {
        this.incomplete(response.row, "missing_tool_calls");
        return;
      }
      if (response.calls.has(event.call_id)) return;
      const call = { done: false, result: null };
      response.calls.set(event.call_id, call);
      this.toolTail = this.toolTail.then(async () => {
        await response.row.tail;
        if (this.closed || response.row.incomplete) return;
        const receipt = await response.row.ready;
        if (!receipt || this.closed) return;
        call.result = await this.transport.handleFunctionCall(event, {
          interaction_id: receipt.interaction_id, response_id: response.id, call_id: event.call_id,
        });
        if (this.closed) return;
        if (!call.result) { this.incomplete(response.row, "tool_failed"); return; }
        call.done = true;
        void this.refresh();
        this.finish(response);
      }).catch((err) => { this.report(errorText(err)); this.incomplete(response.row, "tool_failed"); });
    }

    done(value) {
      const response = this.responses.get(value.id);
      if (!response) { this.report("Completed response is unlinked; it has not been saved."); return; }
      if (response.done) return;
      const output = Array.isArray(value.output) ? value.output : [];
      if (!Array.isArray(value.output)) { this.incomplete(response.row, "missing_tool_calls"); return; }
      response.declared = output.filter((item) => item.type === "function_call").map((item) => item.call_id);
      if (response.declared.some((id) => !id) || new Set(response.declared).size !== response.declared.length) {
        this.incomplete(response.row, "missing_tool_calls"); return;
      }
      for (const item of output) {
        if (item.id && !response.row.items.includes(item.id)) response.row.items.push(item.id);
        if (item.type === "message") {
          const text = (item.content || []).map((part) => part.text || part.transcript || "").join("");
          if (text) this.final({ response_id: response.id, item_id: item.id }, text);
        }
      }
      response.done = true;
      response.doneReceipt = this.event(response.row, { kind: "response.done", response_id: response.id,
        status: value.status === "completed" ? "completed" : value.status === "cancelled" ? "cancelled" : "failed",
        tool_call_ids: response.declared });
      void response.doneReceipt.then(() => this.finish(response)).catch(() => {});
      if (value.status !== "completed") this.incomplete(response.row, "response_failed");
      // Arguments may arrive after response.done. Only explicit call IDs can
      // satisfy this barrier; the number of completed promises cannot.
      this.finish(response);
    }

    finish(response) {
      if (this.closed || response.finished || !response.done || response.row.incomplete) return;
      if (Array.from(response.calls.keys()).some((id) => !response.declared.includes(id))) {
        this.incomplete(response.row, "missing_tool_calls"); return;
      }
      if (response.declared.some((id) => !response.calls.has(id) || !response.calls.get(id).done)) return;
      response.finished = true;
      void response.row.tail.then(async () => {
        if (this.closed || response.row.incomplete) return;
        if (response.declared.length) {
          const selections = response.declared.map((id) => response.calls.get(id).result.selection).filter(Boolean);
          if (selections.length) {
            // A tool supplies an intent. Only the page's authorized /switch
            // receipt can replace this connection; original input stays here.
            const same = selections.every((intent) => JSON.stringify(intent) === JSON.stringify(selections[0]));
            const activate = this.transport.cb.onSelectionIntent;
            if (same && activate && await activate(selections[0], this.transport)) return;
            if (this.closed) return;
            if (!same) this.report("Conflicting target selections require an explicit choice.");
            for (const id of response.declared) {
              const result = response.calls.get(id).result;
              if (result.selection) result.message.item.output = "Target activation was not confirmed on this connection. Do not claim a target change; ask the operator to reconcile or retry.";
            }
          }
          for (const id of response.declared) {
            const result = response.calls.get(id).result;
            result.message.item.id = clientId("item_");
            response.row.items.push(result.message.item.id);
            this.transport.send(result.message);
          }
          this.requestResponse(response.row, response.id);
        } else {
          const receipt = await this.event(response.row, { kind: "interaction.settle", response_id: response.id });
          if (receipt && receipt.ok) this.rememberCompleted(response.row);
          void this.refresh();
        }
      }).catch((err) => this.report(errorText(err)));
    }

    close() {
      if (this.closed) return;
      this.closed = true;
      this.controllers.forEach((controller) => controller.abort());
      this.controllers.clear();
      this.requests.clear();
      // Revocation is best effort; all local continuations are already fenced.
      void apiCall("/close", { method: "POST", body: JSON.stringify(this.context), keepalive: true }).catch(() => {});
    }
  }

  /** NDJSON encoder for the cascade relay, minted lazily (cascade mode only). */
  let relayEncoder = null;
  function encodeRelayLine(line) {
    if (!relayEncoder) relayEncoder = new TextEncoder();
    return relayEncoder.encode(JSON.stringify(line) + "\n");
  }

  /** The cascade's PCM is 24kHz mono s16le; the context should agree. */
  const PCM_RATE = 24000;

  /**
   * An AudioContext at the PCM's OWN rate, so no resampling happens at all.
   *
   * Web Audio resamples every AudioBuffer independently. At the browser
   * default (48kHz on Windows) two chunks resampled in isolation do not line
   * up where they meet, so every chunk seam is a discontinuity — an audible
   * tick every couple of seconds while the PCM leaving the server is clean.
   * Chrome and Edge honour the requested rate; the ones that refuse fall back
   * to a resampler that carries state across chunks (see resampleToContext).
   */
  /** One mono AudioBuffer at `rate`, filled from `values` scaled by `scale`. */
  function pcmBuffer(ctx, values, rate, scale) {
    const buffer = ctx.createBuffer(1, values.length, rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < values.length; i++) channel[i] = values[i] / scale;
    return buffer;
  }

  function makePcmContext() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return null;
    try {
      return new Ctx({ sampleRate: PCM_RATE });
    } catch (e) {
      return new Ctx();
    }
  }

  /**
   * Whether this page may send a STREAMING request body.
   *
   * Chrome only streams an upload over HTTP/2 or HTTP/3. On a plain HTTP/1.1
   * origin — which a local dashboard almost always is — `fetch` with
   * `duplex: "half"` rejects outright, and the cascade's own `.catch()` used
   * to swallow it: the model's text captioned fine, no audio ever played, and
   * zero requests reached the relay. Gate on the protocol the page actually
   * negotiated and buffer the answer when we cannot stream.
   *
   * Memoised, and computed on FIRST USE rather than at load: the navigation
   * timing entry is not necessarily populated while this IIFE is still
   * evaluating.
   */
  let canStreamUploadCache = null;
  function canStreamUpload() {
    if (canStreamUploadCache !== null) return canStreamUploadCache;
    canStreamUploadCache = false;
    try {
      const nav = (typeof performance !== "undefined" && performance.getEntriesByType)
        ? (performance.getEntriesByType("navigation")[0] || null)
        : null;
      const hop = String((nav && nav.nextHopProtocol) || "").toLowerCase();
      if (!/^h[23]/.test(hop)) return canStreamUploadCache;
      if (typeof Request === "undefined" || typeof ReadableStream === "undefined") {
        return canStreamUploadCache;
      }
      // The browser must also ACCEPT a stream body — older engines throw here.
      new Request("/", { method: "POST", body: new ReadableStream(), duplex: "half" });
      canStreamUploadCache = true;
    } catch (e) {
      canStreamUploadCache = false;
    }
    return canStreamUploadCache;
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
   *
   * Cascade mode (session.voiceMode === "cascade"): the session was minted
   * with text output, so no audio track ever arrives. Instead the model's
   * response.output_text deltas stream to POST /cascade-tts as NDJSON and the
   * PCM24k that comes back plays through an AudioContext. The ElevenLabs key
   * never touches this page — the server owns it.
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
      this.continuationPending = false;
      this.toolBatch = null;
      this.toolTail = Promise.resolve();
      this.cascade = session && session.voiceMode === "cascade";
      // Every response gets its own relay stream; a tool-call continuation
      // can start streaming while the previous answer's PCM still drains, so
      // in-flight streams are a SET, and playback carries a generation that
      // barge-in bumps — a chunk decoded before the operator spoke but
      // scheduled after must not play (the Python cascade's rule, mirrored).
      this.cascadeReq = null;
      this.cascadeReqs = new Set();
      this.pcmContext = null;
      this.pcmNextTime = 0;
      this.pcmSources = [];
      this.pcmGeneration = 0;
      // Resampler carry-over: the previous chunk's last sample and the
      // fractional read position. Only used when the browser refused a
      // 24kHz context; stale values would click on the next answer, so
      // stopPcmPlayback() clears them alongside the generation bump.
      this.pcmPrev = null;
      this.pcmPos = 0;
      // One receipt per session for a relay that failed for a real reason.
      this.cascadeFailureLogged = false;
      this.task = session && session.task ? new TaskContinuity(this, session.task) : null;
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
      channel.addEventListener("open", () => {
        if (this.closed) return;
        this.cb.onStatus("Listening…");
        if (this.task) void this.task.refresh();
      });
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
      if (this.task) this.task.close();
      // Teardown is idempotent and each step is guarded so a throw in one can
      // never skip the rest. (A throw in abortCascade() used to leave the
      // channel/peer open, so the server kept listening even though the UI
      // reset to idle.)
      if (this.offerAbort) {
        try { this.offerAbort.abort(); } catch (e) { /* already aborted */ }
        this.offerAbort = null;
      }
      try { this.abortCascade(); } catch (e) { /* already torn down */ }
      if (this.pcmContext) {
        const ctx = this.pcmContext;
        this.pcmContext = null;
        if (ctx.close) Promise.resolve(ctx.close()).catch(() => {});
      }
      if (this.channel) {
        try {
          if (this.channel.readyState === "open" || this.channel.readyState === "connecting") {
            this.channel.close();
          }
        } catch (e) { /* already closed */ }
        this.channel = null;
      }
      if (this.peer) {
        try {
          if (this.peer.connectionState !== "closed") this.peer.close();
        } catch (e) { /* already closed */ }
        this.peer = null;
      }
      if (this.media) {
        try { this.media.getTracks().forEach((track) => track.stop()); } catch (e) { /* already stopped */ }
        this.media = null;
      }
      if (this.audio) {
        try { this.audio.remove(); } catch (e) { /* already removed */ }
        this.audio = null;
      }
    }

    send(payload) {
      if (!this.closed && this.channel && this.channel.readyState === "open") {
        if (payload && payload.type === "response.create") this.continuationPending = true;
        this.channel.send(JSON.stringify(payload));
      }
    }

    async sendTyped(text) {
      if (this.closed || !text.trim() || !this.channel || this.channel.readyState !== "open") return false;
      if (this.task) return this.task.typed(text);
      this.send({ type: "conversation.item.create", item: {
        id: clientId("item_"), type: "message", role: "user", content: [{ type: "input_text", text: text.trim() }],
      } });
      this.cb.onTranscript("user", text.trim(), true);
      this.send({ type: "response.create" });
      return true;
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
        case "input_audio_buffer.committed":
          if (this.task && event.item_id) {
            this.task.committed.add(event.item_id);
            const row = this.task.inputs.get(event.item_id);
            if (row) void this.task.respondToAudio(row);
          }
          return;
        case "conversation.item.input_audio_transcription.completed":
          if (this.task) { this.task.voice(event); return; }
          if (event.transcript) this.cb.onTranscript("user", event.transcript, true);
          return;
        case "response.output_audio_transcript.delta":
          if (event.delta) this.cb.onTranscript("assistant", event.delta, false);
          return;
        case "response.output_audio_transcript.done":
          if (this.task && event.transcript) this.task.final(event, event.transcript);
          if (event.transcript) this.cb.onTranscript("assistant", event.transcript, true);
          return;
        case "response.output_text.delta":
          // Cascade sessions: the model's answer arrives as TEXT, not audio.
          // The caption uses it directly; the relay speaks it server-side.
          if (event.delta) this.cb.onTranscript("assistant", event.delta, false);
          if (event.delta && this.cascade) this.cascadeSend({ delta: event.delta });
          return;
        case "response.output_text.done": {
          const text = typeof event.text === "string" ? event.text : "";
          if (this.task && text) this.task.final(event, text);
          if (text) this.cb.onTranscript("assistant", text, true);
          if (this.cascade) {
            this.cascadeSend({ done: text });
            this.finishCascadeStream();
          }
          return;
        }
        case "response.created":
          if (this.task) this.task.created(event.response || {});
          this.continuationPending = false;
          this.responseActive = true;
          this.cb.onStatus("Thinking…");
          return;
        case "response.done":
          this.responseActive = false;
          // A response that ends with its text stream still open was
          // interrupted upstream; its half-spoken answer dies with it.
          if (this.cascade && this.cascadeReq && this.cascadeReq.sink) this.abortCascade();
          if (this.task) this.task.done(event.response || {});
          else this.finishToolResponse();
          this.cb.onStatus("Listening…");
          return;
        case "input_audio_buffer.speech_started":
          this.cb.onStatus("Listening…");
          // Barge-in kills the cascade mid-word too: abort the relay fetch
          // (the server cancels the TTS on EOF) and stop every queued buffer.
          if (this.cascade) this.abortCascade();
          // Barge-in. Server VAD already interrupts; the explicit cancel is the
          // relay's contract, and the gate is why it does not error every turn.
          if (this.responseActive) this.send({ type: "response.cancel" });
          return;
        case "input_audio_buffer.speech_stopped":
          this.cb.onStatus("Processing…");
          return;
        case "response.function_call_arguments.done":
          if (this.task) this.task.tool(event);
          else this.enqueueFunctionCall(event);
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

    // -- cascade relay (custom voice; the server speaks, the page plays) -----

    /**
     * Open the relay POST for one response's text stream. The request body is
     * a ReadableStream (duplex: "half") so deltas flow while the PCM answer
     * streams back — the first sentence plays while the model is still
     * writing the second, same as the terminal lane's sentence pipelining.
     */
    startCascadeStream() {
      const req = { controller: new AbortController(), sink: null, buffered: null };
      this.cascadeReq = req;
      this.cascadeReqs.add(req);
      if (!canStreamUpload()) {
        // HTTP/1.1: this browser cannot send a streaming body at all. Collect
        // the answer's lines and post them in one piece when its text is
        // done (see finishCascadeStream). That costs sentence pipelining —
        // the first sentence no longer plays while the model writes the
        // second — but it produces audio instead of silence.
        req.buffered = [];
        return;
      }
      const body = new ReadableStream({
        start: (controller) => {
          req.sink = controller;
        },
      });
      this.sendCascadeRequest(req, body, true);
    }

    /** POST one relay request; `stream` picks the duplex upload path. */
    sendCascadeRequest(req, body, stream) {
      const headers = { "content-type": "application/x-ndjson" };
      const token = readToken();
      if (token) headers["x-talk-token"] = token;
      const init = {
        method: "POST",
        headers: headers,
        body: body,
        signal: req.controller.signal,
      };
      if (stream) init.duplex = "half";
      // Raw fetch, not apiCall: this is a byte stream, not JSON.
      fetch(API + "/cascade-tts", init)
        .then((res) => {
          if (!res.ok || !res.body) {
            this.noteCascadeFailure("relay refused the answer (" +
              ((res && res.status) || "no response") + ")");
            return undefined;
          }
          return this.playCascadePcm(req, res.body.getReader());
        })
        .catch((error) => {
          // A barge-in or hang-up aborts on purpose and is not a failure.
          // Anything else used to vanish here — which is how a browser that
          // silently refused to stream the upload looked exactly like a
          // working session with a mute voice.
          if (!req.controller.signal.aborted) {
            this.noteCascadeFailure(errorText(error));
          }
        });
    }

    /** Say once, per session, that the custom voice is not being heard. */
    noteCascadeFailure(detail) {
      if (this.cascadeFailureLogged) return;
      this.cascadeFailureLogged = true;
      const message = "Custom voice unavailable — answers stay text-only. " + detail;
      if (typeof console !== "undefined" && console.warn) console.warn(message);
      if (this.cb && this.cb.onError) {
        try { this.cb.onError(message); } catch (e) { /* UI must not kill audio */ }
      }
    }

    cascadeSend(line) {
      // The previous response's relay may still be draining PCM — that is no
      // reason to drop THIS response's text; it opens its own stream.
      const open = this.cascadeReq && (this.cascadeReq.sink || this.cascadeReq.buffered);
      if (!open) this.startCascadeStream();
      const req = this.cascadeReq;
      if (!req) return;
      // An upload stream carries BYTES — a string chunk is a fetch-type error.
      if (req.sink) req.sink.enqueue(encodeRelayLine(line));
      else if (req.buffered) req.buffered.push(encodeRelayLine(line));
    }

    /** The response's text is complete; the PCM answer keeps streaming. */
    finishCascadeStream() {
      const req = this.cascadeReq;
      if (!req) return;
      if (req.sink) {
        try { req.sink.close(); } catch (e) { /* an errored stream is already closed */ }
        req.sink = null;
        return;
      }
      if (req.buffered) {
        // The whole answer at once — this is the point the buffered path was
        // waiting for. A response with no text never posts at all.
        const lines = req.buffered;
        req.buffered = null;
        if (!lines.length) {
          this.cascadeReqs.delete(req);
          if (this.cascadeReq === req) this.cascadeReq = null;
          return;
        }
        let total = 0;
        for (let i = 0; i < lines.length; i++) total += lines[i].length;
        const body = new Uint8Array(total);
        let offset = 0;
        for (let i = 0; i < lines.length; i++) {
          body.set(lines[i], offset);
          offset += lines[i].length;
        }
        this.sendCascadeRequest(req, body, false);
      }
    }

    /** Barge-in or hang-up: every in-flight answer stops, server and speaker. */
    abortCascade() {
      const reqs = this.cascadeReqs;
      this.cascadeReqs = new Set();
      this.cascadeReq = null;
      reqs.forEach((req) => {
        if (req.sink) {
          try { req.sink.close(); } catch (e) { /* already closed */ }
        }
        // A buffered answer that never posted is dropped, not sent: the
        // operator interrupted it, so there is nothing left to speak.
        req.buffered = null;
        req.controller.abort();
      });
      this.stopPcmPlayback();
    }

    stopPcmPlayback() {
      for (let i = 0; i < this.pcmSources.length; i++) {
        try {
          this.pcmSources[i].stop();
        } catch (e) {
          /* a finished source throws on stop — that is the goal anyway */
        }
      }
      this.pcmSources = [];
      this.pcmNextTime = 0;
      this.pcmGeneration += 1;
      // Interpolation state belongs to the answer that was speaking. Left
      // behind, it would splice the end of an interrupted sentence onto the
      // start of the next one — the same seam click, one barge-in later.
      this.pcmPrev = null;
      this.pcmPos = 0;
    }

    /** PCM24k mono s16le off the wire onto the playback timeline. */
    async playCascadePcm(req, reader) {
      const generation = this.pcmGeneration;
      let pending = new Uint8Array(0);
      try {
        for (;;) {
          const step = await reader.read();
          if (step.done || !this.cascadeReqs.has(req)) break;
          const chunk = step.value;
          const joined = new Uint8Array(pending.length + chunk.length);
          joined.set(pending, 0);
          joined.set(chunk, pending.length);
          const even = joined.length - (joined.length % 2);  // s16le = 2 bytes/sample
          pending = joined.slice(even);
          if (even > 0) this.schedulePcm(joined.slice(0, even), generation);
        }
      } catch (e) {
        // An aborted fetch rejects the reader — the barge-in already spoke.
      }
      this.cascadeReqs.delete(req);
      if (this.cascadeReq === req) this.cascadeReq = null;
    }

    /**
     * PCM24k -> the context's rate, continuous ACROSS chunks.
     *
     * Carrying the previous chunk's last sample and the fractional read
     * position is what makes the stream one unbroken signal. Interpolating
     * each chunk in isolation is the original bug in a different costume.
     */
    resampleToContext(samples, rate) {
      const ratio = PCM_RATE / rate;
      const prev = this.pcmPrev === null ? samples[0] : this.pcmPrev;
      const n = samples.length;
      const at = (i) => (i === 0 ? prev : samples[i - 1]) / 32768;  // 0 = carried
      const out = [];
      let p = this.pcmPos;
      while (p < n) {
        const i = Math.floor(p);
        const frac = p - i;
        out.push(at(i) * (1 - frac) + at(i + 1) * frac);
        p += ratio;
      }
      this.pcmPrev = samples[n - 1];
      this.pcmPos = Math.max(0, p - n);
      return out;
    }

    /** Schedule one chunk after the last — gapless, in arrival order. */
    schedulePcm(bytes, generation) {
      if (generation !== this.pcmGeneration) return;  // decoded before a barge-in
      if (!this.pcmContext) {
        this.pcmContext = makePcmContext();
        if (!this.pcmContext) return;  // no playback surface — transcript still reads
        this.pcmNextTime = 0;
      }
      const ctx = this.pcmContext;
      const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.length / 2);
      if (!samples.length) return;
      let buffer;
      if (ctx.sampleRate === PCM_RATE) {
        // The common path: the context took the PCM's own rate, so the
        // browser resamples nothing and no seam can drift.
        buffer = pcmBuffer(ctx, samples, PCM_RATE, 32768);
      } else {
        const resampled = this.resampleToContext(samples, ctx.sampleRate);
        if (!resampled.length) return;
        buffer = pcmBuffer(ctx, resampled, ctx.sampleRate, 1);
      }
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      const at = Math.max(ctx.currentTime || 0, this.pcmNextTime);
      source.start(at);
      this.pcmNextTime = at + buffer.duration;
      this.pcmSources.push(source);
      if (this.pcmSources.length > 512) this.pcmSources.splice(0, 256);
    }

    /**
     * Relay one function call to the Python tool surface and feed the result
     * back. The session survives a tool failure — the model speaks the error
     * text instead of the call dying.
     */
    enqueueFunctionCall(event) {
      if (typeof event.call_id !== "string" || typeof event.name !== "string" ||
          !event.call_id || !event.name) return;
      if (!this.toolBatch) this.toolBatch = { done: false, results: [] };
      const batch = this.toolBatch;
      const position = batch.results.length;
      batch.results.push(null);
      this.toolTail = this.toolTail.then(async () => {
        batch.results[position] = await this.handleFunctionCall(event);
        this.flushToolResponse(batch);
      });
    }

    finishToolResponse() {
      if (!this.toolBatch) return;
      this.toolBatch.done = true;
      this.flushToolResponse(this.toolBatch);
    }

    flushToolResponse(batch) {
      if (this.toolBatch !== batch || !batch.done || batch.results.some((item) => !item)) return;
      for (let i = 0; i < batch.results.length; i++) this.send(batch.results[i].message);
      this.send({ type: "response.create" });
      for (let i = 0; i < batch.results.length; i++) this.watchForRun(batch.results[i].output);
      this.toolBatch = null;
    }

    async handleFunctionCall(event, binding) {
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
      let output, selection;
      try {
        const body = Object.assign({ name: name, arguments: args }, binding || {});
        const res = binding ? await this.task.request("/tool", body)
          : await apiPost("/tool", body, TOOL_TIMEOUT_MS);
        if (this.closed) return null;
        output = res && res.output ? String(res.output) : "(no output)";
        if (binding && res && res.ok !== false && res.selection) selection = res.selection;
      } catch (err) {
        if (binding) { this.task.report(errorText(err)); return null; }
        output = name + " failed: " + errorText(err);
      }
      return {
        output: output,
        selection: selection,
        message: {
          type: "conversation.item.create",
          item: { type: "function_call_output", call_id: callId, output: output },
        },
      };
    }

    /** Start polling if this text announced background work. */
    watchForRun(text) {
      if (this.task || this.closed) return;
      const started = WORK_STARTED_RE.exec(String(text || ""));
      if (started) this.pollRun(Number(started[1]), started[2]);
    }

    /**
     * The run finished somewhere else; say so. Injected as a user-role note so
     * the model speaks the result the moment it lands, unprompted. A finished
     * run can announce a follow-on run, so results are re-scanned.
     */
    pollRun(runId, kind) {
      if (this.task || this.closed) return;
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
          if (this.closed) return;
          window.setTimeout(tick, RUN_POLL_MS);
          return;
        }
        if (this.closed) return;
        if (!run || run.status === "running") {
          window.setTimeout(tick, RUN_POLL_MS);
          return;
        }
        if (this.responseActive || this.continuationPending || this.toolBatch) {
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

  function targetLabel(target) {
    return (target.label || target.target_id || "Unavailable target") + " · " + (target.kind || "task") +
      " · " + (target.host_label || "unavailable host") + " (" + (target.peer_id || "local") +
      ") / " + (target.profile || "unavailable profile");
  }

  function controlLabel(control) {
    if (!control) return "Control receipt unavailable";
    if (control.status === "queued" && control.source === "host_receipt" &&
        control.evidence === "backend_queue_ack")
      return "Queued to existing job · delivery and application unconfirmed";
    if (control.status === "rejected") return "Correction rejected · " + (control.evidence || "unavailable");
    if (control.status === "unsupported") return "Steering unavailable · " + (control.evidence || "unsupported");
    return "Correction unconfirmed · keep the original action";
  }

  function steeringLabel(steering) {
    return steering && steering.supported === true
      ? "Steering available for this running job"
      : "Steering unavailable · " + ((steering || {}).reason || "not refreshed");
  }

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
    const [profile, setProfile] = useState("");
    const [peerId, setPeerId] = useState("local");
    const [peers, setPeers] = useState([]);
    const [localProfiles, setLocalProfiles] = useState([]);
    const [unavailable, setUnavailable] = useState([]);
    const [tasks, setTasks] = useState([]);
    const [selectedTask, setSelectedTask] = useState("");
    const [catalogError, setCatalogError] = useState("");
    const [taskState, setTaskState] = useState(null);
    const [stages, setStages] = useState({});
    const [results, setResults] = useState({});
    const [typed, setTyped] = useState("");
    const [sending, setSending] = useState(false);
    const [switching, setSwitching] = useState(false);
    const [choices, setChoices] = useState([]);
    const [reference, setReference] = useState("");
    const [selection, setSelection] = useState(null);
    const [catalogReload, setCatalogReload] = useState(0);

    const transportRef = useRef(null);
    const connectionEpoch = useRef(0);
    const sessionAbort = useRef(null);
    const switchAbort = useRef(null);
    const switchEpoch = useRef(0);
    const switchInstallEpoch = useRef(null);
    const lastTask = useRef(null);
    const tabId = useRef(null);
    if (!tabId.current) tabId.current = taskTabId();
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
      const transport = transportRef.current;
      if (transport && transport.task) { await transport.task.refresh(); return; }
      try {
        const res = await apiCall("/runs");
        if (transportRef.current === transport) setRuns((res && res.runs) || []);
      } catch (e) {
        /* the runs panel is a status board — a failed poll is not a page error */
      }
    }, []);

    useEffect(() => {
      void refresh();
      const cleanup = () => {
        connectionEpoch.current++;
        if (sessionAbort.current) sessionAbort.current.abort();
        sessionAbort.current = null;
        switchEpoch.current++;
        if (switchAbort.current) switchAbort.current.abort();
        switchAbort.current = null;
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
      };
      window.addEventListener("pagehide", cleanup);
      return () => {
        cleanup();
        window.removeEventListener("pagehide", cleanup);
      };
    }, [refresh]);

    useEffect(() => {
      const controller = new AbortController();
      setTasks([]);
      setCatalogError("");
      setUnavailable([]);
      const catalogEpoch = connectionEpoch.current;
      const body = { peer_id: peerId, tab_id: tabId.current };
      if (profile.trim()) body.profile = profile.trim();
      void apiCall("/targets", { method: "POST", body: JSON.stringify(body), signal: controller.signal }).then((res) => {
        if (controller.signal.aborted) return;
        if (!res || !res.ok || !Array.isArray(res.targets)) throw new Error("Authorized target catalog is unavailable.");
        setTasks(res.targets);
        setPeers(res.peers || []);
        setUnavailable(res.unavailable || []);
        if (peerId === "local") setLocalProfiles((prev) => Array.from(new Set(prev.concat(res.targets.map((item) => item.profile)))).filter(Boolean));
        if (catalogEpoch === connectionEpoch.current && res.selection) {
          const current = res.selection.current;
          const activeTask = transportRef.current && transportRef.current.task;
          if (current && activeTask && (current.connection_id !== activeTask.context.connection_id ||
              current.generation !== activeTask.context.generation)) {
            connectionEpoch.current++;
            transportRef.current.stop();
            transportRef.current = null;
            setPhase("idle");
            setLive("");
            setTaskState(null);
            setTranscript([]);
            setStages({});
            setResults({});
            setError("Server selection changed. Rejoin or Return to continue.");
          }
          if (!transportRef.current) {
            lastTask.current = current || null;
            setSelection(res.selection);
            if (current) setSelectedTask(current.target_id);
          }
        }
      }).catch((err) => { if (!controller.signal.aborted) setCatalogError(errorText(err)); });
      return () => controller.abort();
    }, [peerId, profile, catalogReload]);

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

    async function installSession(session, epoch) {
      const current = () => epoch === connectionEpoch.current;
      const transport = new TalkTransport(session, {
        onStatus: (message) => { if (current()) setLive(message); },
        onTranscript: (role, text, final) => { if (current()) appendTranscript(role, text, final); },
        onError: (message) => { if (current()) setError(message); },
        onTaskState: (state) => { if (current()) setTaskState(state); },
        onTaskStage: (row) => { if (current()) setStages((prev) => Object.assign({}, prev, { [row.input_id]: row })); },
        onSelectionIntent: (intent, source) => current() && source === transportRef.current
          ? switchTarget(intent, source) : Promise.resolve(false),
      });
      transportRef.current = transport;
      lastTask.current = session.task || null;
      setSelection(session.selection || (session.task ? { return_depth: session.task.return_depth || 0 } : null));
      setTaskState(session.task ? { task: session.task, history: session.task.history, interactions: [], jobs: [] } : null);
      setTranscript([]);
      setStages({});
      setResults({});
      setRuns([]);
      setTyped("");
      setSending(false);
      setChoices([]);
      if (session.task && session.task.target_id) {
        setSelectedTask(session.task.target_id);
        if (session.task.peer_id) setPeerId(session.task.peer_id);
        if (session.task.profile) setProfile(session.task.profile);
      }
      await transport.start();
      if (current() && !transport.closed) setPhase("active");
    }

    async function startTalk() {
      setError("");
      if (typeof RTCPeerConnection === "undefined" || !navigator.mediaDevices) {
        setError("Talk needs a browser with WebRTC and microphone access.");
        return;
      }
      setPhase("starting");
      setLive("");
      setTranscript([]);
      setStages({});
      setResults({});
      setTaskState(null);
      const epoch = ++connectionEpoch.current;
      const controller = new AbortController();
      sessionAbort.current = controller;
      try {
        const body = voice ? { voice: voice } : {};
        if (selectedTask) body.task = { target_id: selectedTask, tab_id: tabId.current,
          page_reference: { url: window.location.href, title: document.title } };
        const session = await apiCall("/session", {
          method: "POST", body: JSON.stringify(body), signal: controller.signal,
        });
        if (epoch !== connectionEpoch.current || controller.signal.aborted) {
          new TalkTransport(session, {}).stop();
          return;
        }
        if (selectedTask && (!session.task || session.task.target_id !== selectedTask ||
            session.task.tab_id !== tabId.current)) {
          new TalkTransport(session, {}).stop();
          throw new Error("Bound task context did not match the selected task; join was refused.");
        }
        await installSession(session, epoch);
      } catch (err) {
        if (epoch !== connectionEpoch.current) return;
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
        setPhase("idle");
        setLive("");
        handleError(err);
      } finally {
        if (sessionAbort.current === controller) sessionAbort.current = null;
      }
    }

    function stopTalk() {
      connectionEpoch.current++;
      cancelSwitch(false);
      if (sessionAbort.current) sessionAbort.current.abort();
      sessionAbort.current = null;
      if (transportRef.current) transportRef.current.stop();
      transportRef.current = null;
      setPhase("idle");
      setLive("");
      setSending(false);
    }

    function cancelSwitch(showNotice = true) {
      switchEpoch.current++;
      if (switchAbort.current) switchAbort.current.abort();
      switchAbort.current = null;
      if (switchInstallEpoch.current !== null && switchInstallEpoch.current === connectionEpoch.current) {
        connectionEpoch.current++;
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
        setPhase("idle");
        setLive("");
      }
      switchInstallEpoch.current = null;
      setSwitching(false);
      if (showNotice) setError("Switch cancelled locally. If the server already activated it, reconcile or rejoin the selected target.");
    }

    async function switchTarget(intent, source) {
      const old = transportRef.current;
      if (source && source !== old) return false;
      const owner = old && old.task ? old.task.context : lastTask.current;
      if (!owner || !owner.connection_id || switchAbort.current) return false;
      const body = { connection_id: owner.connection_id, generation: owner.generation,
        page_reference: { url: window.location.href, title: document.title } };
      if (intent.back === true) body.back = true;
      else if (typeof intent.target_id === "string" && intent.target_id) body.target_id = intent.target_id;
      else if (typeof intent.reference === "string" && intent.reference.trim()) body.reference = intent.reference.trim();
      else { setError("Target selection is missing an authorized target or exact reference."); return false; }
      for (const key of ["peer_id", "profile"]) if (typeof intent[key] === "string" && intent[key]) body[key] = intent[key];
      const operation = ++switchEpoch.current;
      const controller = new AbortController();
      switchAbort.current = controller;
      setSwitching(true);
      setChoices([]);
      setError("");
      let installedEpoch = null;
      try {
        const session = await apiCall("/switch", {
          method: "POST", body: JSON.stringify(body), signal: controller.signal,
        });
        if (operation !== switchEpoch.current || controller.signal.aborted || transportRef.current !== old) {
          if (session && session.task) new TalkTransport(session, {}).stop();
          return false;
        }
        if (session && session.ok === false && session.state === "ambiguous") {
          setChoices(session.choices || []);
          setError("Choose the exact target; the current connection is unchanged.");
          return false;
        }
        if (!session || !session.task || !session.selection || session.selection.state !== "activated" ||
            session.selection.target_id !== session.task.target_id ||
            session.task.tab_id !== tabId.current ||
            (body.target_id && session.task.target_id !== body.target_id)) {
          if (session && session.task) new TalkTransport(session, {}).stop();
          throw new Error("Target switch was not authorized and activated.");
        }
        installedEpoch = ++connectionEpoch.current;
        switchInstallEpoch.current = installedEpoch;
        if (old) old.stop();
        transportRef.current = null;
        setPhase("starting");
        setLive("Connecting to " + targetLabel(session.task) + "…");
        await installSession(session, installedEpoch);
        return operation === switchEpoch.current && installedEpoch === connectionEpoch.current;
      } catch (err) {
        if (operation !== switchEpoch.current || controller.signal.aborted) return false;
        if (installedEpoch !== null && installedEpoch === connectionEpoch.current) {
          if (transportRef.current) transportRef.current.stop();
          transportRef.current = null;
          setPhase("idle");
          setLive("");
          setError("Target activated, but voice connection failed. Rejoin or Return. " + errorText(err));
        } else handleError(err);
        return false;
      } finally {
        if (switchAbort.current === controller) {
          switchAbort.current = null;
          switchInstallEpoch.current = null;
          setSwitching(false);
        }
      }
    }

    async function sendTyped() {
      const transport = transportRef.current;
      if (!transport || !typed.trim() || sending) return;
      const epoch = connectionEpoch.current;
      setSending(true);
      try {
        const sent = await transport.sendTyped(typed);
        if (epoch === connectionEpoch.current && sent) setTyped("");
      } catch (err) { if (epoch === connectionEpoch.current) handleError(err); }
      finally { if (epoch === connectionEpoch.current) setSending(false); }
    }

    async function showResult(runId) {
      const transport = transportRef.current;
      const epoch = connectionEpoch.current;
      if (!transport || !transport.task) return;
      try {
        const result = await transport.task.result(runId);
        if (epoch === connectionEpoch.current) setResults((prev) => Object.assign({}, prev, { [runId]: result }));
      } catch (err) { if (epoch === connectionEpoch.current) handleError(err); }
    }

    function saveToken() {
      writeToken(tokenDraft.trim());
      setTokenDraft("");
      void refresh();
    }

    const ready = Boolean(status && status.configured);
    const active = phase === "active";
    const starting = phase === "starting";
    const bound = Boolean((transportRef.current && transportRef.current.task) || lastTask.current);
    const returnDepth = Number((selection || {}).return_depth || 0);

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
            ? h(C.Button, { onClick: stopTalk }, starting ? "Cancel connection" : "Stop")
            : h(C.Button, { onClick: () => void startTalk(), disabled: !ready || loading },
                selectedTask ? "Join / resume task" : "Start legacy Talk")
        )
      ),

      h("div", { className: "ht-note" },
        h("label", { className: "ht-field" }, "Target source",
          h("select", { className: "ht-select", value: peerId, disabled: starting || switching, "aria-label": "Target source",
            onChange: (e) => { setPeerId(e.target.value); setProfile(e.target.value === "local" ? "" : "default"); setSelectedTask(""); } },
            h("option", { value: "local" }, "Local host"),
            peers.filter((peer) => peer.peer_id !== "local").map((peer) =>
              h("option", { key: peer.peer_id, value: peer.peer_id }, peer.label + " · " + peer.peer_id)))),
        h("label", { className: "ht-field" }, peerId === "local" ? "Local profile" : "Remote profile (explicit name)",
          peerId === "local" ? h("select", { className: "ht-select", value: profile, disabled: starting || switching, "aria-label": "Local profile",
            onChange: (e) => { setProfile(e.target.value); setSelectedTask(""); } },
            h("option", { value: "" }, "All authorized local profiles"),
            localProfiles.map((name) => h("option", { key: name, value: name }, name)))
            : h(C.Input, { value: profile, disabled: starting || switching, placeholder: "default", "aria-label": "Remote profile",
              onChange: (e) => { setProfile(e.target.value); setSelectedTask(""); } })),
        h("label", { className: "ht-field" }, "Task or Bot target",
          h("select", { className: "ht-select", value: selectedTask, disabled: starting || switching, "aria-label": "Task or Bot target",
            onChange: (e) => setSelectedTask(e.target.value) },
            h("option", { value: "", disabled: active }, "Legacy unbound Talk (no task history)"),
            tasks.map((task) => {
              const id = task.target_id;
              return id && h("option", { key: id, value: id,
                disabled: !(status && status.taskContinuity && status.taskContinuity.supported) },
                targetLabel(task));
            }))),
        h("div", { className: "ht-token-row" },
          bound && h(C.Button, { disabled: !selectedTask || starting || switching,
            onClick: () => void switchTarget({ target_id: selectedTask }) }, "Switch target"),
          bound && h(C.Button, { disabled: returnDepth < 1 || starting || switching,
            onClick: () => void switchTarget({ back: true }) }, "Return to previous (" + returnDepth + ")"),
          h(C.Button, { disabled: starting || switching,
            onClick: () => setCatalogReload((value) => value + 1) }, "Refresh targets / selection"),
          switching && h(C.Button, { onClick: () => cancelSwitch() }, "Cancel switch")),
        bound && h("form", { className: "ht-token-row", onSubmit: (event) => {
          event.preventDefault(); void switchTarget({ reference: reference, peer_id: peerId, profile: profile });
        } }, h(C.Input, { value: reference, disabled: starting || switching, placeholder: "Exact target name",
          "aria-label": "Target reference", onChange: (e) => setReference(e.target.value) }),
          h(C.Button, { type: "submit", disabled: !reference.trim() || starting || switching }, "Find and switch")),
        choices.length > 0 && h("div", { className: "ht-out" }, "Choose a target:",
          choices.map((choice) => h(C.Button, { key: choice.target_id, disabled: starting || switching,
            onClick: () => void switchTarget({ target_id: choice.target_id }) }, targetLabel(choice)))),
        selection && selection.current && !taskState && h("div", { className: "ht-out" },
          "Saved selection: " + targetLabel(selection.current)),
        selectedTask && h("div", { className: "ht-out" }, "Page reference: " + document.title + " · " + window.location.href),
        status && status.taskContinuity && !status.taskContinuity.supported &&
          h("div", null, "Task continuity unavailable: " + status.taskContinuity.reason),
        unavailable.map((item, index) => h("div", { key: index, className: "ht-out" },
          item.peer_id + " / " + item.profile + ": " + item.reason)),
        catalogError && h("div", { className: "ht-error" }, "Target list unavailable: " + catalogError)),

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
          // Tri-state string from the backend: "attached" | "api-server" |
          // "out of process". Rendered verbatim — it was a bool before, so a
          // truthiness test here would read "out of process" as attached.
          label: "Agent loop",
          value: (status && status.agentLoop) || "…",
        })
      ),

      (status && status.voiceMode) === "cascade"
        ? h("div", { className: "ht-field" },
            h("div", { className: "ht-note" },
              "Custom voice: the cascade speaks through ElevenLabs " +
              "(TALK_ELEVENLABS_VOICE_ID on the server); the provider voice " +
              "select sits out cascade sessions."))
        : h("label", { className: "ht-field" }, "Voice",
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

      taskState && h("section", { className: "ht-card" },
        h("div", { className: "ht-card-head" }, "Bound task and server context"),
        h("div", { className: "ht-row ht-text" },
          targetLabel(taskState.task || {}) + " · session: " + ((taskState.task || {}).session_id || "unavailable") + "\n" +
          JSON.stringify((taskState.task || {}).context || { state: "unavailable" }, null, 2)),
        h("div", { className: "ht-card-head" }, "Canonical task history"),
        !((taskState.history || {}).messages || []).length && h("div", { className: "ht-empty" }, "No canonical messages available."),
        ((taskState.history || {}).messages || []).map((row) => h("div", { key: row.id, className: "ht-row" },
          h("div", { className: "ht-role" }, row.role + " · " + row.id), h("div", { className: "ht-text" }, row.content))),
        (taskState.history || {}).truncated && h("div", { className: "ht-out" }, "Earlier canonical history is truncated."),
        h("div", { className: "ht-card-head" }, "Interactions and action receipts"),
        Object.values(stages).filter((row) => !(taskState.interactions || []).some((item) => item.input_id === row.input_id))
          .map((row) => h("div", { key: row.input_id, className: "ht-row" },
            h("div", { className: "ht-role" }, row.state + " · pending canonical receipt"),
            h("div", { className: "ht-text" }, row.text))),
        (taskState.interactions || []).map((row) => h("div", { key: row.id, className: "ht-row",
          id: "ht-interaction-" + encodeURIComponent(row.id) },
          h("div", { className: "ht-role" }, row.state + " · canonical: " + row.canonical_state),
          h("div", { className: "ht-text" }, row.text),
          (row.actions || []).filter((action) => action.name === "steer_work").map((action) =>
            h("div", { key: action.action_id, className: "ht-out" }, controlLabel(action.control) +
              " · action " + action.action_id + " · existing job " +
              ((action.control || {}).target_run_id || (action.control || {}).api_run_id || "unavailable"))),
          h("div", { className: "ht-out" }, JSON.stringify({ responses: row.responses, actions: row.actions,
            canonical_message_ids: row.canonical_message_ids }, null, 2)))),
        h("div", { className: "ht-card-head" }, "Observation / action log"),
        (taskState.events || {}).retention_gap && h("div", { className: "ht-row ht-out" },
          "Observation retention gap: earlier events are unavailable."),
        (taskState.events || {}).snapshot_refetch_required && h("div", { className: "ht-row ht-out" },
          "Snapshot refetch required: observations may be incomplete."),
        !((taskState.events || {}).events || []).length &&
          h("div", { className: "ht-empty" }, "No observations available."),
        ((taskState.events || {}).events || []).slice()
          .sort((left, right) => left.observed_index - right.observed_index).map((event) => {
            const origin = (taskState.interactions || []).find((row) => row.origin_turn_id === event.origin_turn_id);
            const job = (taskState.jobs || []).find((row) => row.action_id === event.action_id);
            const action = (taskState.interactions || []).find((row) =>
              (row.actions || []).some((item) => item.action_id === event.action_id));
            const originLink = event.origin_turn_id && origin ? "#ht-interaction-" + encodeURIComponent(origin.id) : null;
            const actionLink = event.action_id && job ? "#ht-job-" + encodeURIComponent(job.run_id)
              : event.action_id && action ? "#ht-interaction-" + encodeURIComponent(action.id) : null;
            const value = (entry) => entry == null ? "unavailable" : String(entry);
            return h("div", { key: event.observed_index, className: "ht-row" },
              h("div", { className: "ht-role" }, "Observed index: " + value(event.observed_index) +
                " · " + value(event.kind) + " · " + value(event.state)),
              event.label && h("div", { className: "ht-text" }, event.label),
              h("div", { className: "ht-out" }, "Source sequence: " + value(event.source_seq) +
                " · source epoch: " + value(event.source_epoch) +
                " · canonical revision: " + value(event.canonical_revision)),
              h("div", { className: "ht-out" }, "Origin: ",
                h(originLink ? "a" : "span", originLink ? { href: originLink } : null, value(event.origin_turn_id)),
                " · action: ", h(actionLink ? "a" : "span", actionLink ? { href: actionLink } : null, value(event.action_id))));
          }),
        h("div", { className: "ht-card-head" }, "Task jobs"),
        (taskState.jobs || []).map((job) => h("div", { key: job.run_id, className: "ht-row",
          id: "ht-job-" + encodeURIComponent(job.run_id) },
          h("div", { className: "ht-role" }, job.status + " · run " + job.run_id + " · action " + job.action_id),
          h("div", { className: "ht-text" }, job.goal),
          h("div", { className: "ht-out" }, steeringLabel(job.steering)),
          h("div", { className: "ht-out" }, "Approval: " + ((job.approval || {}).state || "unavailable")),
          job.result_available && h(C.Button, { onClick: () => void showResult(job.run_id), disabled: !active }, "View available result"),
          results[job.run_id] && h("div", { className: "ht-text" }, results[job.run_id].output),
          results[job.run_id] && results[job.run_id].truncated && h("div", { className: "ht-out" }, "Result truncated by transport.")))),

      h("form", { className: "ht-token-row", onSubmit: (event) => { event.preventDefault(); void sendTyped(); } },
        h(C.Input, { value: typed, disabled: !active || sending, placeholder: "Type to this Talk connection",
          "aria-label": "Typed input", onChange: (e) => setTyped(e.target.value) }),
        h(C.Button, { type: "submit", disabled: !active || sending || !typed.trim() }, sending ? "Staging…" : "Send")),

      h("div", { className: "ht-grid" },
        h("section", { className: "ht-card" },
          h("div", { className: "ht-card-head" }, taskState ? "Live captions (not canonical history)" : "Transcript"),
          transcript.length === 0
            ? h("div", { className: "ht-empty" }, "No transcript yet. Start a session and speak.")
            : transcript.map((row) =>
                h("div", { key: row.id, className: "ht-row" },
                  h("div", { className: "ht-role" },
                    (row.role === "user" ? "You" : "Hermes") + (row.final ? "" : " …")),
                  h("div", { className: "ht-text" }, row.text)))
        ),
        !taskState && h("section", { className: "ht-card" },
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

  if (window.__HERMES_TALK_TEST_HOOK__) {
    window.__HERMES_TALK_TEST__ = { TalkTransport: TalkTransport, TalkPage: TalkPage,
      controlLabel: controlLabel, steeringLabel: steeringLabel };
  }
  window.__HERMES_PLUGINS__.register("hermes-talk", TalkPage);
})();
