# Grok (xAI) provider for hermes-talk — spec

Status: approved direction, pre-implementation. 2026-08-28.
Owner repo: TheSmokeDev/hermes-talk. No core (NousResearch/hermes-agent) edits.

## Why now

- hermes-talk is the only shipping realtime voice transport in the Hermes
  ecosystem. The core interface PR (kvnloo's #95147) is interface-only, a
  draft, with zero maintainer review; his xAI adapter has never touched the
  live API (FakeSocket tests only).
- xAI's Grok Voice Agent API is live, documented, and OpenAI-Realtime-shaped:
  `wss://api.x.ai/v1/realtime`, custom function calling, 5 voices, PCM
  8-48kHz. Nobody in the ecosystem has shipped a working provider for it.
- Verified live 2026-08-28 with the operator key: REST `/v1/models` 200, and
  the realtime endpoint returned `HTTP/1.1 101 Switching Protocols` for
  `model=grok-voice-latest`. Access is real, not theoretical.

## Verified upstream facts (docs.x.ai, 2026-08-28)

- WebSocket only. No WebRTC offer endpoint — the dashboard lane must relay
  through our backend (Phase 2; terminal + Discord first).
- Region: `us-east-1` only.
- Voices: `ara` (default, F warm), `rex` (M confident), `sal` (neutral),
  `eve` (F energetic), `leo` (M authoritative).
- Audio: PCM Linear16 configurable 8-48kHz; G.711 u-law/A-law for telephony.
- Tool calling: OpenAI-style custom function schemas, plus xAI built-ins
  (web_search, x_search, collections) that we do NOT advertise by default.
- Ephemeral secrets for browser use ride as the `xai-client-secret.` WS
  subprotocol (no header auth possible from browsers). Not needed for
  Phase 1; recorded for the dashboard relay design.

## Design

Provider selection is a new call-time knob. Everything else reuses the
existing contract — the whole point of `talk_realtime.py` was this PR.

### Provider selection (talk_config.py)

- `TALK_PROVIDER` = `openai` (default) | `grok`. Call-time resolved,
  fail-closed `TalkConfigError` on any other value. Never infer a provider
  from which key happens to be set — an operator with both keys must get the
  provider they asked for or an error, never a silent switch.
- `talk_provider() -> str` alongside `talk_model()`/`talk_voice()`.

### New module: talk_grok_realtime.py

Mirrors `talk_openai_realtime.py` against the same `rt.RealtimeSession`
Protocol (`connect(SessionSetup)`, `send(commands)`, `__aiter__` events,
`close()`):

- `XAI_REALTIME_WS_URL = "wss://api.x.ai/v1/realtime"`
- `GrokRealtimeSession` + `_GrokWireSession` owning the socket.
- Session payload builder translating `rt.SessionSetup` (model, voice,
  instructions, tools, automatic_response) into the xAI `session.update`
  shape: server VAD with `interrupt_response`, PCM 24kHz in/out, tool list,
  instructions. Start from the OpenAI payload and diff against xAI docs —
  "compatible" is a claim, the event vocabulary diff is an implementation
  task, not an assumption.
- Event translation xAI -> contract: `SpeechStarted/Stopped`, `OutputAudio`
  (with `response_id`/`item_id` identity), `Transcript` (input + output
  provenance), `FunctionCall`, `ResponseFinished`, `ProviderFailure`,
  `SessionTerminated`. Where xAI event names differ from OpenAI GA names,
  translate; do not leak provider vocabulary past the module.
- Commands: `AppendInputAudio`, `StartResponse`, `CancelResponse`,
  `SubmitToolResult` map directly. `TruncateOutput`: xAI claims truncation
  support (kvnloo's reference assumes truncation-before-cancel); verify live
  and, if absent, degrade to cancel-only with a logged receipt — never fake
  a truncation that did not happen.
- Tail-audio drop by `response_id` ledger: same as the OpenAI lane.

### Auth (talk_auth.py + talk_config.py)

- `resolve_xai_key()`: `TALK_XAI_API_KEY` -> `XAI_API_KEY`, fail-closed,
  set-but-blank is a hard refusal (same rule as `resolve_openai_key()`).
  No OAuth lane exists for xAI; API key only. Key is never logged; receipts
  carry source name only.
- Provider-aware auth resolution at session mint: provider=grok requires the
  xAI lane; provider=openai keeps today's chain unchanged.

### Config (talk_config.py)

- `GROK_REALTIME_VOICES = ("ara", "rex", "sal", "eve", "leo")`
- `DEFAULT_GROK_MODEL = "grok-voice-latest"` (handshake-verified 2026-08-28;
  re-confirm against docs at implementation).
- `TALK_GROK_MODEL`, `TALK_GROK_VOICE` knobs; `talk_voice()` becomes
  provider-aware, fail-closed against the active provider's voice list.

### Wiring

- `talk_cli.py::_openai_session` becomes a provider switch:
  `_realtime_session(auth, provider)`. Terminal lane inherits Grok with no
  other changes.
- Discord lane (`/voice join` then `/talk join`) inherits via the same
  session factory; `DiscordAudio` resampling already handles 24kHz mono.
- Dashboard lane: Phase 2, separate PR — backend WS relay, since xAI has no
  WebRTC offer URL.
- `talk doctor`: grok lane shows key presence (redacted), provider selection,
  model/voice validity. Optional deep probe (`TALK_DOCTOR_PROBE=1`) performs
  the WS upgrade handshake and reports 101/non-101.

## Testing

- Fake-socket contract tests for the Grok session mirroring
  `tests/fake_realtime.py`: payload shape, event translation, barge-in
  ordering, tool round-trip, truncation degrade path, fail-closed auth.
- Provider-selection tests: unknown provider refuses; both-keys-set uses
  `TALK_PROVIDER`; set-but-blank `TALK_XAI_API_KEY` refuses.
- Live smoke: `scripts/grok_smoke.py`, gated behind `TALK_GROK_LIVE=1`,
  never runs in CI. One scripted exchange + one tool call + one barge-in.
- Full suite green on ubuntu + windows; `plugin-guard.yml` stays clean.

## Security / scanner notes

- `.env.example` gains `XAI_API_KEY=` (blank). Docs must not place a host
  and `$HERMES_HOME`-style variable on one line (plugin_guard FP class from
  0.10.1 — keep the gate green).
- No xAI payloads with realistic-looking secrets in tests; fixtures go in
  `tests/fixtures/*.fixture` if ever needed.

## Non-goals (this PR)

- Gemini Live provider (separate PR; proprietary protocol, bigger lift).
- Custom-voice cascade (ElevenLabs/streaming TTS) — separate feature.
- Dashboard lane for Grok (Phase 2 PR).
- xAI built-in tools (web_search/x_search/collections) — not advertised.
- Changing the default provider away from `openai`.
- Any change to the operator-auth ledger, runs registry, or identity chain.

## Acceptance

1. `TALK_PROVIDER=grok hermes talk` — live terminal call against xAI with
   the operator key; assistant speaks, hears, answers.
2. Mid-call `search_memory` tool call round-trips and is spoken.
3. Barge-in during playback truncates/cancels cleanly; no tail audio.
4. Discord: `/voice join` + `/talk join` with provider=grok works end to end.
5. `hermes talk doctor` shows the grok lane with redacted key status.
6. Suite + plugin-guard green; README provider section + CHANGELOG 0.11.0.

## Reference etiquette

Protocol cross-check only against `kvnloo/zer0-voice`
(`feat/hermes-xai-realtime-provider`, untested FakeSocket code) and xAI's
official docs. Our implementation is original against our own contract. If
the reference materially saved time, a one-line thanks in the README
acknowledgments — no co-authorship, no merge entanglement.
