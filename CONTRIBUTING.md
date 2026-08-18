# Contributing

Small plugin, strong opinions. This page is everything you need to get a
change from clone to green.

## Setup

```bash
git clone https://github.com/TheSmokeDev/hermes-talk.git
cd hermes-talk
pip install -e ".[dev]"        # add ,audio if you want a real mic locally
```

## Tests and lint

```bash
pytest -q          # the whole suite — seconds, not minutes
ruff check .       # the exact check CI runs
```

**The suite is offline by design**: no secrets, no network, no audio
device. Realtime sessions are exercised against scripted transcripts, the
host against a stub plugin context, audio against fakes. That's why CI
needs nothing from you — no keys, no env, no fixtures — and why a test
that wants the network is a test shaped wrong for this repo.

## Layout — flat modules, on purpose

Top-level `talk_*` modules, no package nesting. Hermes loads plugins by
file path, and the flat layout with a collision-proof prefix is the shape
that survives both that loader and `pip install`. Don't restructure it.

| Module | One line |
|---|---|
| `talk_wire.py` | Pure OpenAI Realtime protocol — payloads and ephemeral mints, zero host knowledge |
| `talk_auth.py` | The three credential lanes, resolved fail-closed |
| `talk_doctor.py` | Read-only detect/decide/verify diagnostics and renderers |
| `talk_setup.py` | Interactive missing-decision prompts, per-write confirmation, and post-write doctor verification |
| `talk_realtime.py` | Typed provider-neutral session setup, events, commands, lifecycle states, and adapter protocol |
| `talk_openai_realtime.py` | OpenAI ephemeral-mint/WebSocket adapter and neutral-to-wire translation |
| `talk_config.py` | Every knob, resolved at call time — never cached at import |
| `talk_identity.py` | Identity sections → session instructions, budgeted |
| `talk_relay.py` | Realtime event loop — transport-agnostic, fully testable offline |
| `talk_audio.py` | Duplex mic/speaker (sounddevice) |
| `talk_cli.py` | `hermes talk` — transport, lifecycle, the announcement pump |
| `talk_tools.py` | The 9 model-facing tools and their speakable-error contract |
| `talk_host.py` | The host adapter — agent lanes, steer/redirect/stop verbs |
| `talk_runs.py` | Async-run registry with the durable history tail |
| `talk_steer.py` | The receipt ledger and both delivery-artifact watchers |
| `talk_lifecycle.py` | subagent start/stop hooks → roster, ledger, announcements |
| `talk_apiserver.py` | The api-server agent lane |
| `talk_capabilities.py` | Live capability catalog — in-process probe first, REST fallback, TTL-cached |
| `talk_providers.py` | Optional REST TTS/STT providers |

## How changes ship here

Every release goes through adversarial review before it tags: the suite
and lint first, then at least one independent review round explicitly
prompted to refute the change, findings reconciled with regression tests
before anything ships. That's why the history is full of commits titled
"reconcile … round" — the findings and their closures are part of the
record, not cleaned out of it. Expect a PR to get the same treatment, and
expect wording to matter: a spoken sentence may claim only what an
artifact proves. If a review round says your claim outruns your receipt,
the fix is usually the sentence, sometimes the code, never the standard.

## Pull requests

- Keep the suite green on ubuntu + windows × 3.11–3.13 (CI runs exactly
  `pytest -q` and `ruff check .`).
- One logical change per PR; tests ride with the change, not behind it.
- Broad `except` clauses are house style at the voice boundary (a live
  call must not die on a stack trace) — each carries a `noqa: BLE001`
  with the reason. Don't "fix" them; do justify new ones.
- Bug reports: the issue form asks for your `talk_status` output — that
  one paste answers version, auth lane, agent lane, and audio in one go.
