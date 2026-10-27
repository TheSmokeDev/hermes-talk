# Providers and auth

The [README](../README.md) carries the summary table — which lane authenticates
how, on which default model, and where it runs. This page is the per-lane
detail behind it: how `TALK_PROVIDER` resolves, what each lane needs, and the
fail-closed credential order the OpenAI Realtime lane uses.

GPT-Live is selected separately by `TALK_VOICE_MODE=live` and carries its own
billing selection; see [GPT-Live configuration](GPT-LIVE.md#choose-billing-and-voice).
Every variable, with defaults and failure modes, is in
[OPERATING.md](OPERATING.md#configuration--every-knob).

## Provider details — OpenAI (default), Grok, or Gemini

`TALK_PROVIDER` picks the realtime voice transport: `openai` (the default),
`grok` (xAI Grok Voice), or `gemini` (Gemini Live). The
knob is fail-closed and never inferred from which keys exist — an operator
holding several gets the provider they named or an error, not a silent
switch.

The Grok lane runs on an xAI key: `TALK_XAI_API_KEY`, falling back to
`XAI_API_KEY` (set-but-blank refuses). The SuperGrok / X Premium login
(`xai-oauth`) is not used in this release: the plugin catalog allows a
subscription sign-in only through Hermes-signed requests, and Hermes signs
only Codex requests for plugins today. Talk does not read Hermes's credential
store, so `TALK_PREFER_XAI_OAUTH=true` refuses with that reason instead of
spending a key. A rejected or
tier-denied token gets a one-line remediation at connect, never a
traceback; `hermes talk doctor --probe` makes two live calls to
`api.x.ai` to prove the resolved bearer reaches realtime before you sit
down to talk. The lane rides model `grok-voice-latest`
(override: `TALK_GROK_MODEL`), and offers all 28 documented built-in voices
via `TALK_GROK_VOICE`: `ara`, `rex`, `sal`, `eve`, `leo`, `carina`, `zagan`,
`helix`, `orion`, `luna`, `iris`, `altair`, `zenith`, `perseus`, `helios`,
`lux`, `kepler`, `rigel`, `cosmo`, `celeste`, `ursa`, `sirius`, `lumen`,
`castor`, `naksh`, `atlas`, `aurora`, `liora`. Names are case-insensitive;
`ara` remains the default, and unknown names refuse closed. The roster is
shared with xAI's [text-to-speech voice table](https://docs.x.ai/developers/model-capabilities/audio/text-to-speech#voices)
([speech-to-speech docs](https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech#available-voices),
checked 2026-10-04). For example, set `TALK_GROK_VOICE=carina`. Same
contract, same tools, same barge-in; terminal and Discord lanes both honor
the knob. The dashboard tab stays OpenAI-only for now — xAI has no WebRTC
offer endpoint, so that lane is a separate backend-relay piece. Doctor gains
a provider check: selection, redacted key presence, model/voice validity.

The Gemini lane is the zero-cost option: free-tier Google AI Studio keys
work. Set `GEMINI_API_KEY` (or Talk-scoped `TALK_GEMINI_API_KEY`;
set-but-blank refuses), ride model `gemini-3.1-flash-live-preview`
(override: `TALK_GEMINI_MODEL`), and pick a voice via `TALK_GEMINI_VOICE` —
`Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede`, fail-closed and
**case-sensitive**, exactly as Google's wire expects them. Two lane-specific
notes: the key rides the WebSocket URL query on this provider, so the URL is
treated as a secret (assembled at connect, never logged, scrubbed from
transport errors), and the Live protocol has no client cancel/truncate
command, so barge-in bookkeeping degrades to local playback handling with a
logged receipt — never a faked upstream call. The Discord lane refuses
Gemini for now: its gated-response authorization flow has no Live wire
equivalent, so connect fails closed rather than answering unvetted speakers.

## Grok speech tags and text-to-speech

The Grok realtime lane generates speech from Hermes tool results and background
reports; the voice manager summarizes the result rather than synthesizing the
entire answer verbatim. The [xAI speech-tag field notes](XAI-SPEECH-TAGS.md)
collect 114 inline spellings and 13 phrase wrappers from Switchboard experiments,
with exact syntax, listening classifications, and the distinction between native
Talk, xAI REST TTS, and xAI's separate `force_message` extension. These are
experimental observations, not automatic tag support in every provider lane.

## Auth — no API key needed if you have ChatGPT

This section describes **OpenAI Realtime**. GPT-Live uses the independent
`TALK_LIVE_AUTH` selection described [here](GPT-LIVE.md#choose-billing-and-voice).

Signed in to Codex in Hermes (`hermes auth add openai-codex`, or the Codex
sign-in in Hermes Desktop)? Talk runs on your own ChatGPT subscription's
Realtime entitlement — no key, no per-minute API bill. Bring a key instead if
you'd rather.

Resolved fail-closed in this order:

1. `TALK_OPENAI_API_KEY` — a Talk-scoped API key (set-but-empty refuses, never
   falls through)
2. `OPENAI_API_KEY` — the shared environment key
3. **Codex sign-in, signed by Hermes** — no key at all. Talk asks Hermes to make
   the one request that needs the subscription (the Realtime client-secret
   mint) through `credentialed_provider_request`; Hermes attaches and refreshes
   the profile's Codex sign-in and Talk never sees the token. Talk does not read
   `~/.codex/auth.json` or Hermes's `auth.json`. Needs Hermes Agent 0.21.7 or
   newer; `plugin.yaml` declares `requires_auth: [openai-codex]`.

Keys are read through Hermes's profile-scoped secret route, so a gateway
serving several profiles never hands Talk another profile's key.

That historical order remains unchanged when `TALK_PREFER_CODEX_OAUTH` is
absent or explicitly false. Set `TALK_PREFER_CODEX_OAUTH=true` to require the
subscription lane even when API keys exist. The preference is fail-closed: a
host that cannot sign Codex requests refuses instead of spending a metered key,
and a blank or invalid preference refuses until corrected. `hermes talk doctor`
names the winning lane and whether this Hermes can sign Codex requests; whether
the profile is signed in is Hermes's answer when the call starts. It never
prints a key or token.
When setup offers the API-key lane under an enabled OAuth preference, it reuses
an existing metered key when present and separately confirms the required
`TALK_PREFER_CODEX_OAUTH=false` policy transition.

For these Realtime lanes, Talk mints an **ephemeral client secret** for the
audio connection. GPT-Live uses its own server-owned negotiation and sideband;
provider keys, OAuth tokens and account IDs are never sent to the dashboard browser.
