# Hermes Talk

**Realtime duplex voice for your own agent — talk to it, it runs real work in the background, it reports back out loud.**

<p>
  <a href="https://github.com/TheSmokeDev/hermes-talk/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/TheSmokeDev/hermes-talk/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/TheSmokeDev/hermes-talk/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/TheSmokeDev/hermes-talk/actions/workflows/codeql.yml/badge.svg"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/TheSmokeDev/hermes-talk"><img alt="OpenSSF Scorecard" src="https://api.scorecard.dev/projects/github.com/TheSmokeDev/hermes-talk/badge"></a>
  <a href="https://pypi.org/project/hermes-talk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/hermes-talk?cacheSeconds=3600"></a>
  <a href="https://pypi.org/project/hermes-talk/"><img alt="PyPI downloads" src="https://img.shields.io/pypi/dw/hermes-talk"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-16a34a"></a>
</p>

Hermes Talk is a realtime voice plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent): you speak, it answers out loud, and it calls the agent's own tools without leaving the conversation. It runs in the terminal (`hermes talk`), in a Discord voice channel (`/talk join`), and in the Hermes dashboard **Talk** tab.

**It rides your ChatGPT or SuperGrok subscription. No API key required.**

The realtime lanes are **OpenAI Realtime** (`gpt-realtime-2.1`, on your ChatGPT subscription through `codex login`), **xAI Grok Voice** (on an X Premium or SuperGrok login, no key), and **Gemini Live** (which does need a `GEMINI_API_KEY` — free-tier AI Studio keys work); **GPT-Live** is selected separately by `TALK_VOICE_MODE=live` and runs `gpt-live-1-codex` on the Codex subscription or `gpt-live-1` on explicitly chosen API billing.

Talk calls the host's tools, delegates background work while you keep talking, reports results, and handles current approval requests. Provider support differs by surface; see the tables below.

**What it is:** a plug-in that adds interruptible, duplex speech-to-speech voice to an
existing Hermes Agent install — one bidirectional audio session in which the agent uses
its real tools, hands work to background agents, and speaks the results when they land.

**Who it is for:** people already running Hermes Agent who would rather talk to it than
type at it, and who want to keep talking while it works. It is not a standalone
assistant, and it does not replace Hermes's built-in turn-based voice mode — that is a
different shape, and a good one.

<!-- Regenerate this GIF (needs ffmpeg): python docs/render-dashboard-gif.py -->
![The Talk tab in the Hermes dashboard — a live voice session, a background agent delegated mid-conversation, its result landing in the runs panel (8× speed)](docs/dashboard.gif)

### 🔊 [Watch the 2:27 cut with sound](https://github.com/TheSmokeDev/hermes-talk/releases/download/v0.3.0/hermes-talk-dashboard-cut.mp4) — delegate, keep talking, hear the result land.

*Real Realtime session, 8× speed in the GIF, recorded at v0.3.0. It does not demonstrate the new GPT-Live or Codex-worker integration.*

## Install

```bash
hermes plugins install TheSmokeDev/hermes-talk --enable
pip install "hermes-talk[audio]"   # mic + speaker support (sounddevice); skip if dashboard-only
```

Needs Python ≥ 3.11 and a Hermes host ≥ v0.17. GPT-Live, shared task attachment
and Codex workers need additional host capabilities; the full list is in
[Prerequisites](docs/OPERATING.md#prerequisites).

## Quickstart

**Terminal** — start here:

```bash
hermes talk
```

You are live: speak, and it answers out loud in the same breath. Ctrl+C hangs
up. → [Use](docs/OPERATING.md#use)

**Discord** — the call happens inside a voice channel, not in chat:

```
/voice join     # put Hermes in the voice channel first
/talk join      # Talk borrows that connection — it never opens a second one
```

Talk answers in the room everyone can hear; `/talk leave` ends it. Mutating
tools stay denied until you set `TALK_DISCORD_OPERATOR_USER_IDS`.
→ [Discord voice](docs/OPERATING.md#discord-voice--talking-in-the-channel-hermes-is-already-in)

**Dashboard** — browser audio, no local mic drivers:

```bash
hermes dashboard    # then open the Talk tab and hit Start
```

Allow the microphone and talk. You see the live transcript plus a list of
background runs. → [Dashboard tab](docs/OPERATING.md#dashboard-tab)

## New in 0.20.0

- A floating Talk panel shared by the dashboard tab and the Desktop Talk view, with a runtime that survives collapsing the controls and browsing other tasks.
- `POST /text/input` and a `textInput` descriptor on `GET /status`: one authenticated route for explicit operations from the panel.
- Live replay — `POST /live/speech` with `replay:true` re-announces a terminal result into the exact bound Live session.
- `POST /native/attach` accepts `input_mode:"typed"` for microphone-off use that mints no voice credentials, plus read-only recipient catalog, history, status and selection routes.
- Result presentation across transports: result ready, context submitted, playback started, playback finished, interrupted and unknown stay separate facts; native terminal and Discord gain `/replay EVENT_ID`.
- Talk inside the current Desktop conversation, plug-and-play: open a conversation, **Talk**, **Connect**.

Every version with its receipts: [CHANGELOG.md](CHANGELOG.md).

## Providers and billing

| Lane | How it authenticates | Default model | Surfaces |
|---|---|---|---|
| OpenAI Realtime (`TALK_PROVIDER=openai`, default) | ChatGPT subscription through `codex login`, or `TALK_OPENAI_API_KEY` / `OPENAI_API_KEY` | `gpt-realtime-2.1` | every surface |
| GPT-Live (`TALK_VOICE_MODE=live`) | `TALK_LIVE_AUTH=subscription` (the default, on the Codex subscription) or explicitly chosen `api` billing; no automatic paid fallback | `gpt-live-1-codex` (subscription) / `gpt-live-1` (API) | terminal, Discord, dashboard, Desktop — on an explicit task |
| xAI Grok Voice (`TALK_PROVIDER=grok`) | an X Premium or SuperGrok login (`hermes auth add xai-oauth`) — **no API key** — or `TALK_XAI_API_KEY` / `XAI_API_KEY` | `grok-voice-latest` | terminal + Discord |
| Gemini Live (`TALK_PROVIDER=gemini`) | `GEMINI_API_KEY` / `TALK_GEMINI_API_KEY` — free-tier AI Studio keys work | `gemini-3.1-flash-live-preview` | terminal + `hermes realtime`; Discord refuses it for now |
| Cascade voice (`TALK_VOICE_MODE=cascade`) | `TALK_ELEVENLABS_API_KEY` / `ELEVENLABS_API_KEY`, on top of the OpenAI Realtime lane | `eleven_flash_v2_5` | terminal, Discord, dashboard |

The provider knob is fail-closed and never inferred from which keys exist.
Per-lane detail and the credential order: [docs/PROVIDERS.md](docs/PROVIDERS.md).
Speaking in a voice of your own: [docs/CASCADE.md](docs/CASCADE.md).

## Surfaces

| Surface | Entry point |
|---|---|
| Terminal | `hermes talk` — task attachment with `hermes talk --task TARGET` |
| Inside a Hermes session | `/talk` — the one surface with an **attached** agent loop, so lookups and delegation answer inline |
| Discord voice channel | `/voice join`, then `/talk join [TARGET]` |
| Dashboard **Talk** tab | `hermes dashboard`, select a task, **Start** |
| Desktop **Talk** composer action | open a conversation → **Talk** → **Connect** ([host requirements](docs/DESKTOP.md)) |

## Is it working?

```bash
hermes plugins list        # → hermes-talk · enabled · current version
hermes talk doctor         # → read-only: auth lane, provider, model/voice, audio, host lanes
hermes talk check          # → doctor + one live provider turn + one bounded Hermes run
# then, in any session: say "status report" — talk_status answers with
# version, auth lane, agent lane, and audio state.
```

Doctor is read-only by design: it names which lane came up and what is missing,
and never writes, probes, or refreshes a token. `check` is the other half and is
deliberately **not** read-only — one short provider turn and one short agent run,
exit 0 only if every step passed. A mock can never go green.

**Filing an issue?** `hermes talk diagnostics --bundle` writes one redacted,
owner-only file — versions, the *names* of the variables you have set, device and
host facts, and every doctor outcome; no values, logs, prompts, transcripts,
audio, or paths. It is safe to paste into a public issue and it is what the
[bug template](.github/ISSUE_TEMPLATE/bug_report.yml) asks for.

**Upgrade** with `hermes plugins update hermes-talk` — not a second `install` —
then **restart the gateway**: a running process keeps executing the old code
until you do.

The full diagnostic walk, every receipt, and the upgrade runbook:
[docs/OPERATING.md](docs/OPERATING.md#verify--the-receipts).

## Documentation

Everything Hermes Talk does, in depth:

- [OPERATING.md](docs/OPERATING.md) — install, upgrade, use, every knob, the three agent lanes, the Discord lane, the dashboard tab, current boundaries, troubleshooting.
- [PROVIDERS.md](docs/PROVIDERS.md) — per-lane provider detail and the fail-closed OpenAI credential order.
- [BACKGROUND-WORK.md](docs/BACKGROUND-WORK.md) — delegation, admission control, steering a running agent, and the capability bridge.
- [CASCADE.md](docs/CASCADE.md) — the cascade lane: your own ElevenLabs voice over a realtime provider.
- [GPT-LIVE.md](docs/GPT-LIVE.md) — GPT-Live billing and voice, task attachment, Codex workers, operator acceptance.
- [DESKTOP.md](docs/DESKTOP.md) — Talk in the Hermes desktop app and the host support it requires.
- [VOICE-COMMANDS.md](docs/VOICE-COMMANDS.md) — say this, hear this, and what each spoken receipt commits to.
- [REALTIME-ORCHESTRATOR.md](docs/REALTIME-ORCHESTRATOR.md) — architecture map of the tool-calling realtime lane.
- [dashboard-task-continuity.md](docs/dashboard-task-continuity.md) — joining, continuing and reconnecting to a canonical Hermes task.
- [recipient-routing.md](docs/recipient-routing.md) — addressing an existing application task from a voice task.
- [codex-workers.md](docs/codex-workers.md) — selecting a Codex background worker from a bound task.
- [task-event-projection.md](docs/task-event-projection.md) — the worker-side library that restores task and work state.
- [passive-attachment-client.md](docs/passive-attachment-client.md) — the shared passive-history client used by all three surfaces.
- [PROVIDER-RECEIPT.md](docs/PROVIDER-RECEIPT.md) — how to report a provider lane that worked, or broke, for you.
- [CAPABILITY-KERNEL-PORT.md](docs/CAPABILITY-KERNEL-PORT.md) — the capability-plugin kernel adaptation guide.

## Design rules

The three that shaped everything else:

- **Nothing fails quietly.** A degraded backend, a missing tool, a run whose
  watcher died — each is said out loud in the conversation. A voice surface
  that silently does less than you asked is worse than one that refuses.
- **The credential never leaves the process.** Key or OAuth token hits exactly
  one OpenAI endpoint (the mint) and the socket only ever sees the ephemeral
  secret it returns.
- **Hermes owns the tools and the session.** The Realtime layer is ears, mouth,
  and turn-taking. It never owns the agent loop.

## Background

Hermes Talk began as a plugin and became a reference implementation for the
speech-to-speech contract Hermes core now carries:

- [RFC NousResearch/hermes-agent#77111](https://github.com/NousResearch/hermes-agent/issues/77111)
  — filed from this repo: a `RealtimeVoiceProvider` ABC for Hermes core.
- [PR NousResearch/hermes-agent#101808](https://github.com/NousResearch/hermes-agent/pull/101808)
  — the core contract, orchestrator, and first built-in provider, ported from
  this plugin's orchestrator and OpenAI transport. hermes-talk already publishes
  its three lanes on that contract
  ([details](docs/OPERATING.md#hermes-core-realtime-contract)).
- [PR NousResearch/hermes-agent#97325](https://github.com/NousResearch/hermes-agent/pull/97325)
  — a pointer to this plugin on the official Voice Mode docs page.

## Status

Under active development; the PyPI badge above is the released version.
Every version with its receipts: [CHANGELOG.md](CHANGELOG.md). Open epics and
threads:
[#19](https://github.com/TheSmokeDev/hermes-talk/issues/19) provider-neutral
Realtime voice platform,
[#32](https://github.com/TheSmokeDev/hermes-talk/issues/32) operator-grade
orchestration UX,
[#43](https://github.com/TheSmokeDev/hermes-talk/issues/43) Talk over the Bot
Mode roster,
[#44](https://github.com/TheSmokeDev/hermes-talk/issues/44) channel-neutral
voice transport.

## Contributing

`uv sync --extra dev` (or `pip install -e ".[dev]"`), `pytest -q`, `ruff check .`
— offline, no keys, seconds. Priorities, the path for each kind of change
(a provider, a surface, a tool, a fix), the merge bar, and the one test trap
on a box that has Hermes installed: [CONTRIBUTING.md](CONTRIBUTING.md).
First response within 24 hours;
[`good first issue`](https://github.com/TheSmokeDev/hermes-talk/issues?q=is%3Aopen+label%3A%22good+first+issue%22)
fits in one sitting; a provider that works or broke for you is a
contribution too ([docs/PROVIDER-RECEIPT.md](docs/PROVIDER-RECEIPT.md)).

Contributors adapting The Homie's v1.7.0 capability-plugin lessons to Hermes
should use the [capability-kernel port plan](docs/CAPABILITY-KERNEL-PORT.md).
It maps the reusable safety and lifecycle contracts onto Hermes-owned APIs;
it does not claim that hot lifecycle support already exists here.

### Contributors

[@danclaw93](https://github.com/danclaw93): room-scoped spoken approvals send
the `request_id` the Hermes run API reads, so they stop failing with HTTP 400
(0.17.1).

[@kvnloo](https://github.com/kvnloo): PulseAudio WebRTC echo cancellation
on Linux, and the fix that stopped quiet words being clipped during
playback ([#81](https://github.com/TheSmokeDev/hermes-talk/pull/81));
semantic turn-detection controls across the three lanes
([#107](https://github.com/TheSmokeDev/hermes-talk/pull/107), in review).
[@TheAngryPit](https://github.com/TheAngryPit) — a renderer-owned Realtime
transport for the Hermes desktop app that keeps core as the single chat
authority ([#80](https://github.com/TheSmokeDev/hermes-talk/pull/80), in
review). [@webdevtodayjason](https://github.com/webdevtodayjason) —
field-tested feedback from a second live consumer on the upstream
`RealtimeVoiceProvider` contract these lanes register on
([hermes-agent#81404](https://github.com/NousResearch/hermes-agent/pull/81404)).

## License

[MIT](LICENSE). Adapted-source licenses and contributor credits are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
