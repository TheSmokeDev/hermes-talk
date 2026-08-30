# Gemini Live provider for hermes-talk — spec

Status: direction approved, pre-implementation. Build AFTER the Grok provider
PR lands (it establishes TALK_PROVIDER routing that this conforms to).
2026-08-28. Owner repo: TheSmokeDev/hermes-talk. No core edits.

## Why

- Gemini Live is the third unclaimed realtime provider slot in the Hermes
  ecosystem: every Gemini voice PR upstream was batch STT/TTS and died
  unmerged; nobody has shipped a Live API transport.
- Strategic: free-tier API keys make this the zero-cost lane for users who
  won't pay OpenAI or xAI. Also the only provider with native session
  resumption observed live.

## Probe ground truth (live, 2026-08-28, operator key)

Endpoint:
`wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=<KEY>`

- **API key rides in the URL query.** Rule: never log the URL; receipts and
  doctor redact. This is the only provider where the URL itself is a secret.
- Probed model: `models/gemini-3.1-flash-live-preview` (worked first try).
  Fallback line: `gemini-2.5-flash-native-audio-latest`.
- Setup: first client message `{"setup": {model, generationConfig:
  {responseModalities: ["AUDIO"], speechConfig.voiceConfig.
  prebuiltVoiceConfig.voiceName}, systemInstruction.parts[].text,
  tools[0].functionDeclarations[], outputAudioTranscription: {},
  inputAudioTranscription: {}}}` → server acks `{"setupComplete": {}}`.
  Function-declaration parameter schema types are UPPERCASE
  (`"OBJECT"`, `"STRING"`).
- Text turn: `clientContent.turns[{role:"user", parts:[{text}]}]` with
  `turnComplete: true`.
- Tool call observed: `{"toolCall": {"functionCalls": [{"name", "args": {…},
  "id": "fc_…"}]}}` — **args arrive as a parsed dict, not a JSON string**
  (OpenAI/xAI send a string). Translate into `rt.FunctionCall.arguments`
  (string) via `json.dumps`.
- Tool response accepted:
  `{"toolResponse": {"functionResponses": [{"id", "name",
  "response": {"result": …}}]}}`; the model then spoke the result
  ("It is clear and 21°C in Paris."). Full loop verified live.
- Assistant audio: `serverContent.modelTurn.parts[].inlineData` with
  `mimeType: "audio/pcm;rate=24000"`, b64 data. Output transcript chunks:
  `serverContent.outputTranscription.text`. Turn lifecycle flags on
  serverContent: `generationComplete`, `turnComplete`; `turnComplete` carries
  `usageMetadata`.
- **Session resumption is native**: server emits
  `sessionResumptionUpdate.newHandle` (`resumable: true`). v1 records the
  latest handle; reconnect-with-handle is a follow-up, not v1.
- Tolerate: unknown server messages, empty serverContent frames.

## NOT probed (verify during implementation, honestly degrade like Grok)

- Audio input: `realtimeInput.audio` chunks, `mimeType:
  "audio/pcm;rate=16000"` — **input is 16kHz, output is 24kHz**; the relay
  feeds 24kHz, so the adapter must downsample 24k→16k (integer-decimate is
  already the repo's approach in the Discord lane) or request/pacing-check
  documented rates.
- VAD: `realtimeInputConfig.automaticActivityDetection`; activity events
  arrive as `serverContent` activity signals (verify exact field names live).
- Barge-in: server marks `serverContent.interrupted: true`; there is NO
  client-side truncate command in the Live protocol — on interruption the
  adapter drops local playback and emits the contract's truncation
  bookkeeping locally. `TruncateOutput`/`CancelResponse` commands degrade to
  local playback cancel + receipt (nothing to send upstream).
- Voice list: confirm available prebuilt voices for the 3.1 live preview at
  implementation time (2.5-era names include Puck, Charon, Kore, Fenrir,
  Aoede).

## Design (conforms to the Grok PR's provider routing)

- `TALK_PROVIDER=gemini` (third value; fail-closed unchanged).
- `talk_gemini_realtime.py`: `GeminiRealtimeSession` implementing
  `rt.RealtimeSession`; setup/translate/close mirroring the module structure
  of `talk_grok_realtime.py`; deliberate duplication, no cross-provider
  private imports.
- `talk_config.py`: `GEMINI_LIVE_VOICES` (verify live), `DEFAULT_GEMINI_MODEL
  = "gemini-3.1-flash-live-preview"`, `TALK_GEMINI_MODEL`/`TALK_GEMINI_VOICE`,
  `resolve_gemini_key()` — `TALK_GEMINI_API_KEY` → `GEMINI_API_KEY`,
  set-but-blank hard refusal.
- Doctor: gemini lane with redacted key presence; no live probe by default.
- Dashboard: Phase 2 with the relay design (WS only, same as Grok).

## Testing

- Fake-socket contract tests mirroring tests/test_grok_realtime.py: setup
  message shape, dict-args → string translation, tool response envelope,
  inlineData → OutputAudio, interrupted → local truncation path, key-in-URL
  redaction (assert no log line contains the key), config/auth fail-closed
  lanes.
- Full suite + ruff + plugin-guard green. Scanner trap rules from 0.10.1
  apply (no host+`$VAR` line pairings, no realistic secrets in fixtures).

## Non-goals (this PR)

- Session-resumption reconnect using the handle (record only).
- Live `media` (video/image) input; native-audio emotion/style controls.
- Gemini batch STT/TTS providers (different feature, dead upstream).
- Changing default provider.

## Acceptance

1. `TALK_PROVIDER=gemini hermes talk` — live terminal call; assistant hears
   (16kHz path) and speaks (24kHz path).
2. One tool call round-trips (dict-args translation proven).
3. Barge-in: `interrupted` handled, playback drops, no stale audio.
4. Doctor shows the gemini lane; no URL/key leaks in any output.
5. Suite + plugin-guard green; CHANGELOG (next minor) + README row.
