# Voice commands — say this, hear this

There are no magic words. The model hears normal speech and picks the
tool; the phrasings below are just reliable triggers. What IS a contract
is the receipt that comes back: every reply commits to exactly what the
substrate can prove, never more. `WORK_STARTED #7` is a commitment to
watch run 7 and speak its result; "queued" is a queue write, not a
delivery; "landed" only ever follows a real delivery artifact.

## The card

| Say something like | Tool fired | You hear | What it commits to |
|---|---|---|---|
| "what do you remember about the auth refactor?" | `search_memory` | matching excerpts from past Hermes sessions, summarized aloud | a real search ran; "nothing found" means the search was empty, not skipped |
| "what do my notes say about the offer ladder?" | `search_vault` | matching excerpts from your long-term written notes | what was WRITTEN DOWN, as opposed to `search_memory`'s what was SAID. Only offered when a memory provider can actually serve it; "nothing in the notes" and "the lookup failed" are deliberately different sentences |
| "delegate a task: audit the login module and list every route it touches" | `delegate_task` | `WORK_STARTED #N`, or "I can't start that yet — <reason>" if no Talk connection is bound to route the result | a watcher polls that run and SPEAKS the result when it finishes, even if you've gone quiet; the refusal means nothing was accepted, not that it started and broke |
| "…and it's working in the ship checkout" (naming what the job touches) | `delegate_task` with `resource_keys` / `execution_mode` | either the same `WORK_STARTED #N`, or a refusal naming the run in the way: "run 4 (audit the repo) is still running and touches the same resource ('/srv/app'); wait for it, stop it, or re-delegate without that key" | the fence, not a queue: the clash is decided BEFORE a run id is minted, so a refused job burns nothing and never surfaces later as `lost`. Two live runs sharing a key never overlap unless both declared read-only AND you set `TALK_TRUST_DECLARED_READ_ONLY` — the model's own claim about work it hasn't done is policy input, never a sandbox. Naming nothing means no fence, exactly as before |
| "how's the work going?" / "check run 7" | `check_work` | per-run status lines, plus the state of every steering note you've sent | the note states come from the receipt ledger — never "they got it" without the artifact |
| "what's running right now?" | `list_agents` | live subagent ids tagged **can steer**, run numbers tagged **stop only** | ids that exist RIGHT NOW — resolve "the research one" here, never from memory of earlier speech |
| "tell that audit to focus on the token refresh instead" | `steer_agent` | "queued for their next step — I'll confirm when it lands" | queueing only; "landed" arrives later, pushed, when a delivery artifact fires |
| "stop — wrong repo, use the ship branch" | `redirect_agent` | "redirect accepted — it takes the correction at its current step, or its very next one" | the stronger verb: interrupts current thinking where the host supports it (0.20+), degrades to the steer queue mid-tool or on older hosts — and says which |
| "kill the audit" / "stop run 7" | `stop_work` | "sent the stop — winding down" then a death receipt ("it's down", exit code) when confirmed | stopping drops unread steering notes (their receipts flip to `superseded`); every stop offered is real on that lane |
| "once" / "this session" / "no" (answering an approval question) | `resolve_approval` | "approved — just this once", "approved for the rest of the run", or "denied — the agent was told no" | voice can grant `once`, `session`, or `deny` — **never `always`** (narrowed in code, not in the prompt); an unanswered question denies itself on a timer, and interrupting the question denies it on the spot |
| "what are you running on?" / "status report" | `talk_status` | version, model, voice, auth lane, agent lane, audio, identity sections | the verification command — field-by-field meaning in [OPERATING.md](OPERATING.md#4-talk_status--the-in-session-command) |
| "what can you do right now?" / "which tools do you have?" | `talk_capabilities` | installed skills, resolved toolsets with their enabled/configured flags, gateway feature flags, live run counts | live evidence, not the prompt — read in-process off the attached agent, or over the api server when detached; a toolset listed `enabled: false` is reported as installed but NOT usable |
| "stop listening" / "mute the mic" / "hold on, I'm talking to someone" | `pause_voice_input` | "microphone paused — press Enter when you want me back" (standalone `hermes talk` in a real terminal) or "… say `/talk resume`" (Discord) | the call stays up: playback, background runs and their announcements continue; nothing you say reaches the provider until YOU resume it — a paused mic cannot hear "resume", so the way back is a key or a command, never speech, and the tool is offered only where that key or command exists (not for `/talk` at the Hermes prompt, not with a non-tty stdin). Resume gets its own spoken receipt |

Things you'll hear without asking (v0.6+): a background agent finishing
("Background agent sa-… finished…"), a steering note landing ("the
note just landed"), and (v0.8+) bounded progress milestones ("Background
run #7 was accepted", "…is executing — Reading files", "…is waiting on
an approval", "…is still working"). Milestones fire on phase changes only
— not every poll tick — and the tool label is the only job detail that
can surface (never arguments, paths, or output). All arrive the moment
the host reports them. And (v0.15+) when delegated work parks on a gated
action, the approval question itself: "Background run #7 is waiting for
approval — once, this session, or no?" While that question is open the
generic "waiting on an approval" milestone stays silent — the question is
the actionable sentence.

## Steer vs redirect vs stop — three sentences

- **Steer** queues a note the agent reads at its next natural step; its
  current step always finishes.
- **Redirect** interrupts the current step and re-aims it now — the
  correction that can't wait.
- **Stop** cancels the work; anything queued and unread dies with it,
  and the receipt says so.

The full receipt-state vocabulary (`queued` / `landed` / `redirected` /
`unconfirmed` / `missed` / `superseded`) and the artifacts behind each
state live in the README's
[redirecting-work section](../README.md#redirecting-work-thats-already-running)
— that prose is canonical; this card doesn't repeat it.

## Pausing the mic — and the way back

"Stop listening", "mute the mic", "hold on, I'm talking to someone" is a
tool call, not a hang-up. `pause_voice_input` stops your speech reaching the
provider and changes nothing else: the session stays connected, the speaker
keeps playing, background runs keep running, and their announcements still
land. It is classified read-only — a pause can only narrow what a session
does — so in a Discord channel any participant may call it, not just the
operator allowlist.

**A paused microphone cannot hear the word "resume."** That single fact
shapes everything else here: the way back is always a key or a typed
command, never speech.

| Where | Pause | Resume |
|---|---|---|
| standalone `hermes talk` in a real terminal | say it (the tool), or `p` | **Enter** toggles; `r` is the explicit one |
| Discord, inside the gateway | say it, or type `/talk pause` (`mute`) | type `/talk resume` (`unmute`) — `/talk status` says while it is paused |

On Windows those are single keypresses, read one character at a time; an
extended key (an arrow, Insert, an F-key) is swallowed whole, because read
on its own Down-Arrow's scan code is `P` and would pause the microphone.
Everywhere else the terminal stays in its own cooked mode — no tty state is
ever changed, so a crash can never leave your shell raw — and the key is a
line: type `p`, `pause`, `mute`, `r`, `resume`, `unmute`, or nothing at all,
then Enter.

The model can also resume itself (`paused=false`) when something other than
your voice asks it to. Either direction gets a spoken receipt, and the
pause receipt always names the control THIS session registered — so what you
hear is the way back that actually exists in the room you are in, never a
key from the other one.

**Where the tool is not offered at all.** The decision is made once, before
the tool list is built, from the same predicate that starts the keyboard
watcher — so the model is never handed a pause you have no way to undo:

- **`/talk` typed at the Hermes prompt.** That prompt owns the terminal
  (prompt_toolkit, raw mode, its own stdin reader); a second reader would
  race it for every byte. That lane never watches stdin, so there is no key
  — use the standalone `hermes talk` command if you want the pause.
- **A non-tty stdin** — piped, redirected, or a terminal that does not
  report itself as one. Note that Git Bash's mintty reports `isatty()` as
  false to Python, so `hermes talk` under mintty gets no pause key and no
  tool; a native console (Windows Terminal, PowerShell, cmd) does.
- **The dashboard tab**, whose microphone lives in the browser — use the
  page's own mute control. Asked anyway, the model is told there is no
  microphone here to pause and says so.

A pause call that reaches the handler some other way — a relayed name, a
stale schema — is refused rather than armed (`no_resume_path`), because a
pause nobody can undo would end the call in all but name.
