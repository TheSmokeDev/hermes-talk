# Twilio phone server (`talk_twilio_server`), experimental

> **Experimental.** This is a separate, opt-in server for people who host their
> own Twilio number. It is **not** the built-in Phone mode, which uses Dograh
> (see the README's *Optional native Phone mode* section). Pressing Talk never
> starts it and nothing in the plugin imports it. It has not been confirmed on
> a real phone call yet.

Each inbound call to your Twilio number gets a fresh Gemini Live voice session.
That session has no Hermes task, no tools and no memory. It only talks.

```
caller ──PSTN──> Twilio ──WSS──> talk_twilio_server ──> Gemini Live session
                    ▲                                          │
                    └────────── µ-law frames ◄─────────────────┘
```

## How a call is handled

1. Twilio posts the call to `/twilio/voice`. The server checks the
   `X-Twilio-Signature` header against your auth token, then checks the caller's
   number against `TALK_TWILIO_ALLOWED_CALLERS`. A refused call gets `<Reject>`,
   so it is never answered and never billed.
2. An accepted call gets TwiML that opens a bidirectional Media Stream, carrying
   a single-use token tied to that call. The stream socket opens a provider
   session only for that token, so nobody can use the socket directly.
3. Caller audio (8 kHz µ-law) is decoded and resampled to the session's 24 kHz
   PCM. The agent's audio goes back as 20 ms µ-law frames. When the caller talks
   over the agent, Twilio is told to drop the audio it has buffered.
4. The call ends when either side hangs up, when the provider session ends, or
   after 15 minutes. At most 2 calls run at once; more are declined as busy.

There is no local VAD: Gemini Live detects end of speech on its side, and a
phone line has no speaker-to-mic echo to gate.

## Running it

You need a voice-capable Twilio number, its account's primary **Auth Token**
(Twilio Console, Account, API keys & tokens), a Gemini API key, and an HTTPS
tunnel or reverse proxy on the same machine. Quick tunnels such as
`cloudflared tunnel --url http://127.0.0.1:8767` or `ngrok http 8767` work and
carry WebSockets.

```bash
export TALK_PUBLIC_BASE_URL=https://your-tunnel.example.com
export TWILIO_AUTH_TOKEN=...
export TALK_TWILIO_ALLOWED_CALLERS=+15551234567   # your own phone, E.164
export GEMINI_API_KEY=...
python -m talk_twilio_server --check   # prints the webhook URL for Twilio
python -m talk_twilio_server           # listens on 127.0.0.1:8767
```

In the Twilio Console, open the number's **Voice configuration** and set
*A call comes in* to **Webhook**, the printed `voice webhook` URL, **HTTP POST**.
The URL must match exactly, or the signature check returns 403. Regional
routing matters: if the console shows a region such as Ireland (IE1) as active,
set that region's voice URL; an edit to an inactive region does nothing.

If the number already serves something else (for example a Dograh Phone mode
line), use a different number, or put the old webhook back when you are done.

The caller speaks first: the agent answers once you say something.

| Variable | Meaning |
| --- | --- |
| `TALK_PUBLIC_BASE_URL` | Public `https://` origin Twilio calls (plain `http://` only for loopback) |
| `TWILIO_AUTH_TOKEN` | Required. Every webhook is signature-checked; there is no unsigned mode |
| `TALK_TWILIO_ALLOWED_CALLERS` | Required. Comma-separated E.164 numbers, or `*` for anyone |
| `GEMINI_API_KEY` / `TALK_GEMINI_API_KEY` | The Gemini Live key; only Gemini is supported here |
| `TALK_TWILIO_MODEL`, `TALK_TWILIO_VOICE`, `TALK_TWILIO_INSTRUCTIONS` | Optional overrides |

The server binds loopback only, because it speaks plain HTTP. It logs call
start and end, never caller numbers, tokens or keys, and keeps no access log.

## Known limits

* **8 kHz is the ceiling.** Names, postcodes and order numbers transcribe
  markedly worse than on the terminal lane.
* **Caller ID is weak.** It can be spoofed, so the allowlist is a cost guard,
  not authentication. That is why the agent has no tools here.
* **Barge-in is best effort.** Gemini sends a reply faster than it plays, and
  Twilio buffers it. Talking over the agent clears that buffer while Gemini is
  still generating, but not once Gemini has finished the reply.
* **Gemini only.** Its server-side endpointing is what makes a line work
  without local VAD; other providers are refused with a clear error.
* **No DTMF.** `dtmf` messages parse, but nothing uses them.

The transport follows the shape issue
[#44](https://github.com/TheSmokeDev/hermes-talk/issues/44) describes for a
channel-neutral transport, so it can become one implementation of that
contract later.
