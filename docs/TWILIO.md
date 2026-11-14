# Twilio telephony (`talk_twilio`)

Talk over a phone line. Twilio Programmable Voice terminates the call and opens
a bidirectional **Media Stream** to this process; `talk_twilio` converts the
8 kHz G.711 µ-law frames into the PCM16 the realtime provider expects, drives a
normal Talk session, and converts the agent's audio back into µ-law frames.

This is the transport only. The session, tools, memory and approvals are the
same ones the terminal and Discord lanes use.

## Why it isn't wired into the CLI yet

Issue [#44](https://github.com/TheSmokeDev/hermes-talk/issues/44) extracts a
channel-neutral transport contract (mic frames in, PCM out, speaker identity
evidence, join/leave lifecycle). This module is written to that shape but ships
as a standalone server so it can land without changing the CLI's audio path:

```bash
python -m talk_twilio_server   # see "Running it" below
```

Once #44 lands, `TwilioCallBridge` becomes one implementation of the contract and
the duplicated plumbing goes away.

## What it does

```
caller ──PSTN──> Twilio ──WSS──> talk_twilio ──> talk_realtime session
                    ▲                                    │
                    └────── µ-law frames ◄───────────────┘
```

* Inbound `media` frames are decoded µ-law → PCM16 8 kHz, upsampled to the
  provider's input rate (16 kHz for Gemini Live, 24 kHz for OpenAI Realtime) and
  sent as `AppendInputAudio`.
* `OutputAudio` events are downsampled to 8 kHz, encoded to µ-law and emitted as
  20 ms frames (160 bytes), which is what Twilio's jitter buffer expects.
* `OutputInterrupted` becomes a Twilio `clear` message, so barge-in stops audio
  already buffered on Twilio's side rather than letting it finish.
* Webhook requests are verified with `X-Twilio-Signature` (HMAC-SHA1 over the
  URL plus sorted POST parameters) and compared in constant time.

## No local VAD, by design

Twilio does not detect speech. Gemini Live and the OpenAI Realtime lane both do
server-side endpointing, so inbound audio is forwarded continuously and the
provider decides when the caller has finished. That also means there is no
client-side echo gate here — a phone line has no speaker-to-mic loop to gate.

## Running it

1. Buy or reuse a voice-capable number in Twilio (SMS is not needed).
2. Point the number's **voice webhook** at `https://<your-host>/twilio/voice`.
   Regional routing matters: if the console shows a region as *active* (for
   example Ireland/IE1), configure that region's voice URL, not US1 — an edit to
   an inactive region is silently inert.
3. Expose the process publicly over TLS. Caddy or nginx in front of
   `127.0.0.1:8767` is enough; the WebSocket path needs upgrade support.
4. Run the server with the environment below.

```bash
export TWILIO_AUTH_TOKEN=...            # signature verification
export TALK_PUBLIC_BASE_URL=https://voice.example.com
export TALK_PROVIDER=gemini             # or openai/grok/local
export GEMINI_API_KEY=...               # for the Gemini lane
python -m talk_twilio_server --port 8767
```

| Variable | Meaning |
| --- | --- |
| `TWILIO_AUTH_TOKEN` | Twilio auth token, used for webhook signatures |
| `TALK_PUBLIC_BASE_URL` | Public origin Twilio calls back on (`https://…`) |
| `TALK_TWILIO_INPUT_RATE` | Provider input rate, default `16000` |
| `TALK_TWILIO_OUTPUT_RATE` | Provider output rate, default `24000` |
| `TALK_TWILIO_ALLOW_UNSIGNED` | `1` disables signature checks — local tunnels only |

## Known limits

* **8 kHz is the ceiling.** The caller's audio is never better than a phone
  line, and the agent's reply is downsampled to it. Names, postcodes and order
  numbers transcribe markedly worse than they do on the terminal lane.
* **Weak identity.** A PSTN caller proves a phone number, not a person. Per #44,
  a transport with this evidence quality should default to
  **conversational-only**: the agent may talk, but must not run tool actions on
  the caller's say-so.
* **One call per stream.** Each Media Stream gets its own session; there is no
  shared audio bus and no mixing.
* **No DTMF handling yet.** `dtmf` messages parse, but nothing consumes them.

## Who gets through

The caller's number arrives in the webhook's `From` parameter, and the server
passes it into the media stream as a `<Parameter name="caller">` — Twilio echoes
it back in `start.customParameters`, because the WebSocket handshake itself
carries no call metadata.

That number drives the policy:

| Demo mode | Caller | What happens |
| --- | --- | --- |
| off | owner | the real assistant: private briefing, full toolset |
| off | anyone else | `<Say>` a refusal, then `<Hangup/>` |
| on | owner | same assistant, still able to hand over to a stranger mid-call |
| on | anyone else | demo persona, **public facts only**, no memory or message tools |

Demo mode is a file, not a config edit: `~/.hermes/data/phone/demo.json`, flipped
from `/demo` and expiring on its own (45 minutes by default). `/demo` should sit
behind basic auth at the proxy — anyone who can reach it can open the line.

The persona split matters more than it looks: a stranger gets the profile in
`~/.hermes/data/talk-public-profile.md` and a tool list with `lookup_memory`
removed entirely. Undeclared tools cannot be called, so that is a real boundary
rather than a prompt instruction the model could ignore.

## The demo switch

`/demo` is a password-only page (a single field, cookie session valid 12 hours,
`scrypt` hash in `TALK_DEMO_PASSWORD_HASH`, HMAC-signed cookie from
`TALK_DEMO_COOKIE_SECRET`). It is deliberately in the app rather than at the
proxy so it can be opened from a phone and shared by password alone.

Turning it on admits strangers for 45 minutes and then closes itself. Turn it
off when the demo ends; a line left open is a line anyone can talk to.

## Asking the real assistant

`ask_hermes` has an honest budget: it answers from long-term memory when memory
already knows, and otherwise queues the question (to
`~/.hermes/data/phone/questions.jsonl`) and tells the caller Shaan will get back
to them. It does **not** block the call waiting for a live agent — dead air
mid-conversation is worse than a tidy deferral.
