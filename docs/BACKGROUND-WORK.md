# Background work — delegation, steering, and the capability bridge

What a Talk session can hand off while the conversation keeps going, how the
run is admitted, how you correct it mid-flight, and what the voice is allowed
to do by itself. The [README](../README.md) is the front door; this page is
the whole surface.

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

Four properties make that possible:

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

Hermes's built-in voice mode is good and this doesn't replace it — turn-based
STT → inference → TTS is the right shape for plenty of work. This is the other
shape.

## Handing work off

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

Delegation walks the [three lanes](OPERATING.md#reaching-a-real-agent--the-three-lanes) and
then one more, and **every fall-through is said out loud** — the plugin never
silently does less than you asked:

1. **Hermes's own agent loop** — inside `/talk`, where there's a parent agent
   to delegate into.
2. **A real agent over the api_server** — preferred over a spawn: it reuses a
   warm, fully-tooled agent instead of paying a process start.
3. **A detached `hermes -z` one-shot** — needs nothing enabled, so this is the
   lane that always exists as long as `hermes` is on the PATH.
4. None available — a refusal naming all three missing lanes.

## Two jobs, one checkout — admission control

Delegate two tasks that both touch the same repository and, left alone, they
race: two agents editing one checkout, two deploys to one target. Since #101
the model can say what a task touches, and the run registry refuses the second
job instead of letting it collide:

- `delegate_task` takes two optional arguments. `resource_keys` names up to
  eight stable things the task touches — an absolute repo path, a deployment
  target, a service name (whitespace-collapsed and case-folded, so two
  spellings of one path are one key). `execution_mode` is `exclusive` (the
  default) or `parallel_read_only`.
- Two live runs that share any key never overlap unless **both** are
  `parallel_read_only`. The check happens before a run id is minted, before
  the acceptance record is written, before the worker starts — a refused job
  burns nothing and leaves no `lost` record behind.
- A refusal is a spoken tool result naming the run in the way: "run 4 (audit
  the repo) is still running and touches the same resource ('/srv/app'); wait
  for it, stop it, or re-delegate without that key." Never a hang, never a
  silent queue. `check_work` reads out what each running job is holding.
- **`parallel_read_only` is believed only when you say so.** The declaration
  is the delegating model's own claim about work it has not done yet — policy
  input, not a sandbox — so by default it is downgraded to `exclusive` and
  recorded that way. `TALK_TRUST_DECLARED_READ_ONLY=true` lets read-only jobs
  on a shared key run together; the knob is read at admission time, so turning
  it back off closes every overlap it had allowed. It is the only thing that
  can widen behavior.
- No keys means no fence, in either direction: a task that names nothing is
  exactly the task Talk always ran, record and all.

The fence is per process and covers the api-server and detached lanes, whose
runs this registry owns. Inside `/talk`, the host's own delegation registry
runs the child: the job is still checked against the keys this registry
holds — never started on top of one — but it holds none itself afterwards,
and its `WORK_STARTED` receipt says so.

## Redirecting work that's already running

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

## What the voice can do — the capability bridge

The session prompt carries a bounded, live-catalog section: how many skills
are installed, which tool categories are usable *right now*, and the two rules
that keep the model honest — it can **delegate anything Hermes can do**, and it
must **never invent tool names**. The section is assembled from the real
catalog lanes (the host's own registries in-process, or the api-server's
`/v1/skills` + `/v1/toolsets` + `/v1/capabilities` out of process); when the
catalog is unreachable the section is simply absent, and the session runs on
the plain preamble exactly as before.

That changes three everyday exchanges:

- **"What can you do?"** is answered from the catalog — live evidence, never
  a recited prompt. A toolset whose tools all failed the host's availability
  gates is not claimed.
- **"Check my screen" / anything past the advertised tools** delegates by
  default. The classification table in `talk_operator_auth` decides what a
  host tool call may do at the voice surface: a short curated read-only list
  (`web_search`, `web_extract`, `vision_analyze`, `session_search`) runs
  inline; `computer_use`'s read actions (`capture`, `wait`, `list_apps`,
  `list_windows`) need a fresh spoken operator permit; everything else — and
  every destructive computer-use action, whose in-handler gate fails open
  without a real approval context — delegates. The classification applies on
  every transport, the local terminal lane included: mutating host tools
  steer to delegation with spoken approvals rather than running bare. A
  denied call never refuses flat: you hear "I can't do that directly in a
  voice call — I can spin up an agent that can. Want me to?"
- **Delegated work that hits a gated action now asks you out loud.** The run
  lane streams the host's `approval.request` events; Talk speaks the request
  ("run 3 wants to run a shell command — once, this session, or no?") and your
  spoken answer resolves it via the new `resolve_approval` tool, which on
  Discord rides the same fresh-speech permit machinery as the other mutating
  tools. **Voice can grant `once`, `session`, or `deny` — never `always`**:
  the choice set is narrowed in code, in the tool schema, and in the prompt,
  and `session` is scoped to that run. Fail closed on everything ambiguous: an
  unanswered question times out into a deny (`TALK_APPROVAL_PROMPT_TIMEOUT_S`,
  default 60s), and interrupting the question denies it too — a question not
  fully heard is not a question answered. Progress and the result still arrive
  through the existing milestone/result machinery.

## `TALK_AGENT_PROFILE` — which profile the background agent runs under

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
