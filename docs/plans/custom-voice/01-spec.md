# Custom voice cascade (ElevenLabs) for hermes-talk — spec

Status: direction approved, pre-implementation. 2026-08-28.
Owner repo: TheSmokeDev/hermes-talk. No core edits.

## Why

Native provider voices are provider-locked (OpenAI's voices on OpenAI, Grok's
on Grok, Gemini's on Gemini). Operators want their OWN voice — including
cloned voices. This adds a cascade mode: the realtime provider becomes the
brain (listening, thinking, tools, turn-taking) while speech synthesis is
handed to a streaming TTS the operator chooses.

## Probe ground truth (live, 2026-08-28, operator account)

- Endpoint: `wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?model_id=eleven_flash_v2_5&output_format=pcm_24000`
- Auth: `xi-api-key` header. Never log it; scrub like the Gemini URL key.
- Flow: BOS `{"text": " ", "voice_settings": {...}}` → text chunks
  `{"text": "...", "try_trigger_generation": true}` → EOS `{"text": ""}`.
  Server streams JSON `{"audio": "<b64>", ...}`; terminal `{"isFinal": true}`.
- Measured: first audio at ~490ms; ~4.2s of PCM24k delivered in ~774ms total
  (faster than realtime — playback never starves once primed).
- Operator account already carries clones (e.g. a `generated` voice named
  `homie`). Voice id is config, never hardcoded.
- `ELEVENLABS_API_KEY` is present in the operator's user environment on the
  live machine (51 chars) — no new key management needed.

## Design

New voice mode knob, default keeps today's behavior untouched.

### Config (talk_config.py)

- `TALK_VOICE_MODE` = `native` (default) | `cascade` — fail-closed.
- `TALK_CASCADE_TTS` = `elevenlabs` (only value; more later) — fail-closed.
- `resolve_elevenlabs_key()`: `TALK_ELEVENLABS_API_KEY` → `ELEVENLABS_API_KEY`,
  set-but-blank hard refusal (same pattern as the other providers).
- `TALK_ELEVENLABS_VOICE_ID` — required in cascade mode; fail-closed with a
  remediation message (doctor can list voices live behind an explicit probe
  flag; never probe by default).
- `TALK_ELEVENLABS_MODEL` default `eleven_flash_v2_5`.

### Cascade architecture (new module: talk_cascade_voice.py)

Cascade sits between the provider session and playback:

1. Provider session is opened in TEXT-output mode (no provider audio out).
   Verify per-provider: OpenAI GA supports output_modalities ["text"]; gate
   cascade to providers that do (fail-closed error naming the provider if
   not).
2. Assistant text deltas stream into a sentence chunker: emit on terminal
   punctuation (. ! ? …) and on clause breaks past a length budget (~120
   chars) so long sentences don't add latency. Never split mid-word.
3. Each chunk is fed to the ElevenLabs stream with `try_trigger_generation`;
   the returned PCM24k is emitted into the SAME playback path OutputAudio
   uses today (the playback engine must not care whether bytes came from the
   provider or the cascade).
4. Turn bookkeeping rides the provider's events: ResponseStarted/Finished
   semantics stay provider-owned; the cascade synthesizes audio only.
5. Barge-in: SpeechStarted → drop local playback immediately + abort the
   in-flight TTS stream (close it; a fresh stream opens for the next chunk)
   + the normal upstream cancel path. A cancelled sentence must never speak
   after the operator starts talking.
6. Failure isolation: TTS stream errors degrade the response to text-only
   (transcript still renders) with one logged receipt per response — never
   kill the voice session because the TTS leg hiccupped.

### Interaction with existing surfaces

- Terminal lane: cascade works.
- Discord lane: cascade works (same playback path); note the existing
  provider gates still apply (Gemini stays terminal-only until its VAD gate
  is resolved).
- Dashboard lane: out of scope this PR.
- Native mode: byte-identical behavior; zero regression tolerance.

## Latency budget

Native provider audio starts ~300-600ms after turn end. Cascade adds
chunker (~0-150ms) + TTS first-audio (~490ms measured) ≈ one extra
half-second on the first sentence only; later sentences pipeline under
playback. Documented trade: you trade ~0.5s first-word latency for YOUR
voice.

## Testing

- Fake-TTS-socket contract tests: chunker boundaries (abbreviations,
  decimals, ellipses), stream lifecycle (BOS/chunks/EOS/isFinal), barge-in
  abort ordering (no post-barge audio), failure isolation (TTS error →
  text-only degrade + session survives), fail-closed config lanes
  (mode/tts/key/voice-id), native-mode untouched regression.
- Live smoke (operator-machine, never CI): SAPI speech in → cloned-voice PCM
  out → tool call mid-call → barge-in during cascade playback.
- Suite + ruff + plugin-guard green. No realistic secrets in tests; the
  stream-input URL pattern with the voice-id placeholder is fine (no key
  material in it — key is a header).

## Non-goals (this PR)

- Other TTS providers (Cartesia, etc.) — the cascade seam is shaped so they
  can follow.
- Voice-clone management (creating/listing/deleting voices is the operator's
  ElevenLabs account, not the plugin).
- Dashboard cascade; changing the default away from native.
- Per-persona voice mapping (Bot Mode profiles → voices is a follow-up).

## Acceptance

1. `TALK_VOICE_MODE=cascade TALK_ELEVENLABS_VOICE_ID=<id> hermes talk` — the
   assistant speaks in the chosen voice, tools fire mid-call, barge-in cuts
   the cloned voice off cleanly.
2. Native mode unchanged (regression suite proves it).
3. Doctor shows the cascade lane with redacted key/voice status.
4. Suite + plugin-guard green; CHANGELOG 0.13.0 (Unreleased).
