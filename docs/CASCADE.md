# Custom voice — the cascade lane (ElevenLabs)

Native mode speaks with the provider's own voices, and those voices are
provider-locked. Cascade mode splits the call: the realtime provider stays
the brain (listening, thinking, tools, turn-taking) and hands its answer
TEXT to a streaming ElevenLabs TTS, so the assistant speaks in any voice on
your ElevenLabs account — including a clone of your own.

```bash
TALK_VOICE_MODE=cascade \
TALK_ELEVENLABS_VOICE_ID=<your-voice-id> \
hermes talk
```

The key comes from `TALK_ELEVENLABS_API_KEY` or `ELEVENLABS_API_KEY`
(Talk-scoped wins; set-but-blank refuses), and the TTS model defaults to
`eleven_flash_v2_5` (override: `TALK_ELEVENLABS_MODEL`). To clone your own
voice, create it in ElevenLabs VoiceLab first (VoiceLab → your voice → copy
the ID); voice management stays in your ElevenLabs account, not the plugin.

The trade, stated plainly: native provider audio starts ~300–600ms after
turn end, and the cascade adds roughly one extra half-second on the FIRST
sentence (sentence chunking plus TTS first-audio, ~490ms measured) — later
sentences pipeline under playback. You trade ~0.5s of first-word latency
for your voice.

Cascade is OpenAI-only for now (it is the one provider whose text-output
mode is wired and verified — picking grok or gemini fails closed and names
the provider). Barge-in cuts the cloned voice off exactly like native:
SpeechStarted aborts the in-flight TTS stream and drains playback in the
same synchronous step, so a cancelled sentence never speaks. A TTS failure
degrades that one answer to text-only with a single logged receipt; the
call itself survives. `TALK_VOICE_MODE` is fail-closed and defaults to
`native`, which is byte-identical to the pre-cascade behavior. Doctor gains
a `cascade` check: mode, TTS provider, redacted key presence, voice-id
status — no live probe.

The cascade speaks on every Talk surface:

| Surface | How the cascade speaks |
| --- | --- |
| Terminal (`hermes talk`) | The provider session opens in text-output mode; the cascade feeds the SAME playback sink the relay feeds. |
| Discord (`talk join`) | The same shared session loop; cascade PCM24k takes the relay's exact path through the 24k→48k voice-channel conversion. |
| Dashboard tab | The browser keeps its WebRTC socket but mints a text-output session and relays the model's text deltas to `POST /api/plugins/hermes-talk/cascade-tts`; the server-side cascade speaks them and streams PCM back. The ElevenLabs key never reaches the browser — the route sits behind the same `TALK_DASHBOARD_TOKEN` / loopback gate as the mint, and barge-in aborts the fetch, which cancels the TTS exactly like the terminal lane. |

Every cascade variable — including `TALK_CASCADE_SPEED`, the per-turn
generation behavior, SSML parsing, and the number-normalization trap on
`eleven_flash_v2_5` — is documented in
[OPERATING.md](OPERATING.md#custom-voice-cascade-lane).
