# hermes-talk

**Realtime voice as an orchestrator for [Hermes Agent](https://github.com/NousResearch/hermes-agent) — talk to it, it runs agents, it reports back out loud.**

![The Talk tab in the Hermes dashboard — a live voice session, a background agent delegated mid-conversation, its result landing in the runs panel (8× speed)](docs/dashboard.gif)

*Real session, 8× speed. 🔊 [Watch it with sound](https://github.com/TheSmokeDev/hermes-talk/releases/download/v0.3.0/hermes-talk-dashboard-cut.mp4) — 2:27: delegate, keep talking, hear the result land. This is a voice demo; the sound is the point. Recorded at v0.3.0 — the flow is unchanged.*

## What this actually is

Not dictation. Not read-my-reply-aloud. A conversation you can hand work to
while it's still going:

> **You:** audit the auth module for error-handling gaps and report back
> **Hermes:** starting that now — run one.
> **You:** while that runs — what did we decide about the retry policy?
> **Hermes:** *(searches your past sessions)* three attempts with exponential backoff, decided on the 14th…
> **You:** how's that audit going?
> **Hermes:** run one's still working, about two minutes in.
> *…later, unprompted:*
> **Hermes:** that audit finished — three gaps, starting with the token refresh swallowing exceptions…

Four properties make that possible, and each one is the part other voice
integrations don't have:

1. **Duplex.** One bidirectional audio session — turn-taking, interruption,
   and tool calls happen *inside* the speech layer, not around it. Cut it off
   mid-sentence and it stops, because it never stopped listening.
2. **Its tools are Hermes's tools.** Realtime function calls relay straight
   into the agent's real tool surface. Ask it something it can't know and you
   hear it go look, then answer from what it found.
3. **Work outlives the sentence.** Delegation spawns a real background Hermes
   agent. You keep talking. The result is spoken when it lands — you don't
   poll, you don't wait, you don't go check a terminal.
4. **It starts already knowing you.** The session prompt is assembled from
   what Hermes itself knows — your `SOUL.md`, and whatever your configured
   memory provider contributes. Install one (e.g.
   [hermes-homie-memory](https://github.com/TheSmokeDev/hermes-homie-memory))
   and the first thing you say lands on an agent that already has context. No
   tool call, no "let me look that up", no warm-up turn.

**Why this exists:** OpenAI shipped this exact pattern for Codex on 2026-07-23
— voice as a control layer over concurrent agents. It's excellent, and it's
closed: paid ChatGPT plans only, and GPT-Live has no developer API. Their own
docs point builders back at the Realtime API. So that's what this is built on,
for an agent you actually own.

Hermes's built-in voice mode is good and this doesn't replace it — turn-based
STT → inference → TTS is the right shape for plenty of work. This is the other
shape.

## Install

Needs Python ≥ 3.11 and a Hermes host ≥ v0.17. `redirect_agent`'s
clean-abort path wants 0.20+ and [degrades honestly below it](#redirecting-work-thats-already-running).
The plugin remains backward-compatible with older hosts, but session-owned
subagent completion announcements require a Hermes release exposing
`PluginContext.active_parent_session_id` (upstream
[PR #79716](https://github.com/NousResearch/hermes-agent/pull/79716)); without
that property, announcements are suppressed rather than guessed. Details are in
[docs/OPERATING.md](docs/OPERATING.md#prerequisites).

```bash
hermes plugins install TheSmokeDev/hermes-talk --enable
pip install "hermes-talk[audio]"   # mic + speaker support (sounddevice)
hermes talk
```

Zero core edits — pure `register(ctx)` plugin surface, proven on a stock
v0.17.0 install. 650+ offline tests, CI on ubuntu + windows × py3.11–3.13.

## Quickstart — your first call on each surface

**Terminal** (simplest — start here):

```bash
hermes plugins install TheSmokeDev/hermes-talk --enable
pip install "hermes-talk[audio]"
hermes talk        # you are live; speak. Ctrl+C hangs up.
```

**Discord** (the call happens inside a voice channel, not in chat):

1. Hermes's Discord adapter is connected and its bot is in your server
   (that's the host's own setup, not this plugin's).
2. Put Hermes in a voice channel first: `/voice join`. Talk borrows that
   connection — it never opens a second one.
3. Then `/talk join` starts the Talk session on that channel. `/talk leave`
   (or leaving the channel) ends it. `/voice leave` disconnects Hermes
   entirely.
4. Want mutating tools (`delegate_task`, `steer_agent`, …) in a shared
   channel? Set `TALK_DISCORD_OPERATOR_USER_IDS=<your Discord user id>` and
   restart the gateway. Without it everyone can talk but nobody can mutate —
   that fail-closed default is intentional.

**Dashboard** (browser, no mic drivers needed): with the gateway running,
open the Hermes dashboard and use the **Talk** tab — start the session from
there. Audio is browser-native.

In every case, `hermes talk doctor` (or saying "status report" on the call)
tells you which lane came up and what is missing. If a surface shows
nothing, run doctor first — it names the gap.

## Verify your install

```bash
hermes plugins list    # → hermes-talk · enabled · current version
hermes talk --help     # → registration proof: the command only exists if the plugin loaded
hermes talk setup      # → guided, confirmation-gated setup plus doctor verification
hermes talk doctor     # → read-only human diagnostics
hermes talk doctor --json  # → the same versioned receipt for scripts/issues
# then, in any session: say "status report" — talk_status answers with
# version, auth lane, agent lane, and audio state.
```

**Upgrade** with `hermes plugins update hermes-talk` — not a second
`install` (it refuses on an existing plugin) — then **restart the
gateway**: a running process keeps executing the old code until you do.
Full runbook, wire canary included: [docs/OPERATING.md](docs/OPERATING.md#verify--the-receipts).

Contributors adapting The Homie's v1.7.0 capability-plugin lessons to Hermes
should use the [capability-kernel port plan](docs/CAPABILITY-KERNEL-PORT.md).
It maps the reusable safety and lifecycle contracts onto Hermes-owned APIs;
it does not claim that hot lifecycle support already exists here.

## Auth — no API key needed if you have ChatGPT

Signed into the [Codex CLI](https://github.com/openai/codex) (`codex login`)?
Talk runs on your own ChatGPT subscription's Realtime entitlement — no key, no
per-minute API bill. Bring a key instead if you'd rather.

Resolved fail-closed in this order:

1. `TALK_OPENAI_API_KEY` — a Talk-scoped API key (set-but-empty refuses, never
   falls through)
2. `OPENAI_API_KEY` — the shared environment key
3. **Codex OAuth** — no key at all: if you're signed into the
   [Codex CLI](https://github.com/openai/codex) (`codex login`), Talk rides
   your own ChatGPT subscription's Realtime entitlement. Expired tokens
   refresh automatically and write back atomically, so the Codex CLI keeps
   working.

That historical order remains unchanged when `TALK_PREFER_CODEX_OAUTH` is
absent or explicitly false. Set `TALK_PREFER_CODEX_OAUTH=true` to require the
subscription lane even when API keys exist. The preference is fail-closed: a
missing/unusable Codex login refuses instead of spending a metered key, and a
blank or invalid preference refuses until corrected. `hermes talk doctor`
names the winning lane and distinguishes valid OAuth from an expired credential
that still requires a successful refresh; it never prints the key or token.
When setup offers the API-key lane under an enabled OAuth preference, it reuses
an existing metered key when present and separately confirms the required
`TALK_PREFER_CODEX_OAUTH=false` policy transition.

Whatever the lane, the session is minted server-side into an **ephemeral
client secret** — the raw key or OAuth token touches exactly one OpenAI
endpoint and never reaches the socket, a log line, or a client.

## Providers — OpenAI (default), Grok, or Gemini

`TALK_PROVIDER` picks the realtime voice transport: `openai` (default,
everything above), `grok` (xAI Grok Voice), or `gemini` (Gemini Live). The
knob is fail-closed and never inferred from which keys exist — an operator
holding several gets the provider they named or an error, not a silent
switch.

The Grok lane needs an xAI key (`TALK_XAI_API_KEY`, falling back to
`XAI_API_KEY`; set-but-blank refuses), rides model `grok-voice-latest`
(override: `TALK_GROK_MODEL`), and offers five voices — `ara`, `rex`, `sal`,
`eve`, `leo` — via `TALK_GROK_VOICE` (fail-closed on unknown names). Same
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

## Custom voice — the cascade lane (ElevenLabs)

Native mode speaks with the provider's own voices, and those voices are
provider-locked. Cascade mode splits the call: the realtime provider stays
the brain (listening, thinking, tools, turn-taking) and hands its answer
TEXT to a streaming ElevenLabs TTS, so the assistant speaks in any voice on
your ElevenLabs account — including a clone of your own.

```bash
TALK_VOICE_MODE=cascade \
TALK_ELEVENLABS_VOICE_ID=<your-voice-id> \
hermes talk
```

The key comes from `TALK_ELEVENLABS_API_KEY` or `ELEVENLABS_API_KEY`
(Talk-scoped wins; set-but-blank refuses), and the TTS model defaults to
`eleven_flash_v2_5` (override: `TALK_ELEVENLABS_MODEL`). To clone your own
voice, create it in ElevenLabs VoiceLab first (VoiceLab → your voice → copy
the ID); voice management stays in your ElevenLabs account, not the plugin.

The trade, stated plainly: native provider audio starts ~300–600ms after
turn end, and the cascade adds roughly one extra half-second on the FIRST
sentence (sentence chunking plus TTS first-audio, ~490ms measured) — later
sentences pipeline under playback. You trade ~0.5s of first-word latency
for your voice.

Cascade is OpenAI-only for now (it is the one provider whose text-output
mode is wired and verified — picking grok or gemini fails closed and names
the provider). Barge-in cuts the cloned voice off exactly like native:
SpeechStarted aborts the in-flight TTS stream and drains playback in the
same synchronous step, so a cancelled sentence never speaks. A TTS failure
degrades that one answer to text-only with a single logged receipt; the
call itself survives. `TALK_VOICE_MODE` is fail-closed and defaults to
`native`, which is byte-identical to the pre-cascade behavior. Doctor gains
a `cascade` check: mode, TTS provider, redacted key presence, voice-id
status — no live probe.

The cascade speaks on every Talk surface:

| Surface | How the cascade speaks |
| --- | --- |
| Terminal (`hermes talk`) | The provider session opens in text-output mode; the cascade feeds the SAME playback sink the relay feeds. |
| Discord (`talk join`) | The same shared session loop; cascade PCM24k takes the relay's exact path through the 24k→48k voice-channel conversion. |
| Dashboard tab | The browser keeps its WebRTC socket but mints a text-output session and relays the model's text deltas to `POST /api/plugins/hermes-talk/cascade-tts`; the server-side cascade speaks them and streams PCM back. The ElevenLabs key never reaches the browser — the route sits behind the same `TALK_DASHBOARD_TOKEN` / loopback gate as the mint, and barge-in aborts the fetch, which cancels the TTS exactly like the terminal lane. |

## Use

```bash
hermes talk       # terminal duplex voice session
hermes talk setup # detect → ask only missing decisions → confirm/write → verify
hermes talk doctor # strictly read-only configuration and host diagnostics
```

Setup commits all individually confirmed settings to the active Hermes home's
`.env` as one secure atomic transaction and updates the current process to match.
On failure it rolls both surfaces back when possible, emits a value-free
applied/rolled-back/failed receipt, attempts and verifies every secret-bearing
temporary-file cleanup, and reruns doctor whenever a mutation may remain. Any
surviving temp is a surviving mutation: setup returns `failed` and identifies
the cleanup slot/error class without printing the path nonce or secret value.
New secret files keep POSIX `0600` behavior and receive a protected owner-only
DACL on Windows; an existing Windows destination DACL is preserved. A healthy
configuration asks no questions and performs no writes. Doctor never delegates
to setup.

or `/talk` inside an interactive Hermes session, which additionally reaches the
agent-loop-only tools (`memory`, `session_search`, `honcho_search`,
`delegate_task`). A spoken memory lookup tries the transcript first
(`session_search`) and remembered profile facts second (`honcho_search`), and
says which of the two it answered from — a recollection can be stale in a way
a verbatim line cannot, and nothing is on screen to check it against.

**In Discord**, `/talk join` runs the call in the voice channel Hermes is
already in — same conversation, same tools, same steering, in a room other
people can hear. Talk now reports speaker transitions to the model using the
member's immutable Discord user ID; display names are quoted as untrusted data,
and an unknown SSRC stays unresolved and unauthorized. Configure immutable IDs
with `TALK_DISCORD_OPERATOR_USER_IDS=<id>[,<id>...]`. Only those speakers may
run `delegate_task`, `steer_agent`, `redirect_agent`, or `stop_work`; everyone
may still converse and use read-only tools. Unset, blank, or any malformed list
authorizes nobody. Talk binds permission to the exact Discord PCM, VAD input
item, and opaque Realtime response metadata — never a display name, SSRC,
model argument, or whichever person spoke most recently. Mixed, missing, or
unresolved attribution fails closed with a spoken denial. Terminal microphone
and dashboard sessions retain their existing behavior. Talk borrows the host's
own voice connection rather than opening a second one. Details:
[docs/OPERATING.md](docs/OPERATING.md#discord-voice--talking-in-the-channel-hermes-is-already-in).

What can you actually say? The full say-this → hear-this card, with what
each spoken receipt commits to: [docs/VOICE-COMMANDS.md](docs/VOICE-COMMANDS.md).

## Dashboard tab

The demo at the top of this README is this tab. Start the dashboard and Talk
appears in the nav:

```bash
hermes plugins enable hermes-talk   # already done by `install --enable`
hermes dashboard                    # then open the Talk tab
```

Hit **Start**, allow the microphone, and talk. The page mints an ephemeral
secret server-side, dials OpenAI directly over WebRTC, relays every function
call back into the plugin's real tool surface, and shows the transcript plus a
live list of background runs. Nothing to install — the bundle ships with the
plugin and the host serves it.

**Memory writeback currently covers terminal and Discord Talk sessions.** Those
rooms share the server-side Realtime relay, which durably captures completed
turns. The dashboard's Realtime events stay in the browser, so matching durable
capture requires a separate authenticated transcript endpoint; until that lane
exists, the tab does not claim to write its conversation back to memory.

The tile at the top of the tab reads **attached**, **api-server**, or **out of
process** — which of the three agent lanes below this session would actually
use. It is not a guess; it is the lane the next tool call will take.

### `TALK_DASHBOARD_TOKEN` — the tab's own gate

Dashboard routes already sit behind the dashboard's session auth, but this
plugin's routes mint real credentials, so they carry a second check that never
fails open:

- **Unset (default): loopback only.** A browser on the same machine works with
  no configuration. Anything else is refused with a message naming this
  variable — including a request whose peer address this process cannot read
  at all, which is treated as remote rather than trusted.
- **Set: the token is required**, on loopback too, compared with
  `hmac.compare_digest`. Paste it into the field the tab offers when it gets
  refused; it's held in `sessionStorage`, so it dies with the tab.

Set it whenever the dashboard is reachable from anywhere but this machine.

## Reaching a real agent — the three lanes

Everything that needs an actual Hermes agent — a memory lookup, a delegated
task — goes down the same chain, and **every fall-through is said out loud**:

1. **Attached** — the agent loop this session is running inside. Only `/talk`
   has one. Answers come back inline, in the same breath.
2. **api-server** — a real, fully-tooled Hermes agent reached over the
   [api_server gateway platform](#turning-the-api-server-lane-on). This is what
   makes the dashboard tab and a standalone `hermes talk` more than a fallback.
3. **Out of process** — no agent lane. Delegation still spawns a detached
   `hermes -z` one-shot; a memory lookup refuses, naming exactly what's missing.

Lanes 2 and 3 answer with a receipt rather than the answer, and speak the
result when it lands. That is not a shortcut: an agent run takes seconds to
minutes, and the tool call that starts it runs on the same thread carrying your
microphone. Waiting there wouldn't be patience, it would be dead air.

### Turning the api-server lane on

```bash
# in your gateway environment
API_SERVER_ENABLED=true
API_SERVER_KEY=<a key you choose>
```

Restart the gateway. Talk finds it by itself — no Talk-side configuration is
needed, because `API_SERVER_KEY` is the same variable the gateway reads. If you
want Talk to use a *different* key or a non-default address, set
`TALK_API_SERVER_KEY` / `TALK_API_SERVER_URL`.

Talk probes `GET /v1/capabilities` (which is authenticated, on purpose) at
session start, so a wrong key is reported as **"running but rejected my key"**
rather than as "not reachable" — those send you to two different places.

## Background work

Say "go audit the site and tell me what's broken" and it starts a real agent,
then keeps talking to you. When the work lands, Talk speaks the result
unprompted. Ask "how's that going?" in the meantime and `check_work` answers.

Between the receipt and the landing, the session speaks bounded progress
milestones: "accepted", "executing — Reading files", "waiting on an approval",
and periodic "still working" heartbeats. The only detail that can name what a
job is doing is a safe label from a fixed table — never the tool's arguments,
paths, or output.

Known limitation: delivery is bound to the exact session that started the
work. If you disconnect before it lands, reconnecting on the *same* Hermes
session adopts and speaks what you were owed, exactly once — a different
session, or one with no durable Hermes context, never receives it.

Delegation walks the [three lanes](#reaching-a-real-agent--the-three-lanes) and
then one more, and **every fall-through is said out loud** — the plugin never
silently does less than you asked:

1. **Hermes's own agent loop** — inside `/talk`, where there's a parent agent
   to delegate into.
2. **A real agent over the api_server** — preferred over a spawn: it reuses a
   warm, fully-tooled agent instead of paying a process start.
3. **A detached `hermes -z` one-shot** — needs nothing enabled, so this is the
   lane that always exists as long as `hermes` is on the PATH.
4. None available — a refusal naming all three missing lanes.

### Redirecting work that's already running

Say "tell that audit to focus on the token refresh instead" and `steer_agent`
queues the note into the running agent. Steering is not stopping: the agent
sees the note after its current step, and the current step always finishes.

The honest part — and the reason this surface looks the way it does — is that
the host's steer primitive is a **queue write**. Queued is not delivered. So
every note gets a receipt with a state the substrate can actually prove:

| State | What proves it |
|---|---|
| `queued` | the steer call was accepted — the only claim made at call time |
| `landed` | one of the host's own drain artifacts fired: the post-tool-batch log line (matched by the correlation token each note carries), or the pre-API drain attributed to that exact agent |
| `redirected` | `AIAgent.redirect()` returned True on a live turn — the return value IS the artifact; that path emits no log line |
| `unconfirmed` | the agent finished and no landing was ever observed |
| `missed` | a patched host reported the note back as undelivered |
| `superseded` | the agent was stopped — stopping drops unread notes, by design |

Ask `check_work` and you hear the note's state in those words — never "they
got it" unless the artifact that proves it exists. Since v0.6 every note
travels as `[tk-xxxxxxxx] note` — the token is what the drain preview is
matched on, so two agents holding identical text can never land each
other's receipts. If a truncated sibling receipt has no exact agent reference,
it stays queued/unconfirmed: receipt order and a reused public subagent id never
let it inherit another receipt's generation. One substrate note: watching the
pre-API drain lowers the host's `agent.conversation_loop` logger to DEBUG (with
a gate filter so operator log output is unchanged) — any DEBUG-guarded
computation in that one module becomes active, a bounded perf cost traded for
killing the false-"unconfirmed" class. And you often don't have to ask: the
host's `subagent_stop` hook announces a finished background agent into the live
call the moment it lands. Those hook events are filtered to the parent session
that owns the call; foreign or ownership-less completions are never spoken.

Four tools carry the surface, discovery-first:

- **`list_agents`** — everything running, tagged `can steer` (live subagent
  ids) or `stop only` (run numbers). The model resolves "the research one"
  here, against ids that exist right now.
- **`steer_agent`** — subagent ids only. Prefers the host's public
  `steer_subagent` ([hermes-agent#76805](https://github.com/NousResearch/hermes-agent/pull/76805))
  when present; otherwise resolves the same delegation registry directly and
  calls the public `AIAgent.steer()`. A genuine host error is spoken, never
  routed around.
- **`redirect_agent`** — the stronger correction, for "stop, wrong repo":
  the host's public `AIAgent.redirect()` (0.20+) aborts the agent's
  in-flight thinking and retries with your correction, instead of waiting
  for the next tool boundary. Mid-tool it degrades to the steer queue and
  says so; on a pre-0.20 host it falls back to `steer_agent` entirely.
  Never cancels the work.
- **`stop_work`** — the one verb every lane supports: subagents via the
  host's `interrupt_subagent()`, api-server runs via `POST /v1/runs/{id}/stop`,
  detached one-shots via their retained process handle. Every "want me to
  stop it?" the refusals offer is backed by this tool — no offered action is
  fictional.

Runs on the api-server and detached lanes cannot be steered at all — those
lanes have no inbound channel — and the refusal says exactly that, then
offers the stop that actually works.

Runs are tracked in `$HERMES_HOME/state/talk-runs.jsonl`. The work is
detached, so ending the call does **not** stop it — but the watcher that would
have spoken the result dies with the session, so a run from a previous session
is reported as `lost`, never as "still running".

### `TALK_AGENT_PROFILE` — which profile the background agent runs under

If your model config lives in a **profile** rather than the root
`config.yaml`, a bare `hermes -z` cannot resolve a model and dies with
`Invalid length for parameter modelId, value: 0`. Talk handles this for you:

- `TALK_AGENT_PROFILE=<name>` — spawn `hermes --profile <name> -z …`.
- **Unset (default): auto-detect.** If the root `config.yaml` names a
  `model.default`, no flag is added. If it doesn't and *exactly one* profile
  under `$HERMES_HOME/profiles/` does, that profile is used.
- Zero matching profiles, or two or more → no flag, deliberately. Guessing
  between profiles would be invisible until the wrong agent had already run;
  the spawn's own error names the problem better.
- Set-but-blank (`TALK_AGENT_PROFILE=`) is an explicit opt out: never pass a
  flag, even if detection would have found one.

## Knobs

The common ones. Every variable — api-server probe internals included —
with defaults and failure modes: [docs/OPERATING.md](docs/OPERATING.md#configuration--every-knob).

| Variable | Default | What it does |
|---|---|---|
| `TALK_MODEL` | `gpt-realtime-2.1` | Realtime model; doctor certifies only the bounded duplex-audio + tool-calling policy and labels other Realtime-shaped ids compatibility-unknown |
| `TALK_VOICE` | `cedar` | Realtime voice (fail-closed on unknown ids) |
| `TALK_PROVIDER` | `openai` | Realtime voice provider: `openai`, `grok`, or `gemini` (fail-closed; never inferred from which keys exist) |
| `TALK_GROK_MODEL` | `grok-voice-latest` | Grok realtime model |
| `TALK_GROK_VOICE` | `ara` | Grok voice: `ara`, `rex`, `sal`, `eve`, `leo` (fail-closed) |
| `TALK_XAI_API_KEY` / `XAI_API_KEY` | unset | xAI key for the Grok lane, Talk-scoped first; set-but-blank refuses |
| `TALK_GEMINI_MODEL` | `gemini-3.1-flash-live-preview` | Gemini Live model (bare id; the adapter adds the wire prefix) |
| `TALK_GEMINI_VOICE` | `Puck` | Gemini Live voice: `Puck`, `Charon`, `Kore`, `Fenrir`, `Aoede` (fail-closed, case-sensitive) |
| `TALK_GEMINI_API_KEY` / `GEMINI_API_KEY` | unset | Gemini key for the Gemini lane, Talk-scoped first; set-but-blank refuses; free-tier keys work |
| `TALK_VOICE_MODE` | `native` | `native` (provider voices, unchanged) or `cascade` (provider thinks in text, ElevenLabs speaks); fail-closed |
| `TALK_CASCADE_TTS` | `elevenlabs` | Cascade TTS provider — the only value today; fail-closed |
| `TALK_ELEVENLABS_API_KEY` / `ELEVENLABS_API_KEY` | unset | ElevenLabs key for the cascade lane, Talk-scoped first; set-but-blank refuses; rides the `xi-api-key` header, never the URL |
| `TALK_ELEVENLABS_VOICE_ID` | unset | Voice the cascade speaks with — **required** in cascade mode (stock or cloned, from your ElevenLabs account) |
| `TALK_ELEVENLABS_MODEL` | `eleven_flash_v2_5` | ElevenLabs TTS model for the cascade lane |
| `TALK_PREFER_CODEX_OAUTH` | unset | `true` requires Codex OAuth and refuses key fallback; absent/`false` keeps key-first precedence |
| `TALK_INPUT_DEVICE` / `TALK_OUTPUT_DEVICE` | auto | sounddevice overrides |
| `TALK_AGENT_PROFILE` | auto-detect | Profile for the detached background agent |
| `TALK_API_SERVER_URL` | `http://127.0.0.1:8642` | Where the api-server lane looks |
| `TALK_API_SERVER_KEY` | `API_SERVER_KEY` | Key for the api-server lane (blank = send none) |
| `TALK_AGENT_TIMEOUT_S` | `1800` | Budget for one background run, and its watcher |
| `TALK_IDENTITY_INCLUDE` | all | Which identity sections ride the prompt |
| `TALK_MEMORY_SEARCH_TIMEOUT_S` | `10.0` | Wait bound for the in-process remembered-context (Honcho) lookup |
| `TALK_SESSION_KEY` | unset | Stable operator scope sent as `X-Hermes-Session-Key` on api-server runs, so host-side memory survives `/clear` (blank = send none). **Not a session boundary: every voice-channel participant shares this scope** — memory reads are not gated by the operator ledger, so do not set it in multi-user channels until per-speaker scoping lands |
| `TALK_DASHBOARD_TOKEN` | unset | Token for the dashboard tab's routes (unset = loopback only) |
| `TALK_DISCORD_OPERATOR_USER_IDS` | none | Comma-separated immutable Discord IDs allowed to run mutating tools; malformed = nobody |

### `TALK_IDENTITY_INCLUDE` — what the session starts knowing

Three sections are resolved at session start, each independently and each
optional:

- **`PERSONA`** — your `SOUL.md`, read through Hermes's own loader (so it gets
  the same injection scan the text agent's copy does).
- **`MEMORY`** — the system-prompt block your configured memory provider
  contributes. Inside `/talk` this is the live agent's already-assembled
  block; standalone, Talk loads the configured provider itself, reads the
  block, and shuts it down again.
- **`WORKING`** — `memories/WORKING.md`, the one identity file **you** write
  rather than the model: who you are, which repos and plugins you mean by
  name, what an alias maps to. Entries are separated by `\n§\n`, the same
  delimiter Hermes uses for `MEMORY.md`, and each is threat-scanned
  independently — one bad entry costs that entry, not your whole table. When
  a host is attached, one sentence is appended pointing at `search_memory`
  for names *not* in the file. The rule to ASK when a spoken name could match
  more than one thing rides the voice preamble itself, on every lane — it
  depends on no file, no tool, and no include list, so nothing can drop it.

A broken or missing provider costs that section and nothing else — the call
still starts. `talk_status` reports which sections resolved and how many
characters each contributes, never their content.

`WORKING.md` is what stops a voice session asking who you are every call.
Nothing fills it for you — no producer writes installed plugins or recent
work into it; what you curate by hand is all a session gets. It is read once
at session mint and stays frozen for the call: an edit lands on the NEXT
session, never the live one. Keep it short — the resolved prompt is resident
and paid for on every turn:

```markdown
Pedro, solo operator. Ships at night, prefers blunt answers.
§
"Talk" or "hermes-talk" means TheSmokeDev/hermes-talk (this plugin).
§
"Dograh" (often heard as "Dobra" or "Dog Bras") is the voice stack.
```

Conflicting entries are left alone on purpose. Two lines claiming the same
alias both travel, because resolving that by file order would silently bind
your words to whichever line you happened to write first — the model is told
to ask instead.

Set `TALK_IDENTITY_INCLUDE=MEMORY,PERSONA` to pin the list. **The trap: this
REPLACES the default rather than extending it** — `TALK_IDENTITY_INCLUDE=MEMORY`
means memory *and nothing else*, and the only symptom is a session that has
quietly stopped knowing who it's talking to. Unknown names are dropped
silently, so a typo narrows the prompt instead of taking voice down.
**The upgrade trap is the same trap, aged:** a list pinned before `WORKING`
existed (e.g. `MEMORY,PERSONA`) keeps working verbatim and silently drops
your curated context after upgrading — the session logs one warning at mint
when a pinned list lacks `WORKING`.

Sections are capped (`PERSONA` 4,000 chars, `MEMORY` 6,000, `WORKING` 2,000).
A Realtime session's instructions are resident for the whole call and paid on
every turn, so these are a budget, not a nicety. Caps trim from the tail, and
each section puts what is KNOWN before what can be looked up — so an oversized
file loses its lookup pointer before it loses your facts.

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

## Status

v0.7.0 — under active development. Changes, all seven versions with their
receipts: [CHANGELOG.md](CHANGELOG.md). Roadmap: barge-in latch + spoken-text
normalizer, Gemini Live backend
([#3](https://github.com/TheSmokeDev/hermes-talk/issues/3)), `computer_use`
relay, session-end memory debrief, gateway platform adapter.

Related: [RFC #77111](https://github.com/NousResearch/hermes-agent/issues/77111)
proposes a `RealtimeVoiceProvider` ABC in Hermes core — four open PRs are
building duplex voice independently, and the category deserves an interface
rather than a merge queue. This plugin is a working reference implementation
for that discussion, not a bid to be merged.

## License

MIT
