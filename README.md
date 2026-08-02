# hermes-talk

**OpenAI Realtime speech-to-speech voice for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — duplex talk with live tool calling.**

Hermes has voice mode. This is different: full speech-to-speech duplex over the
OpenAI Realtime API — you talk, it talks back, you can interrupt it
mid-sentence — with Realtime function calls relayed live into Hermes's own
tool surface. Ask what it remembers and you hear a real `memory` lookup.
Tell it to kick off work and a real background agent runs while you keep
talking.

## Install

```bash
hermes plugins install TheSmokeDev/hermes-talk --enable
pip install "hermes-talk[audio]"   # mic + speaker support (sounddevice)
```

## Auth — bring a key, or bring your ChatGPT subscription

Two lanes, resolved fail-closed in this order:

1. `TALK_OPENAI_API_KEY` — a Talk-scoped API key (set-but-empty refuses, never
   falls through)
2. `OPENAI_API_KEY` — the shared environment key
3. **Codex OAuth** — no key at all: if you're signed into the
   [Codex CLI](https://github.com/openai/codex) (`codex login`), Talk rides
   your own ChatGPT subscription's Realtime entitlement. Expired tokens
   refresh automatically and write back atomically, so the Codex CLI keeps
   working.

Whatever the lane, the session is minted server-side into an **ephemeral
client secret** — the raw key or OAuth token touches exactly one OpenAI
endpoint and never reaches the socket, a log line, or a client.

## Use

```bash
hermes talk        # terminal duplex voice session
```

or `/talk` inside an interactive Hermes session (full agent-loop tool access).

## Status

v0.1 — under active development. Roadmap: run steering by voice, browser
dashboard tab, session-end memory debrief, gateway platform adapter.

## License

MIT
