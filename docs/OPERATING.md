# Operating hermes-talk

The [README](../README.md) says what this is and why it's built the way it
is. This page says how to run it — and, because that's the house style, how
to **prove** it's running instead of assuming it is.

## Prerequisites

- **Python ≥ 3.11** (the host's Python; `pyproject.toml` enforces it).
- **Hermes Agent ≥ v0.17** — everything works on a stock v0.17 install.
  One verb is version-gated: `redirect_agent`'s clean-abort path needs the
  host's public `AIAgent.redirect()` (**0.20+**). Below that it degrades
  to the steer queue and the spoken reply says queued — never a fake
  abort. (See the README's [redirect section](../README.md#redirecting-work-thats-already-running).)
  Subagent completion announcements have a second compatibility boundary:
  they require a Hermes release exposing
  `PluginContext.active_parent_session_id` (upstream
  [PR #79716](https://github.com/NousResearch/hermes-agent/pull/79716)). On older Hermes the
  rest of Talk remains compatible, but those announcements fail closed and
  stay silent rather than risking a foreign session's result being spoken.
- **Audio**: the terminal session needs the `[audio]` extra
  (`sounddevice` + PortAudio). The dashboard tab uses the browser's mic
  instead and needs nothing installed locally.
- **One credential, any of three lanes**: a Talk-scoped key
  (`TALK_OPENAI_API_KEY`), a shared key (`OPENAI_API_KEY`), or a ChatGPT
  subscription via Codex CLI OAuth (`codex login`). No key is required if
  the third lane is signed in.

## Install

```bash
hermes plugins install TheSmokeDev/hermes-talk --enable
pip install "hermes-talk[audio]"   # terminal audio; skip if dashboard-only
hermes talk
```

## Upgrade

```bash
hermes plugins update hermes-talk
```

Two traps, both named:

1. **`install` refuses on an existing plugin** — upgrading is `update`,
   not a second `install`.
2. **A running gateway keeps executing the OLD code** until you restart
   it. The plugin is loaded at process start; the files on disk changing
   does nothing to a live process. After any plugin update:

   ```bash
   hermes gateway restart   # or restart however you run your gateway
   ```

If you installed the pip package too, update it alongside:
`pip install -U "hermes-talk[audio]"`.

## Verify — the receipts

Nothing below requires talking. Each step has an expected output; if you
see it, that layer is proven.

### 1. Registration

```bash
hermes plugins list      # → hermes-talk · enabled · <version>
hermes talk --help       # → the talk command's own help text
```

The second command is the deeper receipt: the CLI surface only exists if
the plugin registered inside the host.

### 2. `hermes talk setup` — confirmation-gated configuration

```bash
hermes talk setup
```

Setup first runs the read-only detector, asks only decisions that are unresolved
(auth, incompatible/unknown model, or invalid voice), and shows the exact
`.env` key plus winning Hermes-home path before each write. Every setting needs
its own `yes`; declined settings are untouched. The complete confirmed set is
staged in a unique, securely created sibling file and committed to that home's
`.env` with one atomic replacement while the current process environment is
updated under the same rollback boundary. Existing permissions are preserved;
new secret files use POSIX `0600` or a protected owner-only Windows DACL. Any
apply error is caught, every staged path is cleanup-attempted and independently
verified, and file/environment changes are rolled back when possible. A temp
that survives denied cleanup counts as a surviving mutation: the value-free
receipt says `failed` and emits only its redacted staged slot plus error class.
Otherwise the receipt says `applied` or `rolled-back`. Setup reruns doctor
immediately after the transaction attempt and uses that report plus the apply
receipt as its verification/exit result. API-key input is hidden and never
echoed. If an existing metered key is blocked only by
`TALK_PREFER_CODEX_OAUTH=true`, choosing the key reuses it and separately asks
to write the required `false` policy transition. If the only missing action is
`codex login`, setup names it and writes nothing.

Dotenv setting names follow the target platform: Windows matching is
case-insensitive and collapses duplicate case variants to one canonical entry;
POSIX matching remains case-sensitive.

### 3. `hermes talk doctor` — read-only readiness

```bash
hermes talk doctor
hermes talk doctor --json
```

Both forms run the same deterministic checks: plugin registration receipts,
the winning auth lane, model compatibility/voice validity, audio dependency availability,
identity section counts plus active profile/root provenance, Discord operator
configuration, and host capability surfaces. The JSON envelope is versioned
with `schema_version: 1` and marks `read_only: true`.

Doctor performs no setup or repair. In particular it does not refresh expired
OAuth, rewrite `auth.json`, create Hermes state directories, open audio devices,
start services, or probe an api-server sidecar. Remediations are instructions
only. Identity content, credentials, and Discord IDs are never included; only
section/operator counts and lane/state receipts are emitted.

### 4. `talk_status` — the in-session command

In any session (voice, or the dashboard's tool relay), ask for a status
report. The model calls `talk_status` and reads back a JSON object. What
good looks like, field by field:

| Field | Healthy value | If not |
|---|---|---|
| `version` | the version you just installed | old version → the gateway wasn't restarted (see Upgrade) |
| `model` / `voice` | your configured model and voice | a config error raises before this point |
| `attached_to_hermes` | `true` inside a Hermes process | `false` standalone is normal |
| `agent_lane` | `"attached"`, `"api-server"`, or `"out of process"` | this is the lane a real delegation would take RIGHT NOW — not a boolean that was true for a different reason |
| `audio_available` | `true` | `false` → the `[audio]` extra or a device is missing (terminal lane only) |
| `identity` | section names with character counts — never content | empty → no identity files resolved (fine standalone) |
| `auth` | `{configured: true, source: ..., detail: ...}` | `configured: false` → no credential on any lane |
| `registration_failures` | **absent** | present → a surface failed to register; each line names which |

### 5. Dashboard

With the Hermes dashboard running, `GET /api/plugins/hermes-talk/status`
returns the same auth/lane/voice picture as JSON. From the machine itself
no token is needed; from anywhere else the route requires
`TALK_DASHBOARD_TOKEN` (see Configuration).

### 6. The wire canary — proving a session mints and connects

The full end-to-end proof short of speech. `hermes talk` buffers its
stdout when backgrounded, so **do not judge it by a redirected log file** —
judge it by the process and the socket:

```bash
hermes talk &            # or run it in a second terminal
# give it ~10s to mint and connect, then:

# Linux:   ss -tpn state established '( dport = :443 )' | grep -i python
# macOS:   lsof -iTCP:443 -sTCP:ESTABLISHED | grep -i python
# Windows: Get-NetTCPConnection -State Established -RemotePort 443
```

An **established TLS connection to port 443** from the talk process means:
the credential resolved, the ephemeral session minted, and the Realtime
WebSocket is open and held. That is a live session waiting for a voice.
Hang it up cleanly (Ctrl+C in its terminal, or stop that specific process
— only that one).

## Configuration — every knob

All variables are resolved at call time, never cached at import. The
README's [Knobs table](../README.md#knobs) covers the common ones; this is
all of them. Canonical source: `talk_config.py` and `talk_auth.py`.

### Session

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_MODEL` | `gpt-realtime-2.1` | Doctor certifies only exact ids in its bounded duplex-audio + function-calling policy. Known transcription/translation-only models fail; other Realtime-shaped ids are an honest `syntax-only` / compatibility-unknown warning, not a validity claim. |
| `TALK_VOICE` | `cedar` | **Fail-closed**: any id outside the known voice list refuses with the valid names rather than letting the API pick silently. |

### Audio (terminal lane)

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_INPUT_DEVICE` | auto | sounddevice input override. List devices: `python -c "import sounddevice; print(sounddevice.query_devices())"` |
| `TALK_OUTPUT_DEVICE` | auto | sounddevice output override. |

### Identity

What a session knows before you say anything, and where each part comes from:

| Section | Source | Absent when |
|---|---|---|
| `PERSONA` | `SOUL.md`, through Hermes's own loader (which injection-scans it) | hermes-agent is not importable, or SOUL.md is empty |
| `USER` | `<hermes_home>/memories/USER.md`, read directly | the file is missing or blank |
| `MEMORY` | `<hermes_home>/memories/MEMORY.md`, then a one-line pointer at `search_vault` | no file **and** no usable memory provider |
| `WORKING` | nothing yet — declared, no producer | always |

`USER` and `MEMORY` are read from disk rather than through an agent, so they
work on all three lanes. The gateway and the dashboard have no agent and no
plugin context, and those are the lanes most likely to be asked "what do you
know about X".

Two budgets apply in order: the host's own `memory.user_char_limit` /
`memory.memory_char_limit` from `config.yaml` (its WRITE budget, reused here
as a read cap), then this plugin's per-section cap. A section that resolves
empty is omitted entirely rather than rendering a header with nothing under
it. `talk_status` reports character COUNTS per section and never content.

The session instructions also carry the current date and time, built per
session — a voice assistant that cannot say what day it is fails the first
obvious question.

**Discord speaker attribution is the authorization floor.** For
each decoded audio packet, Talk snapshots the receiver's current SSRC mapping and
PCM together under the receiver lock, then gives the model the immutable Discord
user ID plus display name immediately before that exact audio. The display name
is JSON-quoted, bounded, and explicitly marked as untrusted profile data inside
a persistent `role=system` context item; it is never a `role=user` turn. Speaker
transitions replace the previous context in the same serialized outgoing batch
as the PCM, without creating a response. If an SSRC is unknown, Talk reports it
as unresolved and unauthorized and never guesses that it belongs to the
operator. Attribution changes are deduplicated by immutable user ID even when
Discord changes SSRC, and stop/rejoin/remap invalidates stale queued callbacks.

Authorization itself is enforced outside the model at tool execution time.
Discord sessions disable VAD's automatic response creation, resolve the exact
`audio_start_ms`/`audio_end_ms` interval against the packet ledger, and create
the response with an opaque binding token. A function call must carry a server
response ID that resolves through that token to exactly one immutable Discord
user ID. Tool continuations preserve the same token; bounded item/response
ledgers are released on completion and cleared on teardown. Display names,
SSRC alone, model arguments, and "last speaker" state are never authority.

Configured IDs may run the four state-changing tools: `delegate_task`,
`steer_agent`, `redirect_agent`, and `stop_work`. Other speakers retain normal
conversation and the read-only tools (`search_memory`, `search_vault`,
`check_work`, `list_agents`, `talk_status`, and `talk_capabilities`). Missing
response correlation,
an unresolved speaker, two speakers in one VAD turn, or a speaker outside the
allowlist returns a non-sensitive spoken denial without running the handler.

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_IDENTITY_INCLUDE` | all sections | Comma-separated section list. **REPLACES the default set, does not extend it** — the trap and the budgets are documented in the [README](../README.md#talk_identity_include--what-the-session-starts-knowing). Unknown names are dropped silently. |
| `TALK_DISCORD_OPERATOR_USER_IDS` | nobody | Comma-separated immutable decimal Discord user IDs authorized for mutating tools in Discord voice. Unset/blank = nobody. **Any** blank or malformed entry invalidates the whole list and authorizes nobody; valid entries are never partially accepted. Example: `TALK_DISCORD_OPERATOR_USER_IDS=<your-user-id>,<another-user-id>`. |

### Agent lanes

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_AGENT_PROFILE` | auto-detect | Hermes profile for the detached spawn. **Set-but-blank = explicit opt-out** (never pass `--profile`). Full story: [README](../README.md#talk_agent_profile--which-profile-the-background-agent-runs-under). |
| `TALK_AGENT_TIMEOUT_S` | `1800` | Wall-clock budget for one background run and its watcher. Junk or ≤0 silently takes the default. |

### api-server lane

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_API_SERVER_URL` | `http://127.0.0.1:8642` | Gateway base URL; trailing slash stripped. |
| `TALK_API_SERVER_KEY` | falls back to `API_SERVER_KEY` | Bearer key for the lane. **Set-but-blank = send no Authorization header.** |
| `API_SERVER_KEY` | unset | The gateway's own variable, reused as fallback so you configure the key once. |
| `TALK_API_SERVER_PROBE_TIMEOUT_S` | `1.5` | Budget for one availability probe — tight on purpose; it runs on the mic's event loop. |
| `TALK_API_SERVER_PROBE_TTL_S` | `30.0` | How long a probe verdict is trusted before an off-hot-path refresh. |
| `TALK_API_SERVER_POLL_S` | `1.0` | Poll interval while waiting on a run. |

### Dashboard

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_DASHBOARD_TOKEN` | unset | Unset = the four routes answer **loopback only**. Set = the token is required from everywhere, compared constant-time. Set-but-blank reads as unset. Details: [README](../README.md#talk_dashboard_token--the-tabs-own-gate). |

### Auth

| Variable | Default | Effect / failure mode |
|---|---|---|
| `TALK_OPENAI_API_KEY` | unset | Talk-scoped key, first in order. **Set-but-empty is a hard refusal, never a fall-through.** |
| `OPENAI_API_KEY` | unset | Shared key, second in order. Same set-but-empty refusal. |
| `CODEX_HOME` | `~/.codex` | Where the Codex OAuth lane (third in order) reads `auth.json` from. |
| `TALK_PREFER_CODEX_OAUTH` | unset | Absent/`false` preserves the order above. `true` requires Codex OAuth and ignores API keys; missing/unusable OAuth refuses. Blank or invalid values also refuse. |

### Host

| Variable | Default | Effect / failure mode |
|---|---|---|
| `HERMES_HOME` | platform default | The host resolves context override → process `HERMES_HOME` → platform default. Doctor compares host API results using the host's exact `Path(value)` semantics, including literal tilde/relative values and Windows defaults; it reports unknown provenance when those semantics cannot be established. Determines the state dir (`$HERMES_HOME/state/`, home of `talk-runs.jsonl`) and is inherited by spawned children. |

(One internal: the presence of `PYTEST_CURRENT_TEST` disables the durable
run-history tee so test suites can't write into a real Hermes home.)

## Discord voice — talking in the channel Hermes is already in

Inside the gateway, `/talk join` runs the call in the Discord voice
channel the host is already sitting in. `/talk leave` ends it, `/talk
status` reports whether a session is live. Outside the gateway (a plain
terminal) `/talk` still means the terminal session, and those
subcommands say so.

Before inviting Talk into a shared voice room, set the operator list in the
gateway environment and restart the gateway:

```bash
TALK_DISCORD_OPERATOR_USER_IDS=<your-immutable-discord-user-id>
```

Use Discord's numeric user ID, not a username or display name. With the variable
unset, every participant can converse and use read-only tools, while every
mutating tool is denied.

We do **not** open a second Discord connection. Hermes already has one,
already decrypts DAVE, and already decodes Opus in this process — we
borrow it, so it is one bot, one connection, and the host's own E2EE.
Hermes exposes no plugin hook for voice, so for the duration of a call
the plugin takes over three host surfaces and hands them back on stop:
the voice receive listener, `play_in_voice_channel` (so the host does
not fight us for the speaker), and the inactivity-timer mode getter.

**One asterisk on "hands them back": the ambient mixer is not restored,
deliberately.** Taking over playback makes Discord close that mixer
permanently — and a closed mixer still reports itself as speaking, so
returning it would make every later host voice reply stall for the full
playback timeout and then vanish. Dropping it instead falls back to the
host's plain playback path, which works. The practical consequence: if
you had the ambient bed enabled (`discord.voice_fx`), it is gone until
the bot next rejoins the channel. That is the smaller permanent loss,
chosen over the larger one.

Known limitation: if the bot leaves and rejoins the channel (manually,
or through a voice-server reconnect) while a session is live, the bridge
is left pointing at the old connection and the call goes quiet — it
fails to silence, never to a wrong answer. Say `/talk leave` and rejoin.
Tracked as [#8](https://github.com/TheSmokeDev/hermes-talk/issues/8).

## Troubleshooting

**"Invalid length for parameter modelId, value: 0"** — the detached agent
spawned without a model config because yours lives in a profile. Set
`TALK_AGENT_PROFILE=<name>`, or check why auto-detection didn't find it.
[Full story in the README.](../README.md#talk_agent_profile--which-profile-the-background-agent-runs-under)

**"OpenAI Realtime auth failed (401)" on the Codex lane** — the OAuth
token expired. Run `codex login` to refresh the ChatGPT sign-in, then
start the session again.

**"401" with a key configured** — the key itself was rejected. Remember
the order: `TALK_OPENAI_API_KEY` beats `OPENAI_API_KEY` beats Codex OAuth,
and a set-but-empty variable REFUSES rather than falling through — an
empty `TALK_OPENAI_API_KEY` blocks a perfectly good key below it.

**No audio device / sounddevice errors** — install the extra:
`pip install "hermes-talk[audio]"`. Still failing: list devices (command
in the Audio table above) and pin `TALK_INPUT_DEVICE`/`TALK_OUTPUT_DEVICE`.
The check runs before any network call — a missing mic never wastes a mint.

**"TALK_VOICE must be one of…"** — fail-closed by design. Pick from the
listed names; the error text is the complete menu.

**Dashboard returns 401/403** — from a remote machine with no
`TALK_DASHBOARD_TOKEN` set, that's the loopback-only default doing its
job. Set the token on the gateway and present it from the browser.

**A run reports `lost`** — it started under a previous process. The
detached child may well have finished its work; what died was the watcher
that would have spoken the result. `lost` means "this process cannot know
either way" — an honest answer, not a failure.

**"I can't watch for delivery on this build"** — the steer queued fine,
but neither delivery artifact is observable in this process, so
confirmation will never upgrade past queued. Everything still works; you
just won't hear "landed."

**Plugin updated but behavior didn't change** — the gateway is still
running the old code. Restart it (see Upgrade).

## Dashboard routes (reference)

Four routes, mounted at `/api/plugins/hermes-talk/`, every one gated by
the same auth check by construction (a guard-coverage test fails the suite
if a new route ships ungated):

| Route | What it does |
|---|---|
| `GET /status` | Auth lane, model, voice list, plugin version, and the live `agentLoop` lane tile. The page's first call — and deliberately the one that pays for the cold api-server probe. |
| `POST /session` | Mints one ephemeral Realtime session. The raw key/OAuth token never leaves the server process — the browser only ever sees the ephemeral secret. 503 = no credential; 502 = wire error. |
| `POST /tool` | Relays one model function call into the tool surface. Unknown tool = 400 (client bug); a known tool that fails = 200 with speakable failure text. |
| `GET /runs` | Recent background runs, history included, so a run from a previous process surfaces as `lost` rather than vanishing. |

One honest caveat: the dashboard's backend runs in the web-server process,
which has no attached plugin context — agent-loop-only capabilities
degrade to their announced fallbacks there, exactly as `talk_status`
reports.
