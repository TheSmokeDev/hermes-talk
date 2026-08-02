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

Set `OPENAI_API_KEY` (or a Talk-scoped `TALK_OPENAI_API_KEY`).

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
