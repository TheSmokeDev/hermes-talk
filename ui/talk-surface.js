/**
 * hermes-talk — shared voice surface
 *
 * Browser voice sessions use WebRTC audio. Realtime uses an ephemeral secret
 * and relays function calls to the task coordinator. GPT-Live negotiates SDP
 * through Hermes; its server sideband owns delegation and publishes captions
 * and results to the browser. Long-lived provider credentials stay server-side.
 *
 * The host supplies React, hooks, components and authenticated JSON requests.
 * scripts/build_ui.py embeds this factory in each standalone surface.
 *
 * The minted client secret lives in the transport instance only. It is never
 * logged, never persisted, and never re-sent anywhere but OpenAI's offer URL.
 */
function createTalkSurface(SDK) {
  "use strict";

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
  const LIVE_POLL_MS = 250;
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
    const token = SDK.managedAuthentication ? "" : readToken();
    if (token) headers["x-talk-token"] = token;
    if (opts.body) headers["content-type"] = "application/json";
    opts.headers = headers;
    if (!timeoutMs) return SDK.fetchJSON(API + path, opts, timeoutMs);
    const controller = new AbortController();
    opts.signal = controller.signal;
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await SDK.fetchJSON(API + path, opts, timeoutMs);
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function validateVoiceMode(status) {
    const result = await SDK.validateVoiceMode(status);
    if (result === false || typeof result === "string") {
      throw new Error(typeof result === "string" ? result : "This voice mode is unavailable in this surface.");
    }
  }

  function releaseMicrophone(lease) {
    if (!lease) return;
    try { Promise.resolve(lease.release()).catch(() => {}); } catch (e) { /* already released */ }
  }

  function apiPost(path, body, timeoutMs) {
    return apiCall(path, { method: "POST", body: JSON.stringify(body || {}) }, timeoutMs);
  }

  function pageReference() {
    const url = window.location.href;
    if (!/^https?:\/\//i.test(url)) return undefined;
    return { url, title: document.title };
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

  class TaskSpeechTiming {
    constructor(task) {
      this.task = task;
      this.clock = () => Date.now();
      this.sequence = 0;
      this.speakingSince = null;
      this.nativePlaying = false;
      this.nativeResponseId = null;
      this.outputChangedAt = 0;
      this.awaiting = new Map();
      this.samples = {};
    }

    sample(kind, active) {
      const now = this.clock(), previous = this.samples[kind];
      this.samples[kind] = { at: now, active: active,
        voicedAt: active ? now : previous ? previous.voicedAt : -Infinity };
      if ((!previous || previous.active !== active) && this.task.transport.cb.onAudioActivity) {
        this.task.transport.cb.onAudioActivity(kind, active);
      }
    }

    quiet(kind) {
      const sample = this.samples[kind], now = this.clock();
      return sample && now - sample.at <= 1000 && !sample.active && now - sample.voicedAt >= 700;
    }

    observe(event) {
      const now = this.clock(), task = this.task;
      if (event.type === "input_audio_buffer.speech_started") this.speakingSince = now;
      if (event.type === "input_audio_buffer.speech_stopped") {
        this.speakingSince = null;
        if (!task.inputs.has(event.item_id)) this.awaiting.set(event.item_id || "unknown", now + 12000);
      }
      if (event.type === "input_audio_buffer.committed" && !task.inputs.has(event.item_id)) {
        this.awaiting.set(event.item_id || "unknown", now + 12000);
      }
      if (event.type === "conversation.item.input_audio_transcription.completed") {
        this.awaiting.delete(event.item_id); this.awaiting.delete("unknown");
      }
      if (event.type === "output_audio_buffer.started") {
        this.nativePlaying = true; this.nativeResponseId = event.response_id; this.outputChangedAt = now;
      }
      if (["output_audio_buffer.stopped", "output_audio_buffer.cleared"].includes(event.type) &&
          event.response_id === this.nativeResponseId) {
        this.nativePlaying = false; this.nativeResponseId = null; this.outputChangedAt = now;
      }
      const response = task.responses.get(event.response_id || (event.response || {}).id);
      if (response) response.last_at = now;
    }

    recover() {
      const now = this.clock(), task = this.task, transport = task.transport;
      if (this.speakingSince !== null && now - this.speakingSince >= 30000 && this.quiet("input")) {
        this.speakingSince = null;
        task.report("Recovered a missing speech-stop event after measured silence.");
      }
      if (this.nativePlaying && now - this.outputChangedAt >= 30000 && this.quiet("output")) {
        this.nativePlaying = false;
        task.report("Playback timing recovered after measured silence; delivery remains unconfirmed.");
      }
      for (const [id, expires] of this.awaiting) {
        if (now >= expires) { this.awaiting.delete(id); task.report("Input transcription timing expired."); }
      }
      let expired = false;
      for (const request of task.requests.values()) {
        if (!request.claimed && !request.row.incomplete && now - request.created_at >= 45000) {
          request.claimed = true; expired = true;
          task.incomplete(request.row, "response_failed");
        }
      }
      for (const response of task.responses.values()) {
        const tools = Array.from(response.calls.values()).some((call) => !call.done);
        if (!response.done && !response.row.incomplete && !tools && now - response.last_at >= 45000) {
          expired = true; task.incomplete(response.row, "response_failed");
          transport.send({ type: "response.cancel", response_id: response.id });
        }
      }
      if (expired) {
        transport.responseActive = Array.from(task.responses.values()).some((r) => !r.done && !r.row.incomplete);
        transport.continuationPending = Array.from(task.requests.values()).some((r) => !r.claimed && !r.row.incomplete);
      }
      const summary = task.presentation.active;
      if (summary && now - summary.last_at >= 30000 && !this.nativePlaying) task.presentation.interrupt();
    }

    snapshot() {
      this.recover();
      const task = this.task, transport = task.transport, now = this.clock();
      const active = Array.from(task.responses.values()).filter((response) => !response.row.incomplete);
      const output = this.samples.output;
      return {
        sequence: ++this.sequence,
        operator_speaking: this.speakingSince !== null ||
          Boolean(this.samples.input && now - this.samples.input.voicedAt < 700),
        playback_active: this.nativePlaying || Boolean(output && now - output.voicedAt < 700) ||
          transport.cascadeReqs.size > 0 || Boolean(transport.pcmContext &&
            transport.pcmContext.currentTime < transport.pcmNextTime),
        response_pending: transport.responseActive || transport.continuationPending ||
          active.some((response) => !response.done || !response.finished),
        input_pending: this.awaiting.size > 0 || Array.from(task.inputs.values()).some((row) =>
          !row.incomplete && !row.remembered),
        tools_pending: active.some((response) => Array.from(response.calls.values()).some((call) => !call.done)),
      };
    }
  }

  /** Synthetic summaries have no interaction, ordinary response, or tool authority. */
  class TaskPresentation {
    constructor(task) {
      this.task = task;
      this.pending = [];
      this.active = null;
      this.preparing = false;
      this.responses = new Set();
      this.cancelled = new Set();
      this.submitted = new Set();
      this.epoch = 0;
    }

    offer(state) {
      this.task.timing.recover();
      const replay = this.pending.filter((item) => item.replay);
      this.pending = replay.concat((state.announcements || []).filter((item) =>
        !this.submitted.has(item.event_id) && !replay.some((queued) => queued.event_id === item.event_id)))
        .slice(0, 8);
      void this.drain();
    }

    async replay(eventId) {
      if (this.task.closed || this.task.transport.closed || typeof eventId !== "string" || !eventId ||
          this.task.transport.live || this.pending.length >= 8 ||
          (this.active && this.active.event_id === eventId) ||
          this.pending.some((item) => item.event_id === eventId && item.replay)) return false;
      this.pending = [{ event_id: eventId, replay: true }].concat(
        this.pending.filter((item) => item.event_id !== eventId));
      await this.drain();
      return !this.task.closed;
    }

    idle() {
      const transport = this.task.transport;
      if (this.task.closed || transport.closed || !transport.channel || transport.channel.readyState !== "open") return false;
      const state = this.task.timing.snapshot();
      return !Object.keys(state).some((key) => key !== "sequence" && state[key]);
    }

    async drain() {
      if (this.preparing || this.active || !this.pending.length || !this.idle()) return;
      this.preparing = true;
      let prepared = null;
      const epoch = this.epoch;
      try {
        const next = this.pending.shift();
        const transport = this.task.transport;
        const playbackSupported = Boolean(transport.cascade && (transport.pcmContext ||
          window.AudioContext || window.webkitAudioContext));
        const reply = await this.task.request("/speech", { event_id: next.event_id,
          timing: this.task.timing.snapshot(), presentation_protocol: 1,
          playback_supported: playbackSupported, ...(next.replay ? { replay: true } : {}) });
        if (!reply.speak || this.task.closed) return;
        if (reply.event_id !== next.event_id || typeof reply.attempt_id !== "string" || !reply.attempt_id) {
          throw new Error("Task presentation did not identify the requested event.");
        }
        prepared = Object.assign({}, reply, { playback_supported: playbackSupported,
          last_at: this.task.timing.clock(), pcm_pending: 0 });
        if (epoch !== this.epoch || !this.idle()) {
          await this.receipt(prepared, "deferred");
          this.pending.unshift(next);
          return;
        }
        const response = prepared.response;
        if (!response || response.conversation !== "none" || response.tool_choice !== "none" ||
            !Array.isArray(response.tools) || response.tools.length || !Array.isArray(response.input) ||
            (response.metadata || {}).talk_presentation_id !== prepared.attempt_id ||
            response.metadata.talk_event_id !== prepared.event_id) {
          await this.receipt(prepared, "unknown");
          throw new Error("Invalid isolated task presentation.");
        }
        this.active = prepared;
        if (prepared.result && transport.cb.onTaskResult) transport.cb.onTaskResult(prepared.result);
        await this.receipt(prepared, "submitting");
        if (this.active !== prepared || epoch !== this.epoch || this.task.closed || !this.idle()) {
          if (this.active === prepared) {
            this.active = null;
            await this.receipt(prepared, "deferred");
            this.pending.unshift(next);
          }
          return;
        }
        prepared.dispatching = true;
        this.submitted.add(prepared.event_id);
        if (this.submitted.size > 128) this.submitted.delete(this.submitted.values().next().value);
        if (!transport.send({ type: "response.create", event_id: prepared.attempt_id,
          response: Object.assign({}, response, { output_modalities: [transport.cascade ? "text" : "audio"] }) })) {
          this.finish(prepared, "unknown");
          return;
        }
        await this.receipt(prepared, "context_submitted");
      } catch (err) {
        if (prepared && this.active === prepared) this.finish(prepared, prepared.dispatching ? "unknown" : "deferred");
        this.task.report(errorText(err));
      } finally { this.preparing = false; }
    }

    receipt(prepared, state) {
      const prior = prepared.receiptTail || Promise.resolve();
      const terminal = ["interrupted", "unknown", "deferred", "playback_finished"].includes(state);
      prepared.receiptTail = (terminal ? prior.catch(() => {}) : prior).then(async () => {
        const body = { event_id: prepared.event_id, attempt_id: prepared.attempt_id, state: state,
          ...(prepared.response_id ? { response_id: prepared.response_id } : {}) };
        // Retirement must survive local teardown, which immediately aborts task reads.
        const reply = terminal ? await apiCall("/speech/receipt", { method: "POST", keepalive: true,
          body: JSON.stringify(Object.assign(body, this.task.context)) })
          : await this.task.request("/speech/receipt", body);
        if (!reply || reply.ok !== true) throw new Error("Task presentation receipt was not accepted.");
        return reply;
      });
      return prepared.receiptTail;
    }

    finish(current, state) {
      if (this.active !== current) return;
      this.active = null;
      void this.receipt(current, state).catch((err) => this.task.report(errorText(err)))
        .finally(() => { void this.task.refresh(); });
    }

    playback(responseId, observation) {
      const current = this.active;
      if (!current || !current.playback_supported || current.response_id !== responseId || this.task.closed) return;
      current.last_at = this.task.timing.clock();
      if (observation === "scheduled") current.pcm_pending += 1;
      if (observation === "started" && !current.playback_started) {
        current.playback_started = true;
        void this.receipt(current, "playback_started").catch((err) => {
          this.task.report(errorText(err)); this.finish(current, "unknown");
        });
      }
      if (observation === "drained") current.pcm_pending = Math.max(0, current.pcm_pending - 1);
      if (observation === "stream_done") current.stream_done = true;
      if (observation === "failed") { this.finish(current, "unknown"); return; }
      this.completePlayback(current);
    }

    completePlayback(current) {
      if (current.generation_done && current.stream_done && !current.pcm_pending) {
        this.finish(current, current.playback_started ? "playback_finished" : "unknown");
      }
    }

    handle(event) {
      const response = event.response || {};
      const meta = response.metadata || {};
      const current = this.active;
      if (event.type === "error" && current && (event.error || {}).event_id === current.attempt_id) {
        this.interrupt();
        return false;
      }
      if (event.type === "response.created" && meta.talk_presentation_id) {
        if (this.cancelled.has(meta.talk_presentation_id)) {
          if (response.id) {
            this.responses.add(response.id);
            this.task.transport.send({ type: "response.cancel", response_id: response.id });
            this.task.transport.clearPlayback();
          }
          return true;
        }
        if (current && !current.response_id && meta.talk_presentation_id === current.attempt_id &&
            meta.talk_event_id === current.event_id && response.id) {
          current.response_id = response.id;
          current.last_at = this.task.timing.clock();
          this.responses.add(response.id);
          if (this.responses.size > 64) this.responses.delete(this.responses.values().next().value);
        } else {
          this.task.report("Unlinked task summary refused.");
          if (response.id) {
            this.responses.add(response.id);
            this.task.transport.send({ type: "response.cancel", response_id: response.id });
          }
        }
        return true;
      }
      const id = event.response_id || response.id;
      if (!id || !this.responses.has(id)) return false;
      // Late synthetic events can never be reclassified as ordinary dialogue.
      if (!current || id !== current.response_id) return true;
      current.last_at = this.task.timing.clock();
      const transport = this.task.transport;
      if (event.type === "response.function_call_arguments.done") {
        this.task.report("Task summaries cannot call tools.");
        return true;
      }
      if (["response.output_text.delta", "response.output_audio_transcript.delta"].includes(event.type)) {
        if (event.delta) transport.cb.onTranscript("assistant", event.delta, false);
        if (transport.cascade && event.delta && !current.text_done) transport.cascadeSend({ delta: event.delta }, id);
      }
      if (["response.output_text.done", "response.output_audio_transcript.done"].includes(event.type)) {
        const text = event.text || event.transcript || "";
        if (text) transport.cb.onTranscript("assistant", text, true);
        if (transport.cascade && !current.text_done) {
          current.text_done = true;
          transport.cascadeSend({ done: text }, id);
          transport.finishCascadeStream();
        }
      }
      if (event.type === "response.done") {
        current.generation_done = true;
        if (response.status !== "completed") {
          this.finish(current, current.playback_started ? "interrupted" : "unknown");
          transport.clearPlayback();
        } else if (!current.playback_supported || !current.text_done) this.finish(current, "unknown");
        else this.completePlayback(current);
        void this.task.refresh();
      }
      return true;
    }

    interrupt() {
      this.epoch += 1;
      if (!this.active) return;
      const current = this.active;
      this.cancelled.add(current.attempt_id);
      if (this.cancelled.size > 64) this.cancelled.delete(this.cancelled.values().next().value);
      this.finish(current, !current.dispatching ? "deferred" : current.playback_started ? "interrupted" : "unknown");
      this.task.transport.clearPlayback();
      if (current.response_id) this.task.transport.send({ type: "response.cancel", response_id: current.response_id });
    }
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
      this.timing = new TaskSpeechTiming(this);
      this.presentation = new TaskPresentation(this);
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
      const pending = this.transport.live
        ? this.transport.flushCapture().then(() => this.request("/state", {}))
        : this.request("/state", {});
      this.stateTail = pending.then((state) => {
        if (!this.closed && this.transport.cb.onTaskState) this.transport.cb.onTaskState(state);
        if (!this.closed && !this.transport.live) this.presentation.offer(state);
      }).catch((err) => this.report(errorText(err))).finally(() => { this.stateTail = null; });
      return this.stateTail;
    }

    async result(runId) {
      const query = "?connection_id=" + encodeURIComponent(this.context.connection_id) +
        "&generation=" + encodeURIComponent(this.context.generation) + "&run_id=" + encodeURIComponent(runId);
      // Results are inert UI data. Never inject them into the provider conversation.
      return this.request("/result" + query, null, "GET");
    }

    async preference(mode) {
      const result = await this.request("/preference", { mode: mode });
      await this.refresh();
      return result;
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
        requested: false, incomplete: false, items: [inputId], created_at: this.timing.clock() };
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
      this.presentation.interrupt();
      row.requested = true;
      const token = clientId("req_");
      const metadata = { talk_request_id: token, talk_interaction_id: row.receipt.interaction_id,
        talk_input_id: row.input_id, talk_previous_response_id: previous || "" };
      this.requests.set(token, { row: row, previous: previous || "", claimed: false, created_at: this.timing.clock() });
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
      if (this.responses.has(response.id) || request.row.incomplete) return;
      if (request.claimed) { this.incomplete(request.row, "linkage_ambiguous"); return; }
      request.claimed = true;
      const current = { id: response.id, row: request.row, previous: request.previous,
        calls: new Map(), declared: null, done: false, finished: false, last_at: this.timing.clock() };
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

    close(beforeRevoke) {
      if (this.closed) return;
      this.presentation.interrupt();
      this.presentation.pending = [];
      this.closed = true;
      this.controllers.forEach((controller) => controller.abort());
      this.controllers.clear();
      this.requests.clear();
      // Revocation is best effort; all local continuations are already fenced.
      const revoke = () => apiCall("/close", {
        method: "POST", body: JSON.stringify(this.context), keepalive: true }).catch(() => {});
      if (beforeRevoke) void beforeRevoke.then(revoke, revoke);
      else void revoke();
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
    if (source === "subscription") return "ChatGPT subscription";
    if (source === "api") return "OpenAI API";
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
      this.startAbort = null;
      this.microphoneLease = null;
      this.microphoneInvalidated = null;
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
      this.timingMeters = [];
      // One receipt per session for a relay that failed for a real reason.
      this.cascadeFailureLogged = false;
      this.task = session && session.task ? new TaskContinuity(this, session.task) : null;
    }

    async start() {
      if (typeof RTCPeerConnection === "undefined" || !navigator.mediaDevices) {
        throw new Error("This browser has no WebRTC or microphone access.");
      }
      this.closed = false;
      const controller = new AbortController();
      this.startAbort = controller;
      const current = () => !this.closed && this.startAbort === controller && !controller.signal.aborted;
      try {
        if (SDK.validateVoiceMode) {
          await validateVoiceMode(this.session);
          if (!current()) return;
        }
        if (SDK.acquireMicrophone) {
          const lease = await SDK.acquireMicrophone({ signal: controller.signal });
          if (!current()) { releaseMicrophone(lease); return; }
          if (!lease || typeof lease.release !== "function") {
            throw new Error("The host did not grant a microphone lease.");
          }
          this.microphoneLease = lease;
          if (lease.signal) {
            this.microphoneInvalidated = () => {
              if (this.closed) return;
              this.stop();
              if (!this.live && this.cb.onClosed) this.cb.onClosed();
            };
            lease.signal.addEventListener("abort", this.microphoneInvalidated, { once: true });
            if (lease.signal.aborted) { this.microphoneInvalidated(); return; }
          }
        }
        const peer = new RTCPeerConnection();
        this.peer = peer;

        this.audio = document.createElement("audio");
        this.audio.autoplay = true;
        this.audio.style.display = "none";
        document.body.appendChild(this.audio);
        peer.addEventListener("track", (event) => {
          const stream = event.streams[0];
          if (this.audio && stream) this.audio.srcObject = stream;
          if (stream && this.task) this.meter(stream, "output");
        });

        const media = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (!current()) {
          media.getTracks().forEach((track) => track.stop());
          return;
        }
        this.media = media;
        if (this.task) this.meter(media, "input");
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
            this.cb.onError((this.live ? "Live" : "Realtime") + " connection closed.");
            this.stop();
          }
        });

        const offer = await peer.createOffer();
        if (!current()) return;
        await peer.setLocalDescription(offer);
        if (!current()) return;
        const answer = await this.postOffer(offer);
        if (!current()) return;
        await peer.setRemoteDescription({ type: "answer", sdp: answer });
      } catch (err) {
        if (this.startAbort === controller) this.stop();
        throw err;
      }
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

    stop(beforeTaskClose) {
      this.closed = true;
      if (this.startAbort) this.startAbort.abort();
      this.startAbort = null;
      if (this.task) this.task.close(beforeTaskClose);
      // Teardown is idempotent and each step is guarded so a throw in one can
      // never skip the rest. (A throw in abortCascade() used to leave the
      // channel/peer open, so the server kept listening even though the UI
      // reset to idle.)
      if (this.offerAbort) {
        try { this.offerAbort.abort(); } catch (e) { /* already aborted */ }
        this.offerAbort = null;
      }
      for (const meter of this.timingMeters) {
        window.clearTimeout(meter.timer);
        try { meter.source.disconnect(); } catch (e) { /* already disconnected */ }
        try { Promise.resolve(meter.context.close()).catch(() => {}); } catch (e) { /* already closed */ }
      }
      this.timingMeters = [];
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
      const lease = this.microphoneLease;
      this.microphoneLease = null;
      if (lease && lease.signal && this.microphoneInvalidated) {
        lease.signal.removeEventListener("abort", this.microphoneInvalidated);
      }
      this.microphoneInvalidated = null;
      releaseMicrophone(lease);
    }

    meter(stream, kind) {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (!Context || !this.task || this.closed) return;
      let context;
      try {
        context = new Context();
        const source = context.createMediaStreamSource(stream), analyser = context.createAnalyser();
        analyser.fftSize = 256;
        source.connect(analyser); // Measurement only; no microphone loopback or recording.
        const meter = { context: context, source: source, timer: null };
        this.timingMeters.push(meter);
        const samples = new Uint8Array(analyser.fftSize);
        const tick = () => {
          if (this.closed || this.task.closed) return;
          if (context.state === "running") {
            analyser.getByteTimeDomainData(samples);
            let sum = 0;
            for (const value of samples) sum += Math.pow((value - 128) / 128, 2);
            this.task.timing.sample(kind, Math.sqrt(sum / samples.length) >= 0.02);
            void this.task.presentation.drain();
          }
          meter.timer = window.setTimeout(tick, 100);
        };
        tick();
      } catch (err) {
        if (context) { try { Promise.resolve(context.close()).catch(() => {}); } catch (e) { /* unavailable */ } }
        this.task.report("Audio timing measurement unavailable; recovery requires provider stop events.");
      }
    }

    clearPlayback() {
      if (this.cascade) this.abortCascade();
      else if (this.task) {
        this.send({ type: "output_audio_buffer.clear" });
        this.task.timing.nativePlaying = false;
      }
    }

    send(payload) {
      if (!this.closed && this.channel && this.channel.readyState === "open") {
        if (payload && payload.type === "response.create" &&
            (!payload.response || payload.response.conversation !== "none")) this.continuationPending = true;
        this.channel.send(JSON.stringify(payload));
        return true;
      }
      return false;
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
      if (this.task) this.task.timing.observe(event);
      if (this.task && this.task.presentation.handle(event)) return;
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
        case "output_audio_buffer.started":
        case "output_audio_buffer.stopped":
        case "output_audio_buffer.cleared":
          if (this.task) void this.task.presentation.drain();
          return;
        case "input_audio_buffer.speech_started":
          if (this.task) {
            this.task.presentation.interrupt();
            if (this.responseActive || this.task.timing.nativePlaying) this.clearPlayback();
          }
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
    startCascadeStream(responseId) {
      const req = { controller: new AbortController(), sink: null, buffered: null,
        response_id: responseId, pcm_generation: this.pcmGeneration };
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
            if (this.task) this.task.presentation.playback(req.response_id, "failed");
            this.cascadeReqs.delete(req);
            if (this.cascadeReq === req) this.cascadeReq = null;
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
            if (this.task) this.task.presentation.playback(req.response_id, "failed");
          }
          this.cascadeReqs.delete(req);
          if (this.cascadeReq === req) this.cascadeReq = null;
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

    cascadeSend(line, responseId) {
      // The previous response's relay may still be draining PCM — that is no
      // reason to drop THIS response's text; it opens its own stream.
      const open = this.cascadeReq && (this.cascadeReq.sink || this.cascadeReq.buffered);
      if (open && this.cascadeReq.response_id !== responseId) this.finishCascadeStream();
      if (!open || !this.cascadeReq || this.cascadeReq.response_id !== responseId) this.startCascadeStream(responseId);
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
      this.pcmGeneration += 1;
      const sources = this.pcmSources;
      this.pcmSources = [];
      for (const source of sources) {
        window.clearTimeout(source.talkPlaybackTimer);
        try {
          source.stop();
        } catch (e) {
          /* a finished source throws on stop — that is the goal anyway */
        }
      }
      this.pcmNextTime = 0;
      // Interpolation state belongs to the answer that was speaking. Left
      // behind, it would splice the end of an interrupted sentence onto the
      // start of the next one — the same seam click, one barge-in later.
      this.pcmPrev = null;
      this.pcmPos = 0;
    }

    /** PCM24k mono s16le off the wire onto the playback timeline. */
    async playCascadePcm(req, reader) {
      const generation = req.pcm_generation;
      let pending = new Uint8Array(0);
      let complete = false;
      try {
        for (;;) {
          const step = await reader.read();
          if (!this.cascadeReqs.has(req) || generation !== this.pcmGeneration) break;
          if (step.done) { complete = pending.length === 0; break; }
          const chunk = step.value;
          const joined = new Uint8Array(pending.length + chunk.length);
          joined.set(pending, 0);
          joined.set(chunk, pending.length);
          const even = joined.length - (joined.length % 2);  // s16le = 2 bytes/sample
          pending = joined.slice(even);
          if (even > 0) this.schedulePcm(joined.slice(0, even), generation, req.response_id);
        }
      } catch (e) {
        // An aborted fetch rejects the reader — the barge-in already spoke.
      }
      this.cascadeReqs.delete(req);
      if (this.cascadeReq === req) this.cascadeReq = null;
      if (this.task && generation === this.pcmGeneration && !req.controller.signal.aborted) {
        this.task.presentation.playback(req.response_id, complete ? "stream_done" : "failed");
      }
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
    schedulePcm(bytes, generation, responseId) {
      if (generation !== this.pcmGeneration) return;  // decoded before a barge-in
      if (!this.pcmContext) {
        this.pcmContext = makePcmContext();
        if (!this.pcmContext) {
          if (this.task) this.task.presentation.playback(responseId, "failed");
          return;
        }
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
      const current = () => !this.closed && generation === this.pcmGeneration;
      const observe = (state) => {
        if (current() && this.task) this.task.presentation.playback(responseId, state);
      };
      observe("scheduled");
      const measureStart = () => {
        const summary = this.task && this.task.presentation.active;
        if (!current() || !summary || summary.response_id !== responseId) return;
        if (ctx.state === "running" && ctx.currentTime > at) observe("started");
        else source.talkPlaybackTimer = window.setTimeout(measureStart, 25);
      };
      if (responseId && this.task) source.talkPlaybackTimer = window.setTimeout(measureStart, 25);
      let ended = false;
      source.onended = () => {
        if (ended) return;
        ended = true;
        window.clearTimeout(source.talkPlaybackTimer);
        const index = this.pcmSources.indexOf(source);
        if (index !== -1) this.pcmSources.splice(index, 1);
        if (!current()) return;
        if (ctx.currentTime >= at + buffer.duration) {
          observe("started");
          observe("drained");
        } else observe("failed");
      };
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

  function appendTranscriptRows(rows, event, id) {
    const role = event.role, text = event.text, final = event.final;
    const scope = event.finality, item = event.item_id;
    const rich = scope !== undefined || item !== undefined || event.event_id !== undefined;
    if (event.event_id && rows.some((row) => (row.event_ids || []).includes(event.event_id))) return rows;
    const contains = (outer, inner) => Number.isFinite(outer.start_ms) &&
      Number.isFinite(outer.end_ms) && Number.isFinite(inner.start_ms) &&
      Number.isFinite(inner.end_ms) && outer.start_ms <= inner.start_ms && outer.end_ms >= inner.end_ms;
    let indexes = [];
    if (rich) {
      indexes = rows.flatMap((row, index) => {
        if (row.role !== role) return [];
        const sameItem = typeof item === "string" && row.item_id === item;
        const coveredTurn = scope === "turn" && (row.fragments || []).length &&
          row.fragments.every((fragment) => contains(event, fragment));
        const lateObservation = scope !== "turn" && row.finality === "turn" && contains(row, event);
        return sameItem || coveredTurn || lateObservation ? [index] : [];
      });
    }
    if (!indexes.length && (!rich || !item)) {
      const last = rows[rows.length - 1];
      if (last && last.role === role && !last.final &&
          (!rich || (!last.item_id && last.finality !== "item"))) indexes = [rows.length - 1];
    }
    const previous = indexes.length ? rows[indexes[0]] : null;
    const observations = indexes.flatMap((index) => rows[index].fragments || []);
    const ids = indexes.flatMap((index) => rows[index].event_ids || []);
    const late = previous && previous.finality === "turn" && scope !== "turn" &&
      ((typeof item === "string" && previous.item_id === item) || contains(previous, event));
    const delta = rich ? scope === "delta" || (!scope && !final) : !final;
    const row = Object.assign({}, previous || { id, role, text: "" });
    if (!late) {
      row.text = delta && previous ? previous.text + text : text;
      row.final = rich ? scope === "turn" || (!scope && final) : final;
      if (rich) Object.assign(row, { item_id: item, finality: scope,
        start_ms: event.start_ms, end_ms: event.end_ms });
    }
    if (rich) {
      row.event_ids = ids.concat(event.event_id || []);
      row.fragments = observations.concat([{ event_id: event.event_id, text,
        item_id: item, finality: scope, start_ms: event.start_ms, end_ms: event.end_ms }]);
    }
    if (!indexes.length) return rows.concat([row]);
    return rows.flatMap((value, index) => index === indexes[0] ? [row] :
      indexes.includes(index) ? [] : [value]);
  }

  /** Live audio is browser WebRTC; every task action remains on the server. */
  class LiveTransport extends TalkTransport {
    constructor(session, callbacks) {
      super(session, callbacks);
      this.live = true;
      this.bindingId = null;
      this.liveCursor = 0;
      this.liveTimer = null;
      this.livePolling = false;
      this.operations = new Map();
    }

    async start() {
      if (this.closed) throw new Error("Live connection already closed.");
      if (!this.task || !this.task.context.connection_id ||
          !Number.isInteger(this.task.context.generation) ||
          !this.session.live || this.session.live.transport !== "webrtc") {
        throw new Error("GPT-Live requires an authorized task and WebRTC session.");
      }
      try {
        await super.start();
        if (this.closed) return;
        this.peer.addEventListener("connectionstatechange", () => {
          if (!this.closed && this.peer && this.peer.connectionState === "disconnected") {
            this.fail("Live audio disconnected. Rejoin the task to reconnect.");
          }
        });
        this.channel.addEventListener("close", () => {
          if (!this.closed) this.fail("Live connection closed. Rejoin the task to reconnect.");
        });
        this.channel.addEventListener("error", () => {
          if (!this.closed) this.fail("Live audio channel failed. Rejoin the task to reconnect.");
        });
        void this.pollEvents();
      } catch (err) {
        this.stop();
        throw err;
      }
    }

    async postOffer(offer) {
      const controller = new AbortController();
      this.offerAbort = controller;
      const timer = window.setTimeout(() => controller.abort(), OFFER_TIMEOUT_MS);
      let result;
      try {
        result = await apiCall("/live/session", { method: "POST",
          body: JSON.stringify(Object.assign({ sdp: offer.sdp }, this.task.context)),
          signal: controller.signal });
        if (this.closed || controller.signal.aborted) {
          if (result && typeof result.binding_id === "string") this.closeBinding(result.binding_id);
          throw new Error("Live connection setup cancelled.");
        }
        if (!result || result.ok !== true || typeof result.binding_id !== "string" ||
            !result.binding_id || typeof result.sdp !== "string" || !result.sdp) {
          if (result && typeof result.binding_id === "string") this.closeBinding(result.binding_id);
          throw new Error("Live WebRTC setup did not return a valid session.");
        }
        this.bindingId = result.binding_id;
        return result.sdp;
      } finally {
        window.clearTimeout(timer);
        if (this.offerAbort === controller) this.offerAbort = null;
      }
    }

    // Provider events cannot acquire execution authority through the browser.
    handleEvent() {}
    send() { return false; }
    clearPlayback() {}

    liveTiming() {
      const timing = this.task.timing, now = timing.clock();
      if (!["input", "output"].every((kind) => timing.samples[kind] &&
          now - timing.samples[kind].at <= 1000)) return null;
      return timing.snapshot();
    }

    async sendTyped(text) {
      if (this.closed || !this.bindingId || typeof text !== "string" || !text.trim()) return false;
      try {
        const result = await this.task.request("/live/input", {
          binding_id: this.bindingId, input_id: clientId("live_input_"), text: text,
          admission: "async" });
        if (!result || result.ok !== true) throw new Error("Live typed input was not accepted.");
        return true;
      } catch (err) {
        if (!this.closed) this.fail(errorText(err));
        throw err;
      }
    }

    async flushCapture() {
      if (this.closed || !this.bindingId) return;
      const reply = await this.task.request("/live/flush", { binding_id: this.bindingId });
      if (!reply || reply.ok !== true) throw new Error("Live transcript capture was not confirmed.");
    }

    async pollEvents() {
      if (this.closed || !this.bindingId || this.livePolling) return;
      this.livePolling = true;
      try {
        const reply = await this.task.request("/live/events", {
          binding_id: this.bindingId, after: this.liveCursor, timing: this.liveTiming() });
        if (!reply || reply.ok !== true || !Array.isArray(reply.events) ||
            !Number.isSafeInteger(reply.cursor) || reply.cursor < this.liveCursor) {
          throw new Error("Live event stream could not be reconciled. Rejoin the task.");
        }
        for (const event of reply.events) {
          if (this.closed) return;
          if (!event || !Number.isSafeInteger(event.sequence) || event.sequence < 1 ||
              event.sequence > reply.cursor) throw new Error("Invalid Live event sequence.");
          if (event.sequence <= this.liveCursor) continue;
          if (event.type === "transcript") {
            if (!["user", "assistant"].includes(event.role) || typeof event.text !== "string" ||
                typeof event.final !== "boolean") throw new Error("Invalid Live transcript event.");
            if ((event.finality !== undefined && !["delta", "item", "turn"].includes(event.finality)) ||
                (event.item_id !== undefined && typeof event.item_id !== "string") ||
                (event.event_id !== undefined && typeof event.event_id !== "string")) {
              throw new Error("Invalid Live transcript identity.");
            }
            this.cb.onTranscript(event.role, event.text, event.final, event);
          } else if (event.type === "operation") {
            if (typeof event.operation_id !== "string" || typeof event.pending !== "boolean" ||
                !["admitted", "deciding", "dispatching", "completed", "uncertain", "failed"].includes(event.state)) {
              throw new Error("Invalid Live operation receipt.");
            }
            this.operations.set(event.operation_id, { state: event.state, pending: event.pending });
            if (this.cb.onStatus) this.cb.onStatus(event.pending ? "Hermes is checking the request." :
              "Hermes returned a task decision; job status is shown separately.");
          } else if (event.type === "result") {
            const result = event.result || event;
            if (result.run_id !== undefined && this.cb.onTaskResult) {
              const full = event.result_available === true && result.output === undefined
                ? await this.task.result(result.run_id) : result;
              if (this.closed) return;
              this.cb.onTaskResult(full);
            }
            if (event.selection && this.cb.onSelectionIntent) {
              await this.cb.onSelectionIntent(event.selection, this);
              if (this.closed) return;
            }
            void this.task.refresh();
          } else if (event.type === "error") {
            throw new Error(typeof event.message === "string" ? event.message : "Live session failed.");
          }
          this.liveCursor = event.sequence;
        }
        this.liveCursor = reply.cursor;
      } catch (err) {
        if (!this.closed) this.fail(errorText(err));
      } finally {
        this.livePolling = false;
        if (!this.closed) this.liveTimer = window.setTimeout(() => {
          this.liveTimer = null;
          void this.pollEvents();
        }, LIVE_POLL_MS);
      }
    }

    fail(message) {
      if (this.closed) return;
      if (this.cb.onError) this.cb.onError(message);
      this.stop();
    }

    closeBinding(bindingId) {
      return apiCall("/live/close", { method: "POST", keepalive: true,
        body: JSON.stringify(Object.assign({ binding_id: bindingId }, this.task.context)) }).catch(() => {});
    }

    stop() {
      const wasClosed = this.closed;
      let beforeTaskClose;
      window.clearTimeout(this.liveTimer);
      this.liveTimer = null;
      if (this.bindingId) {
        beforeTaskClose = this.closeBinding(this.bindingId);
        this.bindingId = null;
      }
      if (this.audio) {
        try { this.audio.pause(); this.audio.srcObject = null; } catch (e) { /* already stopped */ }
      }
      super.stop(beforeTaskClose);
      if (!wasClosed && this.cb.onClosed) this.cb.onClosed();
    }
  }

  function makeTransport(session, callbacks) {
    return session && session.voiceMode === "live"
      ? new LiveTransport(session, callbacks) : new TalkTransport(session, callbacks);
  }

  // -- page -----------------------------------------------------------------

  function targetLabel(target) {
    return (target.label || target.target_id || "Unavailable target") + " · " + (target.kind || "task") +
      " · " + (target.host_label || "unavailable host") + " (" + (target.peer_id || "local") +
      ") / " + (target.profile || "unavailable profile");
  }

  function recipientIdentity(row) {
    if (!row) return null;
    const identity = {};
    for (const field of ["recipient_id", "app", "task_id", "host_id"]) {
      if (typeof row[field] !== "string" || !row[field]) throw new Error("Recipient identity is incomplete.");
      identity[field] = row[field];
    }
    return identity;
  }

  function sameRecipient(left, right) {
    return ["recipient_id", "app", "task_id", "host_id"].every(key => left?.[key] === right?.[key]);
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

  function TalkPage({ presentation, presentationProps } = {}) {
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
    const [profile, setProfile] = useState(SDK.desktopOwner?.profile || "");
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
    const [typed, updateTyped] = useState("");
    const [sending, setSending] = useState(false);
    const [switching, setSwitching] = useState(false);
    const [choices, setChoices] = useState([]);
    const [reference, setReference] = useState("");
    const [selection, setSelection] = useState(null);
    const [catalogReload, setCatalogReload] = useState(0);
    const [attachments, updateAttachments] = useState([]);
    const [inputError, setInputError] = useState("");
    const [actionReceipt, setActionReceipt] = useState(null);
    const [recipients, setRecipients] = useState([]);
    const [selectedRecipient, updateRecipient] = useState("");
    const [recipientOperation, updateOperation] = useState("message");
    const [recipientQuery, setRecipientQuery] = useState("");
    const [recipientHistory, setRecipientHistory] = useState(null);
    const [recipientSources, setRecipientSources] = useState([]);
    const [recipientLoading, setRecipientLoading] = useState(false);
    const [recipientError, setRecipientError] = useState("");
    const [selectedJob, setSelectedJob] = useState(null);
    const [pendingActions, setPendingActions] = useState({});
    const [muted, updateMuted] = useState(false);
    const [sleeping, updateSleeping] = useState(false);
    const [audioActivity, setAudioActivity] = useState({ input: false, output: false });
    const [appearance, updateAppearance] = useState({ skin: "system", animate: false,
      collapseOnConnect: true, hoverExpand: true });

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
    const draftRef = useRef({ text: "", files: [], revision: 0 });
    const sendingRef = useRef(false);
    const submissionRef = useRef(null);
    const captureSession = useRef(null);
    if (!captureSession.current) captureSession.current = clientId("typed_session_");
    const textSessionRef = useRef(null);
    const recipientEpoch = useRef(0);
    const recipientBusy = useRef(false);
    const confirmedRecipient = useRef(null);
    const actionLocks = useRef(new Set());
    const actionRequests = useRef(new Map());
    const latestTaskState = useRef(taskState);
    latestTaskState.current = taskState;
    const audioMode = useRef({ muted: false, sleeping: false });
    const mounted = useRef(true);
    const preferenceKey = "hermes-talk-appearance:" + JSON.stringify(SDK.desktopOwner
      ? [SDK.desktopOwner.connectionId, SDK.desktopOwner.profile,
        SDK.desktopOwner.sessionId, SDK.desktopOwner.storedSessionId]
      : [tabId.current, peerId, profile, selectedTask]);

    function setTyped(text) {
      draftRef.current = { ...draftRef.current, text, revision: draftRef.current.revision + 1 };
      updateTyped(text);
    }

    function addAttachments(files) {
      const added = Array.from(files || []);
      const current = draftRef.current.files;
      if (current.length + added.length > 8 || added.some(file =>
        !file || typeof file.name !== "string" || !Number.isFinite(file.size) || file.size < 0 ||
        file.size > 10 * 1024 * 1024 || typeof file.slice !== "function") ||
        [...current, ...added].reduce((total, file) => total + file.size, 0) > 20 * 1024 * 1024) {
        setInputError("Choose up to 8 files, at most 10 MiB each and 20 MiB together.");
        return false;
      }
      const next = current.concat(added.map(file => ({ id: clientId("file_"), file,
        name: file.name, type: file.type || "", size: file.size,
        previewUrl: ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(file.type) &&
          typeof URL !== "undefined" && URL.createObjectURL ? URL.createObjectURL(file) : undefined })));
      draftRef.current = { ...draftRef.current, files: next, revision: draftRef.current.revision + 1 };
      updateAttachments(next);
      setInputError("");
      return true;
    }

    function clearSentDraft() {
      for (const file of draftRef.current.files) if (file.previewUrl) URL.revokeObjectURL(file.previewUrl);
      draftRef.current = { text: "", files: [], revision: draftRef.current.revision + 1 };
      updateTyped("");
      updateAttachments([]);
    }

    function removeAttachment(id) {
      const old = draftRef.current.files.find(file => file.id === id);
      if (old?.previewUrl) URL.revokeObjectURL(old.previewUrl);
      const next = draftRef.current.files.filter(file => file.id !== id);
      draftRef.current = { ...draftRef.current, files: next, revision: draftRef.current.revision + 1 };
      updateAttachments(next);
    }

    // The floating-window behaviors default on; a saved object without them keeps the default.
    const appearanceRecord = (value) => ({ skin: value.skin, animate: value.animate,
      collapseOnConnect: value.collapseOnConnect !== false, hoverExpand: value.hoverExpand !== false });

    function setAppearance(next) {
      if (!["system", "quiet", "contrast"].includes(next?.skin) || typeof next.animate !== "boolean") return;
      const record = appearanceRecord(next);
      updateAppearance(record);
      try { window.localStorage.setItem(preferenceKey, JSON.stringify(record)); } catch (e) { /* storage unavailable */ }
    }

    useEffect(() => {
      try {
        const saved = JSON.parse(window.localStorage.getItem(preferenceKey));
        if (["system", "quiet", "contrast"].includes(saved?.skin) && typeof saved.animate === "boolean") {
          updateAppearance(appearanceRecord(saved));
        }
      } catch (e) { /* appearance is optional */ }
    }, [preferenceKey]);

    function applyAudioMode() {
      const transport = transportRef.current;
      if (!transport) return;
      const mode = audioMode.current;
      transport.media?.getAudioTracks().forEach(track => { track.enabled = !mode.muted && !mode.sleeping; });
      if (transport.audio) transport.audio.muted = mode.sleeping;
    }

    function setMuted(value) {
      audioMode.current.muted = value === true;
      updateMuted(value === true);
      applyAudioMode();
    }

    function setSleeping(value) {
      audioMode.current.sleeping = value === true;
      updateSleeping(value === true);
      applyAudioMode();
    }

    const handleError = useCallback((err) => {
      if (isAuthError(err)) setNeedsToken(true);
      setError(errorText(err));
    }, []);

    const refresh = useCallback(async () => {
      setLoading(true);
      try {
        const res = await apiCall("/status");
        if (!mounted.current) return;
        setStatus(res);
        setVoice((current) => current || res.voice || "");
        setNeedsToken(false);
        setError("");
      } catch (err) {
        if (!mounted.current) return;
        setStatus(null);
        handleError(err);
      } finally {
        if (mounted.current) setLoading(false);
      }
    }, [handleError]);

    const refreshRuns = useCallback(async () => {
      const transport = transportRef.current;
      if (transport && transport.task) {
        await transport.task.refresh();
        // Pending typed operations settle through this poll whether or not the
        // transport is text-only: an owner message admitted during a voice
        // session names an operation on a transport whose textOnly is false.
        if (transport.typedOperations?.size > 0) await pollTypedOperations(transport);
        return;
      }
      try {
        const res = await apiCall("/runs");
        if (transportRef.current === transport) setRuns((res && res.runs) || []);
      } catch (e) {
        /* the runs panel is a status board — a failed poll is not a page error */
      }
    }, []);

    useEffect(() => {
      mounted.current = true;
      void refresh();
      const cleanup = () => {
        mounted.current = false;
        connectionEpoch.current++;
        recipientEpoch.current++;
        if (sessionAbort.current) sessionAbort.current.abort();
        sessionAbort.current = null;
        switchEpoch.current++;
        if (switchAbort.current) switchAbort.current.abort();
        switchAbort.current = null;
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
        for (const file of draftRef.current.files) if (file.previewUrl) URL.revokeObjectURL(file.previewUrl);
      };
      window.addEventListener("pagehide", cleanup);
      SDK.lifetimeSignal?.addEventListener("abort", cleanup, { once: true });
      if (SDK.lifetimeSignal?.aborted) cleanup();
      return () => {
        cleanup();
        window.removeEventListener("pagehide", cleanup);
        SDK.lifetimeSignal?.removeEventListener("abort", cleanup);
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
      if (SDK.desktopOwner) {
        if (!SDK.desktopOwner.storedSessionId) return () => controller.abort();
        body.session_id = SDK.desktopOwner.storedSessionId;
      }
      void apiCall("/targets", { method: "POST", body: JSON.stringify(body), signal: controller.signal }).then((res) => {
        if (controller.signal.aborted) return;
        if (!res || !res.ok || !Array.isArray(res.targets)) throw new Error("Authorized target catalog is unavailable.");
        setTasks(res.targets);
        setPeers(res.peers || []);
        setUnavailable(res.unavailable || []);
        if (SDK.desktopOwner && !transportRef.current) {
          const matches = res.targets.filter((item) => item.peer_id === "local" &&
            item.profile === SDK.desktopOwner.profile &&
            item.session_id === SDK.desktopOwner.storedSessionId);
          if (matches.length !== 1) throw new Error("The current conversation is unavailable. Reopen it and try again.");
          setSelectedTask(matches[0].target_id);
        }
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
            if (current && !SDK.desktopOwner) setSelectedTask(current.target_id);
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
        timer = window.setTimeout(loop, ["active", "text"].includes(phaseRef.current) ? RUN_POLL_MS : IDLE_POLL_MS);
      };
      void loop();
      return () => {
        cancelled = true;
        window.clearTimeout(timer);
      };
    }, [refreshRuns]);

    const appendTranscript = useCallback((role, text, final, metadata) => {
      setTranscript((prev) => appendTranscriptRows(prev,
        Object.assign({}, metadata || {}, { role, text, final }), rowId.current++));
    }, []);

    async function installSession(session, epoch, textOnly = false) {
      const current = () => epoch === connectionEpoch.current;
      const transport = makeTransport(session, {
        onStatus: (message) => { if (current()) setLive(message); },
        onAudioActivity: (kind, value) => {
          if (current()) setAudioActivity(previous => ({ ...previous, [kind]: value }));
        },
        onTranscript: (role, text, final, metadata) => {
          if (current()) appendTranscript(role, text, final, metadata);
        },
        onError: (message) => { if (current()) setError(message); },
        onClosed: () => { if (current()) { setPhase("idle"); setLive(""); setSending(false); } },
        onTaskState: (state) => {
          if (!current()) return;
          setTaskState(state);
          const addressed = state.recipients?.selected;
          if (!recipientBusy.current && addressed && !sameRecipient(addressed, confirmedRecipient.current)) {
            recipientEpoch.current++;
            confirmedRecipient.current = recipientIdentity(addressed);
            updateRecipient(addressed.recipient_id);
            setRecipients(rows => rows.filter(row => row.recipient_id !== addressed.recipient_id).concat(addressed));
            setRecipientHistory(null);
          }
        },
        onTaskResult: (result) => {
          if (current()) setResults((prev) => Object.assign({}, prev, { [result.run_id]: result }));
        },
        onTaskStage: (row) => { if (current()) setStages((prev) => Object.assign({}, prev, { [row.input_id]: row })); },
        onSelectionIntent: (intent, source) => current() && source === transportRef.current
          ? switchTarget(intent, source) : Promise.resolve(false),
      });
      transportRef.current = transport;
      transport.textOnly = textOnly;
      if (textOnly) transport.typedOperations = new Map();
      lastTask.current = session.task || null;
      setSelection(session.selection || (session.task ? { return_depth: session.task.return_depth || 0 } : null));
      setTaskState(session.task ? { task: session.task, history: session.task.history, interactions: [], jobs: [] } : null);
      setTranscript([]);
      setStages({});
      setResults({});
      setRuns([]);
      setAudioActivity({ input: false, output: false });
      if (!textOnly) setSending(false);
      setChoices([]);
      if (session.task && session.task.target_id) {
        setSelectedTask(session.task.target_id);
        if (session.task.peer_id) setPeerId(session.task.peer_id);
        if (session.task.profile) setProfile(session.task.profile);
      }
      if (textOnly) {
        setPhase("text");
        phaseRef.current = "text";
        await transport.task.refresh();
      } else {
        await transport.start();
        applyAudioMode();
        if (current() && !transport.closed) setPhase("active");
      }
    }

    async function startTalk() {
      if (!["idle", "text"].includes(phaseRef.current) || textSessionRef.current || sendingRef.current) return;
      setError("");
      if (typeof RTCPeerConnection === "undefined" || !navigator.mediaDevices) {
        setError("Talk needs a browser with WebRTC and microphone access.");
        return;
      }
      if (status && status.voiceMode === "live" && !selectedTask && !SDK.prepareTask) {
        setError("Choose an authorized task before starting GPT-Live.");
        return;
      }
      setPhase("starting");
      phaseRef.current = "starting";
      setLive("");
      setTranscript([]);
      setStages({});
      setResults({});
      setTaskState(null);
      const epoch = ++connectionEpoch.current;
      if (transportRef.current) transportRef.current.stop();
      transportRef.current = null;
      const controller = new AbortController();
      sessionAbort.current = controller;
      try {
        let targetId = selectedTask;
        if (SDK.prepareTask) {
          const target = await SDK.prepareTask({ tabId: tabId.current, signal: controller.signal });
          if (epoch !== connectionEpoch.current || controller.signal.aborted) return;
          if (!target?.target_id) throw new Error("The current conversation could not be prepared.");
          targetId = target.target_id;
          setSelectedTask(targetId);
          setTasks((rows) => rows.filter((row) => row.target_id !== targetId).concat(target));
          setCatalogError("");
        }
        if (SDK.validateVoiceMode) {
          await validateVoiceMode(status);
          if (epoch !== connectionEpoch.current || controller.signal.aborted) return;
        }
        const body = voice ? { voice: voice } : {};
        if (targetId) body.task = { target_id: targetId, tab_id: tabId.current,
          page_reference: pageReference() };
        const session = await apiCall("/session", {
          method: "POST", body: JSON.stringify(body), signal: controller.signal,
        });
        if (epoch !== connectionEpoch.current || controller.signal.aborted) {
          makeTransport(session, {}).stop();
          return;
        }
        if (targetId && (!session.task || session.task.target_id !== targetId ||
            session.task.tab_id !== tabId.current)) {
          makeTransport(session, {}).stop();
          throw new Error("Bound task context did not match the selected task; join was refused.");
        }
        await installSession(session, epoch);
      } catch (err) {
        if (epoch !== connectionEpoch.current) return;
        if (transportRef.current) transportRef.current.stop();
        transportRef.current = null;
        setPhase("idle");
        phaseRef.current = "idle";
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
      phaseRef.current = "idle";
      setLive("");
      setSending(false);
      sendingRef.current = false;
      textSessionRef.current = null;
      if (SDK.stopHost) SDK.stopHost();
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
      if (SDK.desktopOwner) {
        setError("Stop Talk before opening a different voice owner. Addressing another recipient does not change the owner.");
        return false;
      }
      const old = transportRef.current;
      if (source && source !== old) return false;
      const owner = old && old.task ? old.task.context : lastTask.current;
      if (!owner || !owner.connection_id || switchAbort.current) return false;
      const body = { connection_id: owner.connection_id, generation: owner.generation,
        page_reference: pageReference() };
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
        if (SDK.validateVoiceMode) await validateVoiceMode(status);
        if (operation !== switchEpoch.current || controller.signal.aborted || transportRef.current !== old) return false;
        if (old && old.live) await old.flushCapture();
        if (operation !== switchEpoch.current || controller.signal.aborted || transportRef.current !== old) {
          return false;
        }
        const session = await apiCall("/switch", {
          method: "POST", body: JSON.stringify(body), signal: controller.signal,
        });
        if (operation !== switchEpoch.current || controller.signal.aborted || transportRef.current !== old) {
          if (session && session.task) makeTransport(session, {}).stop();
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
          if (session && session.task) makeTransport(session, {}).stop();
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

    const inputCapabilities = status?.textInput?.version === 1 ? status.textInput : null;

    async function ensureTextBinding() {
      const existing = transportRef.current;
      if (existing?.task && !existing.closed) return existing;
      if (!mounted.current || SDK.lifetimeSignal?.aborted) {
        throw new Error("The Talk owner is no longer available.");
      }
      if (phaseRef.current === "starting" || switchAbort.current) throw new Error("Wait for the current connection.");
      if (textSessionRef.current) return textSessionRef.current;
      const epoch = connectionEpoch.current;
      const controller = new AbortController();
      sessionAbort.current = controller;
      const pending = (async () => {
        let targetId = selectedTask;
        if (SDK.prepareTask) {
          const target = await SDK.prepareTask({ tabId: tabId.current, signal: controller.signal });
          if (controller.signal.aborted || epoch !== connectionEpoch.current) throw new Error("Connection cancelled.");
          targetId = target?.target_id;
          setSelectedTask(targetId || "");
        }
        if (!targetId) throw new Error("Choose an authorized task for microphone-off input.");
        const session = await apiCall("/native/attach", { method: "POST", signal: controller.signal,
          body: JSON.stringify({ input_mode: "typed", surface: SDK.desktopOwner ? "desktop" : "dashboard",
            target_id: targetId, tab_id: tabId.current }) });
        if (controller.signal.aborted || epoch !== connectionEpoch.current) {
          if (session?.task) makeTransport(session, {}).stop();
          throw new Error("Connection cancelled.");
        }
        if (session?.ok !== true || session.input_mode !== "typed" || session.task?.target_id !== targetId ||
            session.task.tab_id !== tabId.current || typeof session.task.connection_id !== "string" ||
            !Number.isSafeInteger(session.task.generation)) {
          if (session?.task) makeTransport(session, {}).stop();
          throw new Error("Microphone-off task binding did not match the selected owner.");
        }
        await installSession(session, epoch, true);
        return transportRef.current;
      })();
      textSessionRef.current = pending;
      try { return await pending; }
      finally {
        if (textSessionRef.current === pending) textSessionRef.current = null;
        if (sessionAbort.current === controller) sessionAbort.current = null;
      }
    }

    async function refreshRecipients() {
      if (recipientBusy.current) return;
      const epoch = connectionEpoch.current;
      const request = ++recipientEpoch.current;
      recipientBusy.current = true;
      setRecipientLoading(true);
      setRecipientError("");
      try {
        const transport = await ensureTextBinding();
        if (epoch !== connectionEpoch.current || request !== recipientEpoch.current) return;
        const response = await transport.task.request("/recipients/catalog", { limit: 20 });
        if (epoch !== connectionEpoch.current || request !== recipientEpoch.current) return;
        if (response?.ok !== true || !Array.isArray(response.recipients) || response.recipients.length > 50) {
          throw new Error("Recipient catalog is unavailable.");
        }
        const ids = new Set();
        for (const row of response.recipients) {
          recipientIdentity(row);
          if (ids.has(row.recipient_id) || !Array.isArray(row.operations)) throw new Error("Recipient catalog has ambiguous identities.");
          ids.add(row.recipient_id);
        }
        setRecipients(response.recipients);
        setRecipientSources(response.sources || []);
        const selected = response.recipients.find(row => sameRecipient(row, confirmedRecipient.current));
        if (!selected) {
          confirmedRecipient.current = null;
        }
      } catch (err) { if (epoch === connectionEpoch.current) setRecipientError(errorText(err)); }
      finally {
        recipientBusy.current = false;
        if (epoch === connectionEpoch.current) setRecipientLoading(false);
      }
    }

    async function setRecipient(id) {
      if (recipientBusy.current) return false;
      // Owner addressing is the default, not a host recipient: clearing it is
      // local state only. Bumping the epoch drops receipts still in flight for
      // the recipient being left. The voice owner is untouched either way.
      if (!id) {
        recipientEpoch.current++;
        confirmedRecipient.current = null;
        updateRecipient("");
        setSelectedJob(null);
        updateOperation("message");
        setRecipientHistory(null);
        setRecipientError("");
        return true;
      }
      const row = recipients.find(item => item.recipient_id === id);
      if (!row || row.available === false) return false;
      const target = recipientIdentity(row);
      const epoch = connectionEpoch.current;
      const request = ++recipientEpoch.current;
      recipientBusy.current = true;
      setRecipientLoading(true);
      setRecipientError("");
      setRecipientHistory(null);
      try {
        const transport = await ensureTextBinding();
        if (epoch !== connectionEpoch.current || request !== recipientEpoch.current) return false;
        const response = await transport.task.request("/recipients/select", { ...target, action_id: clientId("select_") });
        if (epoch !== connectionEpoch.current || request !== recipientEpoch.current) return false;
        if (response?.ok !== true || !sameRecipient(response.recipient, target)) {
          throw new Error("Recipient selection was not confirmed. Refresh before sending.");
        }
        confirmedRecipient.current = target;
        updateRecipient(id);
        setSelectedJob(null);
        updateOperation(row.read_only ? "read" : "message");
        return true;
      } catch (err) {
        if (epoch === connectionEpoch.current) {
          confirmedRecipient.current = null;
          setRecipientError(errorText(err));
        }
        return false;
      } finally {
        recipientBusy.current = false;
        if (epoch === connectionEpoch.current) setRecipientLoading(false);
      }
    }

    function setRecipientOperation(operation) {
      if (!["read", "message", "start_worker", "steer"].includes(operation)) return;
      recipientEpoch.current++;
      updateOperation(operation);
      setActionReceipt(null);
    }

    async function readRecipient() {
      const row = recipients.find(item => item.recipient_id === selectedRecipient);
      if (!row?.operations.includes("history") || row.available === false) return;
      const target = recipientIdentity(row);
      const epoch = connectionEpoch.current, request = ++recipientEpoch.current;
      setRecipientHistory(null);
      setRecipientError("");
      try {
        const transport = await ensureTextBinding();
        if (epoch !== connectionEpoch.current || request !== recipientEpoch.current) return;
        const response = await transport.task.request("/recipients/history", { ...target, limit: 20 });
        if (epoch !== connectionEpoch.current || request !== recipientEpoch.current) return;
        if (response?.ok !== true || !sameRecipient(response, target) || !Array.isArray(response.messages) ||
            response.messages.length > 50 || response.messages.some(message =>
              !["user", "assistant"].includes(message.role) || typeof message.text !== "string")) {
          throw new Error("History did not match the addressed recipient.");
        }
        setRecipientHistory(response);
      } catch (err) {
        if (epoch === connectionEpoch.current && request === recipientEpoch.current) setRecipientError(errorText(err));
      }
    }

    const selectedRecipientRow = recipients.find(row => row.recipient_id === selectedRecipient);
    const selectedJobRow = taskState?.jobs?.find(job => job.run_id === selectedJob);
    const operationSupported = inputCapabilities?.operations?.includes(recipientOperation) === true;
    /**
     * Owner text only leaves /live/typed for /text/input when the descriptor
     * actually declares "message". A descriptor that omits it (cancel/approval
     * only) routes owner text exactly as an absent descriptor does.
     */
    const ownerMessageSupported = inputCapabilities?.operations?.includes("message") === true;
    /**
     * Files ride only where the plugin says an upload adapter exists AND the
     * operation is the one whose dispatch reaches a child. Anything else keeps the
     * existing behavior: the files stay local and Send is disabled with a reason.
     */
    const attachmentsSupported = inputCapabilities?.attachments === true;
    const attachmentsSendable = attachmentsSupported && recipientOperation === "start_worker";
    const recipientCanSend = selectedRecipientRow?.send_agent_message === "direct" &&
      selectedRecipientRow.proven_control !== "none" && selectedRecipientRow.read_only !== true &&
      selectedRecipientRow.available !== false && sameRecipient(selectedRecipientRow, confirmedRecipient.current);
    const legacyText = !selectedRecipient && recipientOperation === "message" &&
      transportRef.current && !transportRef.current.textOnly && phase === "active";
    const ownerText = !selectedRecipient && recipientOperation === "message" &&
      Boolean(SDK.prepareTask || selectedTask);
    const canSendTyped = !switching && !recipientLoading && !sending &&
      (attachments.length === 0 || attachmentsSendable) &&
      (legacyText || ownerText || operationSupported && (recipientOperation === "start_worker" ||
        recipientOperation === "steer" && selectedJobRow?.steering?.supported === true ||
        recipientOperation === "message" && (!selectedRecipient || recipientCanSend)));

    function base64Of(buffer) {
      const bytes = new Uint8Array(buffer);
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 0x8000) {
        binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + 0x8000));
      }
      return btoa(binary);
    }

    /**
     * Upload happens on Send, never while editing. Each file goes to the plugin's
     * own adapter — never the credentialed host endpoint — and comes back as an
     * opaque reference pinned to this input. A failure throws before /text/input is
     * called, so a partial upload sends nothing; the retry reuses the same input_id,
     * and the already-stored files replay their receipts instead of storing twice.
     */
    async function uploadAttachments(transport, files, inputId) {
      const references = [];
      for (const entry of files) {
        const reply = await transport.task.request("/attachments/upload", {
          input_id: inputId, filename: entry.name,
          content_type: entry.type || "application/octet-stream",
          bytes_base64: base64Of(await entry.file.arrayBuffer()),
        });
        if (reply?.ok !== true || reply.input_id !== inputId ||
            typeof reply.attachment_id !== "string" || typeof reply.sha256 !== "string") {
          throw new Error("An attachment upload was not confirmed. Nothing was sent.");
        }
        references.push({ attachment_id: reply.attachment_id, sha256: reply.sha256 });
      }
      return references;
    }

    async function sendTyped() {
      const draft = { ...draftRef.current, files: [...draftRef.current.files] };
      if (!draft.text.trim() || sendingRef.current || !canSendTyped ||
          (transportRef.current?.typedOperations?.size || 0) >= 8) return false;
      const epoch = connectionEpoch.current, addressedEpoch = recipientEpoch.current;
      const recipient = selectedRecipientRow ? recipientIdentity(selectedRecipientRow) : null;
      const operation = recipientOperation, runId = selectedJob;
      const current = () => epoch === connectionEpoch.current && mounted.current;
      sendingRef.current = true;
      setSending(true);
      setInputError("");
      let sent = false;
      try {
        const transport = legacyText && !ownerMessageSupported ? transportRef.current : await ensureTextBinding();
        if (!current()) return false;
        if (legacyText && !ownerMessageSupported) {
          sent = await transport.sendTyped(draft.text);
        } else if (ownerText && !ownerMessageSupported) {
          const signature = JSON.stringify([draft.revision, transport.task.context]);
          if (submissionRef.current?.signature !== signature) submissionRef.current = { signature,
            body: { provider_session_id: captureSession.current, input_id: clientId("typed_"),
              text: draft.text, admission: "async" } };
          const body = submissionRef.current.body;
          const receipt = await transport.task.request("/live/typed", body);
          if (receipt?.ok !== true || typeof receipt.operation_id !== "string" || !receipt.operation_id ||
              !["admitted", "deciding", "dispatching", "completed", "uncertain", "failed"].includes(receipt.state)) {
            throw new Error("Typed admission is unconfirmed. Retry this unchanged input to reconcile it.");
          }
          if (!transport.typedOperations) transport.typedOperations = new Map();
          transport.typedOperations.set(receipt.operation_id, body.input_id);
          if (current() && addressedEpoch === recipientEpoch.current) setActionReceipt({ ...receipt, operation: "message" });
          sent = !["uncertain", "failed"].includes(receipt.state);
          if (current()) await pollTypedOperations(transport);
        } else {
          const signature = JSON.stringify([draft.revision, operation, recipient, runId, transport.task.context]);
          if (submissionRef.current?.signature !== signature) submissionRef.current = { signature,
            files: attachmentsSendable ? draft.files : [], uploaded: false,
            body: { input_id: clientId("typed_"), text: draft.text, operation, attachments: [],
              ...(recipient && operation === "message" ? { recipient } : {}),
              ...(operation === "steer" ? { run_id: runId } : {}) } };
          const pending = submissionRef.current;
          if (pending.files?.length && !pending.uploaded) {
            // The captured send owns these exact files; the references complete it.
            pending.body.attachments = await uploadAttachments(
              transport, pending.files, pending.body.input_id);
            pending.uploaded = true;
            if (!current()) return false;
          }
          const body = pending.body;
          const receipt = await transport.task.request("/text/input", body);
          if (receipt?.ok !== true || receipt.input_id !== body.input_id || receipt.operation !== operation ||
              (body.recipient && !sameRecipient(receipt.recipient, body.recipient)) || typeof receipt.state !== "string") {
            throw new Error("Input receipt was not confirmed. Inspect the task before retrying.");
          }
          if (current() && addressedEpoch === recipientEpoch.current) setActionReceipt(receipt);
          sent = ["queued", "posted", "accepted", "completed", "saved"].includes(receipt.state);
          if (!sent && current()) setInputError("Input delivery is " + receipt.state + ". Inspect the task before retrying.");
          // An owner message admitted asynchronously settles through the same /live/operation
          // polling the provider-free typed path uses; the reply names the operation to watch.
          if (sent && typeof receipt.operation_id === "string" && receipt.operation_id) {
            if (!transport.typedOperations) transport.typedOperations = new Map();
            transport.typedOperations.set(receipt.operation_id, body.input_id);
            if (current()) await pollTypedOperations(transport);
          }
        }
        if (current() && addressedEpoch === recipientEpoch.current && sent && draft.revision === draftRef.current.revision) {
          clearSentDraft();
        }
        return sent;
      } catch (err) { if (current()) setInputError(errorText(err)); return false; }
      finally {
        sendingRef.current = false;
        if (current()) setSending(false);
      }
    }

    async function pollTypedOperations(transport) {
      if (!transport.typedOperations?.size || transport.typedPolling) return;
      transport.typedPolling = true;
      const epoch = connectionEpoch.current;
      try {
        for (const [operationId, inputId] of [...transport.typedOperations].slice(0, 8)) {
          const query = "?connection_id=" + encodeURIComponent(transport.task.context.connection_id) +
            "&generation=" + encodeURIComponent(transport.task.context.generation) +
            "&operation_id=" + encodeURIComponent(operationId);
          const reply = await transport.task.request("/live/operation" + query, null, "GET");
          if (epoch !== connectionEpoch.current || transport !== transportRef.current) return;
          if (reply?.ok !== true || reply.operation_id !== operationId || typeof reply.pending !== "boolean") {
            throw new Error("Typed operation receipt did not match this input.");
          }
          setActionReceipt({ ...reply, input_id: inputId, operation: "message" });
          if (!reply.pending) {
            transport.typedOperations.delete(operationId);
            if (reply.result?.run_id != null) {
              setResults(rows => ({ ...rows, [reply.result.run_id]: reply.result }));
            } else if (typeof reply.result?.output === "string") {
              appendTranscript("assistant", reply.result.output, true, { event_id: operationId });
            }
          }
        }
      } catch (err) { if (epoch === connectionEpoch.current) setInputError(errorText(err)); }
      finally { transport.typedPolling = false; }
    }

    function steerJob(runId) {
      const job = latestTaskState.current?.jobs?.find(row => row.run_id === runId);
      if (!job || job.steering?.supported !== true || !inputCapabilities?.operations?.includes("steer")) return;
      setSelectedJob(runId);
      setRecipientOperation("steer");
    }

    async function submitJobAction(key, operation, fields, text) {
      if (!inputCapabilities?.operations?.includes(operation) || actionLocks.current.has(key)) return false;
      const epoch = connectionEpoch.current;
      actionLocks.current.add(key);
      setPendingActions(rows => ({ ...rows, [key]: { pending: true, error: "" } }));
      try {
        const transport = await ensureTextBinding();
        if (epoch !== connectionEpoch.current) return false;
        const signature = JSON.stringify([fields, transport.task.context]);
        let request = actionRequests.current.get(key);
        if (request?.signature !== signature) {
          request = { signature, body: { ...fields, input_id: clientId("action_"), operation, text, attachments: [] } };
          actionRequests.current.set(key, request);
        }
        if (request.completed) return true;
        const body = request.body;
        const receipt = await transport.task.request("/text/input", body);
        if (epoch !== connectionEpoch.current) return false;
        if (receipt?.ok !== true || receipt.input_id !== body.input_id || receipt.operation !== operation ||
            !["queued", "posted", "accepted", "completed", "saved"].includes(receipt.state)) {
          throw new Error("Action is unconfirmed. Refresh the original task before trying again.");
        }
        setActionReceipt(receipt);
        request.completed = true;
        setPendingActions(rows => ({ ...rows, [key]: { pending: false, error: "" } }));
        await transport.task.refresh();
        return true;
      } catch (err) {
        if (epoch === connectionEpoch.current) setPendingActions(rows => ({ ...rows,
          [key]: { pending: false, error: errorText(err) } }));
        return false;
      } finally { actionLocks.current.delete(key); }
    }

    function cancelJob(runId) {
      const job = latestTaskState.current?.jobs?.find(row => row.run_id === runId);
      if (!job || !["queued", "accepted", "running", "waiting_for_approval"].includes(job.status)) return false;
      return submitJobAction("cancel:" + runId, "cancel", { run_id: runId, action_id: job.action_id }, "Cancel this job.");
    }

    function answerApproval(answer) {
      const job = latestTaskState.current?.jobs?.find(row => row.run_id === answer.run_id && row.action_id === answer.action_id);
      const pending = job?.approval?.approvals?.find(row => row.request_id === answer.request_id);
      if (job?.approval?.actionable !== true || !pending?.choices?.includes(answer.choice)) return false;
      return submitJobAction("approval:" + answer.request_id, "approval", { run_id: answer.run_id,
        action_id: answer.action_id, request_id: answer.request_id, choice: answer.choice }, answer.choice);
    }

    async function replayResult(eventId) {
      const transport = transportRef.current;
      const job = taskState?.jobs?.find(row => row.presentation?.event_id === eventId && row.presentation.replay_eligible);
      if (!transport?.task || transport.textOnly || transport.live || !job || actionLocks.current.has("replay:" + eventId)) return;
      const epoch = connectionEpoch.current, key = "replay:" + eventId;
      actionLocks.current.add(key);
      setPendingActions(rows => ({ ...rows, [key]: { pending: true, error: "" } }));
      try {
        const played = await transport.task.presentation.replay(eventId);
        if (!played) throw new Error("Summary is not ready to replay. Try again in a quiet turn.");
        if (epoch === connectionEpoch.current) setPendingActions(rows => ({ ...rows, [key]: { pending: false, error: "" } }));
      } catch (err) {
        if (epoch === connectionEpoch.current) setPendingActions(rows => ({ ...rows, [key]: { pending: false, error: errorText(err) } }));
      } finally { actionLocks.current.delete(key); }
    }

    async function showResult(runId) {
      // Opening a stored result is a read. It must never mint a binding, so a
      // click without one is inert and the control says why instead.
      const transport = transportRef.current;
      const epoch = connectionEpoch.current;
      if (!transport?.task) return;
      try {
        const result = await transport.task.result(runId);
        if (epoch === connectionEpoch.current) setResults((prev) => Object.assign({}, prev, { [runId]: result }));
      } catch (err) { if (epoch === connectionEpoch.current) handleError(err); }
    }

    async function saveUpdatePreference(mode) {
      const transport = transportRef.current;
      const epoch = connectionEpoch.current;
      if (!transport || !transport.task) return;
      try { await transport.task.preference(mode); }
      catch (err) { if (epoch === connectionEpoch.current) handleError(err); }
    }

    function saveToken() {
      writeToken(tokenDraft.trim());
      setTokenDraft("");
      setCatalogReload((value) => value + 1);
      void refresh();
    }

    const ready = Boolean(status && status.configured);
    const active = phase === "active";
    const starting = phase === "starting";
    const bound = Boolean((transportRef.current && transportRef.current.task) || lastTask.current);
    const returnDepth = Number((selection || {}).return_depth || 0);

    if (presentation) return h(presentation, {
      ...presentationProps,
      status, loading, ready, active, starting, live, error, catalogError, needsToken,
      tasks, selectedTask, taskState, transcript, results, typed, sending, switching,
      returnDepth, bound, voice, startTalk, stopTalk, refresh, setTyped, sendTyped,
      switchTarget, showResult, saveUpdatePreference, setVoice,
      voiceOwner: SDK.desktopOwner, recipients, selectedRecipient, recipientOperation,
      setRecipient, setRecipientOperation, recipientQuery, setRecipientQuery, recipientHistory,
      readRecipient, refreshRecipients, recipientLoading, recipientSources, recipientError,
      attachments, addAttachments, removeAttachment, attachmentsSupported,
      canSendTyped: Boolean(canSendTyped), inputCapabilities, inputError, actionReceipt,
      replaySupported: Boolean(transportRef.current && !transportRef.current.live && !transportRef.current.textOnly),
      resultsReadable: Boolean(transportRef.current?.task),
      selectedJob, pendingActions, cancelJob, steerJob, answerApproval, replayResult,
      muted, sleeping, setMuted, setSleeping, appearance, setAppearance,
      audioActivity: { input: !muted && !sleeping && active && audioActivity.input,
        output: !sleeping && active && audioActivity.output },
      refreshCatalog: () => { setCatalogReload((value) => value + 1); void refresh(); },
    });

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
            : h(C.Button, { onClick: () => void startTalk(), disabled: !ready || loading ||
                (status.voiceMode === "live" && !selectedTask) },
                selectedTask ? "Join / resume task" : status && status.voiceMode === "live"
                  ? "Start GPT-Live" : "Start legacy Talk")
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
            h("option", { value: "", disabled: active }, status && status.voiceMode === "live"
              ? "Choose a task for GPT-Live" : "Legacy unbound Talk (no task history)"),
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
        h("label", { className: "ht-row" }, "Spoken updates for this task ",
          h("select", { value: (taskState.preferences || {}).update_mode || "important",
            disabled: !active, "aria-label": "Task update frequency",
            onChange: (event) => void saveUpdatePreference(event.target.value) },
          h("option", { value: "important" }, "Completion and important updates"),
          h("option", { value: "completion" }, "Completion only"),
          h("option", { value: "frequent" }, "Include meaningful milestones"))),
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
          results[job.run_id] && h("div", null,
            h("div", { className: "ht-role" }, "Full available result · " + results[job.run_id].status),
            results[job.run_id].error && h("div", { className: "ht-out" }, String(results[job.run_id].error)),
            h("div", { className: "ht-text" }, results[job.run_id].output || "No result detail was supplied."),
            (Array.isArray(results[job.run_id].artifacts) ? results[job.run_id].artifacts : []).map((artifact, index) =>
              h("div", { key: index },
                h("div", { className: "ht-role" }, "Artifact changes"),
                h("pre", { className: "ht-out" }, JSON.stringify(artifact, null, 2))))),
          results[job.run_id] && results[job.run_id].truncated && h("div", { className: "ht-out" }, "Result truncated by transport.")))),

      h("form", { className: "ht-token-row", onSubmit: (event) => { event.preventDefault(); void sendTyped(); } },
        h(C.Input, { value: typed, disabled: sending || switching, placeholder: "Type to this Talk connection",
          "aria-label": "Typed input", onChange: (e) => setTyped(e.target.value) }),
        h(C.Button, { type: "submit", disabled: !canSendTyped || !typed.trim() }, sending ? "Staging…" : "Send")),
      inputError && h("div", { role: "alert", className: "ht-error" }, inputError),

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

  return { TalkTransport: TalkTransport, LiveTransport: LiveTransport,
    makeTransport: makeTransport, TalkPage: TalkPage, appendTranscriptRows: appendTranscriptRows,
    controlLabel: controlLabel, steeringLabel: steeringLabel };
}
