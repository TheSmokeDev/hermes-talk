# Contributing

Small plugin, strong opinions. This page is everything you need to get a
change from clone to green.

## Setup

```bash
git clone https://github.com/TheSmokeDev/hermes-talk.git
cd hermes-talk
pip install -e ".[dev]"        # add ,audio if you want a real mic locally
```

Or with [uv](https://docs.astral.sh/uv/), which keeps the whole thing in a
project-local `.venv`:

```bash
uv sync --extra dev            # --extra audio too, for a real mic
uv run pytest -q
uv run ruff check .
```

Either way you get the pinned `ruff==0.16.5` from the dev extra — the pin is
load-bearing, see below.

## Tests and lint

```bash
pytest -q          # the whole suite — seconds, not minutes
ruff check .       # the exact check CI runs, on the pinned ruff
                   # (`pip install -e ".[dev]"` gets that version; a
                   # different ruff can disagree in BOTH directions)
```

**The suite is offline by design**: no secrets, no network, no audio
device. Realtime sessions are exercised against scripted transcripts, the
host against a stub plugin context, audio against fakes. That's why CI
needs nothing from you — no keys, no env, no fixtures — and why a test
that wants the network is a test shaped wrong for this repo.

**One trap on a box that runs the plugin for real** ([#93](https://github.com/TheSmokeDev/hermes-talk/issues/93)):
if `hermes-agent` is importable from the interpreter running the tests —
which it is when `pytest` resolves to the Hermes install's own venv, e.g.
`%LOCALAPPDATA%\hermes\hermes-agent\venv` on Windows — twelve tests in
`tests/test_capabilities.py` and `tests/test_cli.py` fail, because the
capability catalog answers in-process from the live install instead of
staying inert. CI never sees it (no Hermes there). A project-local venv
(`uv sync` above, or `python -m venv .venv && pip install -e ".[dev]"`)
side-steps it; until the tests are hermetic, those twelve are the known
baseline on such a box and anything else red is yours.

## Layout — flat modules, on purpose

Top-level `talk_*` modules, no package nesting. Hermes loads plugins by
file path, and the flat layout with a collision-proof prefix is the shape
that survives both that loader and `pip install`. Don't restructure it.
Every shipped module is listed in `pyproject.toml` under `py-modules` —
setuptools silently drops a name with no file behind it, and `publish.yml`
fails the release if a declared module is missing from the wheel.

| Module | One line |
|---|---|
| `talk_wire.py` | Pure OpenAI Realtime protocol — payloads and ephemeral mints, zero host knowledge |
| `talk_auth.py` | The three credential lanes, resolved fail-closed |
| `talk_grok_auth.py` | Grok (xAI) credential resolution — subscription login or key, fail-closed |
| `talk_doctor.py` | Read-only detect/decide/verify diagnostics and renderers |
| `talk_setup.py` | Interactive missing-decision prompts, per-write confirmation, and post-write doctor verification |
| `talk_realtime.py` | Typed provider-neutral session setup, events, commands, lifecycle states, and adapter protocol |
| `talk_openai_realtime.py` | OpenAI ephemeral-mint/WebSocket adapter and neutral-to-wire translation |
| `talk_grok_realtime.py` | Grok (xAI) implementation of the provider-neutral Realtime session contract |
| `talk_gemini_realtime.py` | Gemini Live implementation of the provider-neutral Realtime session contract |
| `talk_core_provider.py` | The three lanes published on the Hermes core `RealtimeVoiceProvider` contract |
| `talk_core_realtime.py` | Defensive Hermes core API-v2 OpenAI Realtime adapter |
| `talk_core_session.py` | Transport-neutral admission primitives for canonical Talk sessions |
| `talk_config.py` | Every knob, resolved at call time — never cached at import |
| `talk_identity.py` | Identity sections → session instructions, budgeted |
| `talk_relay.py` | Realtime event loop — transport-agnostic, fully testable offline |
| `talk_audio.py` | Duplex mic/speaker (sounddevice), echo gate, PulseAudio AEC on Linux |
| `talk_discord.py` | Discord voice as an audio device — the same methods, a different room |
| `talk_cascade_voice.py` | Custom-voice cascade — the provider thinks in text, ElevenLabs speaks |
| `talk_cli.py` | `hermes talk` — transport, lifecycle, the announcement pump |
| `talk_tools.py` | The 11 model-facing tools and their speakable-error contract |
| `talk_operator_auth.py` | Fail-closed Discord authorization for state-changing Talk tools |
| `talk_approvals.py` | The spoken approval bridge — voice resolves run approvals out loud |
| `talk_host.py` | The host adapter — agent lanes, steer/redirect/stop verbs |
| `talk_runs.py` | Async-run registry with the durable history tail |
| `talk_progress.py` | Bounded progress phases for background work |
| `talk_steer.py` | The receipt ledger and both delivery-artifact watchers |
| `talk_lifecycle.py` | subagent start/stop hooks → roster, ledger, announcements |
| `talk_apiserver.py` | The api-server agent lane |
| `talk_capabilities.py` | Live capability catalog — in-process probe first, REST fallback, TTL-cached |
| `talk_transcript.py` | Durable transcript capture and crash-safe memory handoff |
| `talk_vault.py` | Vault recall — the durable-notes lookup a voice session can make |
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
  `pytest -q` and `ruff check .`). CodeQL and dependency review run on the
  PR as well; every third-party action in `.github/workflows/` is pinned to
  a commit SHA with its version in a comment — keep it that way when you
  bump one.
- One logical change per PR; tests ride with the change, not behind it.
- Broad `except` clauses are house style at the voice boundary (a live
  call must not die on a stack trace) — each states its reason. Don't
  "fix" them; do justify new ones. Whether the reason also needs a
  `noqa: BLE001` depends on the pinned ruff and, surprisingly, on what the
  module calls its logger: since 0.16 ruff drops BLE001 when the handler
  logs the exception through a name it treats as a logger (`logger` yes,
  `_log` no), and a directive it no longer needs fails `RUF100`. Let
  `ruff check .` decide rather than copying a neighbouring handler.
- Bug reports: the issue form asks for your `talk_status` output — that
  one paste answers version, auth lane, agent lane, and audio in one go.
  `hermes talk doctor --json` is the fuller receipt.
