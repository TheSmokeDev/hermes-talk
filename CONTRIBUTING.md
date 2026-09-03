# Contributing

Small plugin, strong opinions. This page is everything you need to get a
change from clone to merged — what we want most, where each kind of change
starts, how to set up, and what a PR has to carry to land.

The promise on our side: **a first response within 24 hours, usually the
same day.** Merged work is credited by name in [CHANGELOG.md](CHANGELOG.md)
and in the README's Contributors line. Not sure where a change belongs?
Open a [Discussion](https://github.com/TheSmokeDev/hermes-talk/discussions)
or an issue first — a ten-line sketch saves a two-day PR.

## What we want most

Ranked. When two contributions compete for review time, the higher one
goes first.

1. **Bug fixes on live lanes.** A session that dies, a barge-in that does
   not cut playback, a tool call that never lands, a spoken receipt that
   claims more than an artifact proves. Reproduce against `main`, name the
   line where it manifests, fix the class (sibling adapters and surfaces
   included), ship the regression test in the same PR.
2. **Provider and host compatibility.** The suite is offline, so a wire
   change at OpenAI, xAI, or Google — or a Hermes host API moving under us —
   reaches us as a broken user. A filled-in
   [provider receipt](docs/PROVIDER-RECEIPT.md) is a contribution on its
   own; the fix that follows it is a better one.
3. **Security hardening**: the auth store, token routing, redaction. The
   invariants are spelled out in [SECURITY.md](SECURITY.md): a raw
   credential is spent on exactly one upstream endpoint and appears nowhere
   else; nothing but the documented Codex refresh writes a credential
   store; `doctor`, `check`, and `diagnostics` redact by construction.
   Report a *break* privately (see SECURITY.md); a *hardening* PR is
   welcome in the open.
4. **Cross-platform.** CI is ubuntu + windows × Python 3.11–3.13; macOS is
   covered by users, not runners. Audio devices, paths, file permissions,
   process handling, encodings — anything that behaves differently across
   the three.
5. **New realtime providers behind the contract.** A fourth lane on
   [`talk_realtime.py`](talk_realtime.py), self-hosted or hosted. The
   path is below.
6. **New surfaces.** Another room the same session can be in — the Discord
   channel and the dashboard tab are the two that exist.
7. **Docs.** A sentence that stopped being true, a knob with no row in the
   [operating manual](docs/OPERATING.md), a voice command with no card in
   [docs/VOICE-COMMANDS.md](docs/VOICE-COMMANDS.md).

Not wanted, even when well built: a new dependency for something the
standard library does, a knob cached at import time, a `TALK_*` variable
that is inferred instead of set, a spoken sentence that outruns its
receipt, and a mock that can make `hermes talk check` go green.

## Common contribution paths

| I want to… | Start here | It touches | It ships with |
|---|---|---|---|
| **Report that a provider works, or broke** | [docs/PROVIDER-RECEIPT.md](docs/PROVIDER-RECEIPT.md) | nothing — it is a filled-in table | the [provider compatibility issue form](.github/ISSUE_TEMPLATE/provider_compatibility_report.yml) |
| **Fix a bug** | the module in the [layout table](#layout--flat-modules-on-purpose) that owns the symptom; [`tests/fake_realtime.py`](tests/fake_realtime.py) to script the failing transcript | the owning module, its siblings if the class repeats | a regression test that fails on `main` |
| **Add a realtime provider** | the neutral contract in [`talk_realtime.py`](talk_realtime.py) (`RealtimeSession`, the event and command types); [`talk_gemini_realtime.py`](talk_gemini_realtime.py) as the freshest complete adapter; its [spec](docs/plans/gemini-provider/01-spec.md) as the shape of a good pre-implementation write-up | `talk_<name>_realtime.py`; `TALK_PROVIDERS` and the model/voice knobs in [`talk_config.py`](talk_config.py); `resolve_provider_lane()` and `_realtime_session()` in [`talk_cli.py`](talk_cli.py); the `provider`/`auth`/`model`/`voice` checks in [`talk_doctor.py`](talk_doctor.py); a core lane in [`talk_core_provider.py`](talk_core_provider.py) with its capabilities declared, never assumed; `py-modules` and `known-first-party` in [`pyproject.toml`](pyproject.toml); [`.env.example`](.env.example) | `tests/test_<name>_realtime.py` against scripted server events, new cases in [`tests/test_doctor.py`](tests/test_doctor.py) and [`tests/test_core_provider.py`](tests/test_core_provider.py); README Providers row, an [operating manual](docs/OPERATING.md) knob row, a CHANGELOG entry; a [receipt](docs/PROVIDER-RECEIPT.md) from a live session in the PR |
| **Add a surface** | the audio-device shape in [`talk_audio.py`](talk_audio.py) (`DuplexAudio`: `start`, `stop`, `read_input_chunk`, `queue_playback`, `drain_playback`, `playback_pending`, `played_ms`); [`talk_discord.py`](talk_discord.py) is the second implementation of the same methods in a different room | a new device module; wiring in [`talk_cli.py`](talk_cli.py); if speakers other than the operator can be in the room, authority in [`talk_operator_auth.py`](talk_operator_auth.py), fail-closed | tests against a fake device (see [`tests/test_discord.py`](tests/test_discord.py)); a README Surfaces row; the spoken receipts the surface commits to |
| **Add a talk tool** | [`talk_tools.py`](talk_tools.py): a schema dict, a `_handle_<name>` returning bounded plain text, an entry in `_HANDLERS`, the advertised list in `default_talk_tools()` | one of `READ_ONLY_TALK_TOOLS` / `MUTATING_TALK_TOOLS` in [`talk_operator_auth.py`](talk_operator_auth.py) — unclassified means unreachable, on purpose | [`tests/test_tools.py`](tests/test_tools.py) and [`tests/test_operator_auth.py`](tests/test_operator_auth.py); a card row in [docs/VOICE-COMMANDS.md](docs/VOICE-COMMANDS.md) stating what the reply commits to |
| **Fix or extend the docs** | [README.md](README.md), [docs/OPERATING.md](docs/OPERATING.md), [docs/VOICE-COMMANDS.md](docs/VOICE-COMMANDS.md) | prose only | nothing else — but every relative link must resolve, and a claim about behaviour must match the code on `main` |

The tool contract that makes a live call survivable, from
[`talk_tools.py`](talk_tools.py): an unknown tool name raises
`TalkToolError` (a client bug); a known tool that fails *returns* the
failure as text, so the model says what broke instead of the session dying
on a stack trace. Outputs are plain text the model will read aloud —
nothing formatted for a screen.

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
uv run --extra dev pytest -q
uv run --extra dev ruff check .
```

Either way you get the pinned `ruff==0.16.5` from the dev extra — the pin is
load-bearing, see below.

**Why `--extra dev` on every `uv run`:** pytest and the pinned ruff live in
the `dev` extra, not in the dependencies. `uv run` only guarantees the base
dependencies are present, so on a fresh clone — or after anything that
re-syncs the environment — a bare `uv run pytest` can find no pytest at
all. Naming the extra on the run is the form that works regardless of what
you synced before.

You need **Python 3.11–3.13** and **git**. `node` on `PATH` is needed for
the two dashboard-transport tests in
[`tests/test_dashboard_js.py`](tests/test_dashboard_js.py), which drive the
hand-authored bundle in `dashboard/dist/` through `node -e`; GitHub's
runners have it. A Hermes install is **not** needed for the suite — only
for the live proof.

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

### The live proof — `hermes talk check`

Green tests prove the policy; they cannot prove a wire. Anything that
touches a provider adapter, credential resolution, or the delegation path
also needs one live pass on a real Hermes install:

```bash
hermes talk check              # doctor + one real provider turn + one bounded Hermes run
hermes talk check --json       # the same, as a report you can paste into the PR
hermes talk check --provider grok   # a lane other than TALK_PROVIDER, this process only
```

To run **your branch** live: push it to your fork, then install that exact
commit over the released plugin and restart the gateway —
`hermes plugins install --force --ref <40-char sha> <you>/hermes-talk --enable`
(a running gateway keeps executing the old code until it restarts; the
[operating manual](docs/OPERATING.md#upgrade) names both traps). `check` is
not read-only: it spends one short provider turn and one short agent run
and changes nothing else. Its report carries no tokens and no paths, which
is what makes it safe to paste.

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
| `talk_check.py` | `hermes talk check` — the live proof: doctor, one provider turn, one bounded Hermes run |
| `talk_diagnostics.py` | `hermes talk diagnostics` — the redacted, default-deny support bundle |
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

The dashboard tab lives beside them: `dashboard/plugin_api.py` (routes),
`dashboard/dist/index.js` (the hand-authored browser transport — source,
not a build artifact), `dashboard/manifest.json`.

## Code style

- **Ruff decides.** `E F I UP B SIM RUF BLE`, line length 100, target
  `py311` — all in `pyproject.toml`. Run the pinned version; the rule set
  and the version are pinned separately, and each pin alone still lets a
  ruff release turn the build red. Bump both together, in their own PR.
- **Knobs resolve at call time.** Every `TALK_*` setting is read when it
  is needed, in `talk_config.py`, never bound at import or cached in a
  module global. Tests rely on flipping the environment between calls.
- **Fail closed, never infer.** `TALK_PROVIDER` names the lane; nothing
  guesses it from which keys happen to exist. The same rule for speaker
  authority, tool classification, and every `doctor` decision.
- **A credential is spent on exactly one endpoint.** It goes in the header
  or URL that endpoint requires and nowhere else — not a log line, not a
  receipt, not a spoken sentence, not a test fixture. Anything printed
  passes through `talk_doctor.SECRET_PATTERNS` or `talk_core_provider.redact`.
- **A spoken sentence claims only what an artifact proves.** "queued" is a
  queue write; "landed" follows a delivery artifact. Receipt states are
  contracts, and a wrong sentence is a real bug.
- **Broad `except` clauses are house style at the voice boundary** — a live
  call must not die on a stack trace — and each one states its reason in
  a comment. Don't "fix" them; do justify new ones. Whether the reason
  also needs a `noqa: BLE001` depends on the pinned ruff and, surprisingly,
  on what the module calls its logger: since 0.16 ruff drops BLE001 when
  the handler logs the exception through a name it treats as a logger
  (`logger` yes, `_log` no), and a directive it no longer needs fails
  `RUF100`. Let `ruff check .` decide rather than copying a neighbouring
  handler.
- **Cross-platform, always.** Open files with `encoding="utf-8"`; build
  paths with `pathlib`; guard POSIX-only process calls behind
  `sys.platform`; treat an audio device as optional (the `[audio]` extra
  is not installed on CI). Windows is a first-class runner here, not a
  best-effort port.
- **One version surface.** Bump `pyproject.toml`; `plugin.yaml` and
  `dashboard/manifest.json` must match it and
  [`tests/test_repository_hygiene.py`](tests/test_repository_hygiene.py)
  fails the suite when they drift.
- **Comments explain intent, trade-offs, and API quirks** — not what the
  next line obviously does. The codebase leans on them; keep the density,
  not the noise.

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

### Branch names

```
fix/description        # bug fixes
feat/description       # new capability
docs/description       # documentation
test/description       # tests only
ci/description         # workflows, pins, release plumbing
chore/description      # dependency bumps, housekeeping
```

### Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):
`<type>(<scope>): <description>`, imperative, lower-case, no trailing
period. Types are the branch prefixes above plus `perf` and `refactor`.
The scope is the area the change lands in — these are the ones the
history already uses:

| Scope | Covers |
|---|---|
| `audio` | `talk_audio.py` — devices, echo gate, AEC |
| `auth` | `talk_auth.py`, `talk_grok_auth.py`, credential lanes |
| `bridge` | the capability bridge and approval bridge — `talk_approvals.py`, `talk_host.py`, `talk_apiserver.py` |
| `catalog` | `talk_capabilities.py` |
| `cli` | `talk_cli.py`, the `hermes talk` command surface |
| `core` | the Hermes core contract lanes — `talk_core_provider.py`, `talk_core_realtime.py`, `talk_core_session.py` |
| `discord` | `talk_discord.py` |
| `grok`, `gemini`, `openai` | one provider adapter |
| `identity` | `talk_identity.py` |
| `lifecycle` | `talk_lifecycle.py` |
| `memory` | `talk_transcript.py`, `talk_vault.py` — what a session remembers |
| `realtime` | `talk_realtime.py`, the neutral contract itself |
| `setup` | `talk_setup.py` |
| `steer` | `talk_steer.py`, the receipt ledger |
| `talk` | the session as a whole — `__init__.py`, relay, several surfaces at once |
| `tools` | `talk_tools.py`, `talk_operator_auth.py` |

A module with no scope yet takes its `talk_` suffix (`fix(check): …`,
`fix(relay): …`). Repo-wide changes go without a scope: `ci: …`,
`docs: …`, `test: …`, `chore: …`.

```
fix(audio): preserve speech through echo cancellation
feat(realtime): report server-cancelled tool calls as a typed event
docs: README leads with what works today
```

### What the PR carries

The [pull request template](.github/PULL_REQUEST_TEMPLATE.md) asks for
exactly this:

- **What and why** — the symptom or the gap, and why this approach. If a
  spoken sentence changes, quote before and after.
- **How to test** — the command and the expected output, so a reviewer
  reproduces it without asking.
- **Platforms** — where you ran it (`Windows 11`, `Ubuntu 24.04`,
  `macOS 15`, …). CI covers ubuntu + windows; say so if that is all.
- **The live receipt, when a lane is touched** — a
  `hermes talk check --json` excerpt (or `hermes talk doctor --json` when
  the change cannot reach a live turn) with the provider, host, and plugin
  versions and the step statuses. Both reports redact by construction.
- **`Fixes #N`** when there is an issue. GitHub closes it on merge.

- One logical change per PR; tests ride with the change, not behind it.
- Keep the suite green on ubuntu + windows × 3.11–3.13 (CI runs exactly
  `pytest -q` and `ruff check .`). CodeQL and dependency review run on the
  PR as well; every third-party action in `.github/workflows/` is pinned to
  a commit SHA with its version in a comment — keep it that way when you
  bump one.

### The merge bar

- **Live-verified before merge for anything on a provider wire** — a
  `check` receipt from the author, and a second one from a maintainer when
  the lane is one they can reach.
- **Offline tests and fake sessions for everything else** — a change that
  cannot be exercised against a scripted transcript or a stub host is not
  done yet.
- **No auth-store writes** beyond the one documented Codex refresh.
- **Tokens go only to the provider's own host** — a credential resolved for
  one lane never reaches another lane's endpoint, a proxy, or a log.
- **Nothing secret in logs or receipts** — `doctor`, `check`, and
  `diagnostics` output, spoken sentences, and test fixtures included.
- **The wording matches the artifact.** A review may ask you to change a
  sentence rather than the code.

### Review and credit

First response within 24 hours, typically the same day. Review is
adversarial by design (see above): expect at least one round that tries
to refute the change, and expect it to be about the change, not about
you. If a round stalls on our side, ping the thread — that is a fair ask.

When it merges, the CHANGELOG entry names you, and the README's
Contributors line credits what you did. Substantial external work is
merged so that authorship survives in the git history — we do not
reimplement a good PR to avoid crediting it.

## Reporting issues

- **A bug**: the [bug form](.github/ISSUE_TEMPLATE/bug_report.yml) asks for
  `hermes talk diagnostics --bundle` first — one redacted file that answers
  version, auth lane, agent lane, audio, and every doctor outcome in one
  paste. In a session, "status report" (the `talk_status` tool) gives the
  short form; `hermes talk doctor --json` is the fuller receipt.
- **A provider that works or broke**: the
  [provider compatibility form](.github/ISSUE_TEMPLATE/provider_compatibility_report.yml),
  filled from [docs/PROVIDER-RECEIPT.md](docs/PROVIDER-RECEIPT.md).
- **A feature**: the [feature form](.github/ISSUE_TEMPLATE/feature_request.yml)
  — the problem first, the proposal second, and which surfaces and
  providers it applies to.
- **A vulnerability**: privately, per [SECURITY.md](SECURITY.md). Never in
  a public issue, never with a credential, transcript, or audio attached.

Issues tagged `good first issue` are scoped so that the fix and its test
fit in one sitting; `help wanted` marks work we would merge but are not
building ourselves right now.

## License

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE), like the rest of the repository.
