# Changelog

All notable changes to hermes-talk, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/). Each release opens with what it means before
listing what it contains.

One honesty note, because this file exists to be trusted: the dashboard tab
and the api-server agent lane landed inside the 0.4.0 development window,
but 0.4.0's release title named only the steering verb. They are recorded
below under 0.4.0 — the first version that shipped them — with the gap
named rather than smoothed.

## [Unreleased]

### Added
- A voice session can now start already knowing who you are, which repos you
  mean by name, and what your aliases map to — provided you curate the file
  that carries it (hermes-talk#36). Dogfooding on 2026-08-16 kept hitting the
  same two failures: the session asked who the operator was every call, and
  when a spoken name could mean two things it picked one silently. Nothing on
  a voice surface shows you it guessed.

  Three parts, each an extension of a mechanism that already existed rather
  than a new one:

  `memories/WORKING.md` is a new identity section, and the only one YOU write
  instead of the model — nothing fills it for you, and it is read once at
  session mint and stays frozen for the call (an edit lands on the next
  session). It rides the same durable-file pipeline as `USER.md`
  and `MEMORY.md`, so it is threat-scanned per entry, capped (2,000 chars),
  and filtered by `TALK_IDENTITY_INCLUDE` without any of that being written
  twice — hand-authored is not the same as trusted, and anything that can
  write to your Hermes home can append an entry. Two entries claiming the
  same alias BOTH travel: resolving that by file order would bind your words
  to whichever line you wrote first, with no symptom, so the model is told to
  ask instead — a rule that rides the voice preamble on EVERY lane, gated on
  nothing, because the lanes that lose identity sections (a ctx-less gateway
  or dashboard, a pinned include list, a failed scan) are exactly the ones
  where nobody watches a silent guess go by. When a host is attached, one
  sentence naming `search_memory` is appended for anything not in the file.
  An include list pinned before `WORKING` existed silently drops the section
  after upgrade; the session logs one warning at mint when that happens.

  `search_memory` grew a middle tier. Between the transcript read
  (`session_search`) and the api-server fallback it now tries Hermes's Honcho
  memory plugin, and prefixes that answer with `from remembered context:`.
  The prefix is the point: a remembered profile fact can be stale in a way a
  verbatim transcript line cannot, and collapsing the two would make a guess
  and a quote sound identical out loud. The prefix marks FACTS only — an
  error-shaped Honcho answer is spoken without it — and it is reserved:
  transcript or vault content that leads with the literal marker has it
  stripped, so a quote can never dress itself as a recollection. A Honcho
  that is simply absent falls through; a Honcho that is present and refuses
  is spoken, not routed around; and the Honcho dispatch is bounded by
  `TALK_MEMORY_SEARCH_TIMEOUT_S` (default 10s) so a wedged plugin costs one
  spoken failure, never the serialized tool pipeline.

  `TALK_SESSION_KEY` sends `X-Hermes-Session-Key` on run submission, so the
  memory an api-server run reads and writes is scoped to you and survives the
  `/clear` that ends a `session_id`. Unset — the default — sends no header
  and changes nothing. It is deliberately static and operator-set: a key
  derived from the hostname or the clock would change between runs, and the
  one property the knob exists for would be silently missing. It is an
  OPERATOR scope, not a session boundary: every voice-channel participant
  shares it, because the authority ledger gates mutating tools and never
  memory reads — do not set it in a multi-user channel until per-speaker
  scoping lands.

  Not built, and named here so the gap is not mistaken for coverage: a
  session-mint profile pre-fetch (`honcho_context`), per-Discord-channel and
  per-dashboard-session key derivation, per-speaker memory scoping,
  code-enforced binding for spoken entities, homophone detection, any
  producer that fills `WORKING.md` (installed plugins, recent work), and
  mid-call refresh of identity sections. Ambiguity and mishears are handled
  by prompt copy plus the aliases you write yourself, the same way the
  preamble's damage-based confirmation policy governs every other
  consequential action here — not by a mechanism that can refuse.

### Fixed
- Delegated work and memory lookups are now bound to the exact Talk session
  that asked for them (hermes-talk#35). Previously the `WORK_STARTED` receipt
  was backed by nothing but an in-process dict with a fail-open history tee:
  no run recorded who started it or where the answer should go, and the only
  watcher that would ever speak the result died with the session. A job could
  finish with nobody listening, and nothing could tell a reconnecting session
  "this result is yours" apart from "this one is a stranger's". On this box
  that left three real runs stuck at `running` forever.

  `talk_runs` now mints an immutable ticket at acceptance — operator, profile,
  durable Hermes session, Talk generation, and a per-request id — and persists
  it BEFORE the worker thread starts. That one write is fail-closed: if it
  cannot land, dispatch is refused with `RoutingUnavailable` and the operator
  hears "I can't start that yet" instead of a receipt for work nothing could
  route. Everything else about the tee stays fail-open, because once a run is
  accepted its result is owed. Delivery is a two-phase claim, on disk as well
  as in memory: a result is CLAIMED exactly once at enqueue and flipped to
  delivered only after the announcement is actually handed to the wire, so a
  session torn down mid-queue leaves the result re-adoptable instead of
  consumed-but-unspoken (the residual duplication window is a crash between
  the wire hand-off and the flip — said once more on reconnect, never lost).
  A reconnecting session adopts only tickets recorded under its own Hermes
  session AND its own operator/profile binding — ownership is enforced at
  adoption, not just recorded — while a different session adopts nothing,
  and pre-#35 history, which carries no ticket, is never adopted by anyone.
  Announcements still ride the existing contained-system-item path, so an
  adopted result is exactly as untrusted as a fresh one and can never
  re-enter the conversation as operator speech.
- The run-history file is now serialized ACROSS PROCESSES, not just across
  threads: the CLI lane and the dashboard lane (the Hermes web server
  process) share one `state/talk-runs.jsonl`, so every load-modify-append —
  delivery claims, compaction, and run-id allocation — holds an OS-level
  one-byte file lock (the same msvcrt/fcntl mechanism as the transcript
  writer lease), and run ids are floored on the file's own highest persisted
  id inside that lock at every acceptance, which makes cross-process id
  collisions impossible instead of merely unlikely.
- A disabled history tee now REFUSES dispatch instead of silently accepting
  a run with no durable route; callers that legitimately want in-memory-only
  routing opt in by name (`TALK_RUNS_ALLOW_EPHEMERAL=1`).
- The api-server lane's remote run id is now written through the strict,
  cross-process-locked append at the moment it is learned — retried once and
  escalated to an error log if it still cannot land, never dropped as
  fail-open telemetry. It is the only handle a reconnect could resume
  tracking a tier-2 run by, and holding it in memory alone meant it died
  with the process a reconnect exists to recover from. The terminal tee
  happened to carry it for runs that finished, which is how the gap stayed
  hidden.
- An owed result that falls off the bounded adoption tail of the history
  file is now counted and logged instead of vanishing silently.
- The dashboard's session mint binds the browser lane's own return route, so
  `POST /tool` can still start real work under the fail-closed rule. It carries
  no Hermes session id (none is ever bound in the web server process) and never
  the ephemeral credential — a secret does not become an identifier.

### Added
- A live capability catalog: the new `talk_capabilities` tool answers "what can
  you do right now?" from evidence instead of from the system prompt — installed
  skills, resolved toolsets with their `enabled`/`configured` flags, the
  gateway's feature flags, and bounded run/delegation counts. `talk_capabilities.py`
  reads it in-process off the committed host attachment when a Hermes agent is
  attached, and falls back to the api server (`/v1/skills`, `/v1/toolsets`,
  `/v1/capabilities`, `/health/detailed`) when it is not — the same two-tier
  doctrine `agent_lane()` already uses. A host that does not expose the
  in-process tool degrades to REST rather than failing. The snapshot is
  TTL-cached (`TALK_CAPABILITY_CATALOG_TTL_S`, default 30s) and warmed at the
  dashboard's session mint, so a tool handler never waits on the network.
  Disabled toolsets are reported rather than hidden, so the model can say
  "installed but not usable" instead of quietly offering something that would
  fail. The tool is classified read-only: reading the catalog grants no
  execution authority, and a catalog read consumes its call permit so it cannot
  be replayed as a mutating call.
- A typed provider-neutral Realtime session boundary: `talk_realtime.py` owns
  setup, events, commands, lifecycle states, and the adapter protocol, while
  `talk_openai_realtime.py` owns OpenAI ephemeral minting, WebSocket lifecycle,
  and wire translation. Hermes policy now runs against the neutral contract, and
  failed sessions stop active tool coordination through an acknowledged,
  bounded teardown. The CLI still resolves OpenAI auth and constructs the sole
  bundled OpenAI adapter; this does not add arbitrary provider selection.
- Native `hermes talk setup`: detect current state, ask only unresolved
  auth/model/voice decisions, explicitly confirm each setting, securely commit
  the confirmed set as one rollback-capable atomic transaction, emit a redacted
  apply receipt, then rerun the separately read-only doctor and verify the
  result. Key selection under preferred Codex OAuth reuses an existing key and
  separately confirms the required policy transition; key selection after an
  invalid preference now resolves the scoped key in that same transaction and
  completes setup in one run.
- Native `hermes talk doctor` human and `--json` diagnostics for registration,
  auth selection, model/voice, audio, identity profile/root/count receipts,
  Discord operators, and host capabilities. The command is strictly read-only
  and redacts credentials, identity content, operator IDs, and secret-shaped
  values pasted into malformed configuration fields.
- `TALK_PREFER_CODEX_OAUTH=true` as an explicit fail-closed subscription lane.
  Without it, the existing scoped-key → shared-key → Codex order is unchanged;
  doctor warns when a metered key wins and distinguishes valid OAuth from an
  expired credential that still requires refresh.
- Cross-platform dotenv mutation: Windows names match case-insensitively and
  duplicate case variants collapse deterministically; POSIX names stay
  case-sensitive. New secret files use POSIX owner-only modes or a native
  protected owner-only Windows DACL while existing Windows destination DACLs
  are preserved. Every staged path is cleanup-verified; a surviving temp makes
  the redacted receipt fail instead of claiming rollback. Hermes-home
  provenance follows the host's exact tilde, relative, and platform-default
  path semantics or reports unknown.
- Bounded model compatibility policy for Talk's duplex-audio and live-tool
  requirements. Specialized Whisper/Translate models fail explicitly; unknown
  Realtime-shaped ids are labeled syntax-only instead of certified valid.

## [0.8.0] — 2026-08-04

The session stops arriving as a stranger. It now knows who it is talking
to, what it already knows, what day it is, and how to look something up
in your written notes — and it stopped telling the model to call tools
it does not have.

### Added
- **`USER` and `MEMORY` actually ride the session.** Both were declared
  with headers, caps and an ordering, and had no producer anywhere. The
  cause was two similarly named host surfaces: `MEMORY.md`/`USER.md`
  live on the agent's memory STORE, and this plugin read the memory
  MANAGER, which holds external providers only. They are now read from
  `<hermes_home>/memories/` directly, so it works on all three lanes —
  the gateway and the dashboard have no agent at all.
- **`search_vault`** — look something up in the operator's long-term
  written notes, as distinct from `search_memory`'s what-was-said.
  Backed by the memory provider's own index read in process, and
  advertised **only** when a lookup can really be served.
- The current date and time, built per session (a module-level clock
  would freeze at import and a long-running gateway would state the day
  it booted).

### Changed
- **The memory pointer stopped lying.** The provider's own
  `system_prompt_block` used to pass straight through, telling the model
  to call `homie_memory_search` — a real tool in a text agent's registry
  and absent from a Realtime session's, so the model called it and got
  "That tool isn't available" on a live call. Every provider's block has
  that shape, so this was wrong as a class. One sentence this plugin
  authors replaces it, naming a tool the session has.
- The vault provider is resolved once behind a single-flight lock (a
  rebuild is a full vault walk, ~0.3s measured, on the loop carrying the
  microphone), and BORROWED from a live agent when one already has it
  initialized — never shut down in that case, because it is that
  agent's.

### Fixed
- A case-variant known section name (`"memory"`) matched neither the
  ordered list nor the extras, so it vanished from the prompt entirely
  rather than rendering out of order.

### Known gaps
- Nothing is written back when a call ends (#9), and every speaker in a
  Discord channel is still treated as the operator (#10).

## [0.7.0] — 2026-08-04

Talk to it in the Discord voice channel Hermes is already sitting in.
`/talk join` turns that channel into a live duplex call — it hears you
while it speaks, you can cut it off mid-sentence, and you can delegate
and steer background agents out loud. Verified on a real call: the
session connected on `gpt-realtime-2.1` over a ChatGPT subscription,
heard the operator, answered, and spawned a background agent by voice.

### Added
- **Discord voice** (`talk_discord.py`). The plugin's audio device is
  seven methods wide, so a voice channel can wear the same shape a
  microphone does — the session, tool calls, steering ledger and
  announcements above it are unchanged.
- `/talk join` / `leave` / `status` inside the gateway. Outside it,
  `/talk` still means the terminal call.

### Changed
- **No second Discord connection.** The host already holds one and
  already decrypts DAVE; the plugin borrows it, so it is one bot, one
  connection, the host's own E2EE. For the duration of a call it takes
  over three host surfaces and returns them on stop — with one documented
  exception: the ambient mixer is dropped rather than handed back,
  because taking over playback closes it permanently and a closed mixer
  still reports itself speaking, which would stall every later host reply.
- Rate conversion is integer 2:1 both ways over `array` — no resampler
  dependency, and specifically no `audioop`, which left the stdlib in 3.13.

### Fixed
Four defects that only a real call could surface, each now pinned by a
regression whose fake models the host's actual behaviour:
- the receive tap was never registered (discord.py stores the bound
  method object and calls what it stored, so rebinding the attribute
  tapped nothing — the call heard silence with nothing logged);
- the playback source was duck-typed where `VoiceClient.play`
  isinstance-checks;
- the adapter lookup imported `Platform` from a module this host does not
  have, so it refused on a healthy gateway;
- capture forwarded only what Discord sent, so during a pause the
  server's turn detection never saw the silence that ends a turn.

## [0.6.1] — 2026-08-03

The polish release: the stop verbs can no longer dead-air the call, and
their receipts survive you hanging up. Closes #2 and #5. One adversarial
review round found three gaps (receipt durability, a reaped-handle race,
announcement interleaving) — all reconciled in-release. 390 tests.

### Changed
- `stop_work` runs its confirmation on daemon workers with a bounded 1.5s
  courtesy wait: the common fast path still speaks the real result, a slow
  server gets honest detached wording, and the voice loop never freezes
  (the old synchronous path could block it ~6s).
- `terminate()` is now confirmed, not just signaled: the exit code is read
  from a handle captured before the signal (immune to the run worker
  reaping the child first), and the run record is consulted before any
  uncertainty claim is spoken.
- All out-of-band announcements (finished children, landed notes) flow
  through one serialized pump — whole batches, deferred while a response
  is in flight, so concurrent events can never interleave or stack active
  responses.

### Added
- Landed steering notes are now **pushed**: the moment a note's delivery
  artifact fires, the live call hears "the note just landed" instead of
  waiting for the next `check_work`.
- Stop receipts persist to the run history (`annotate_run(tee=True)`), so
  a receipt promised past the courtesy wait survives a process restart and
  the next session's `check_work` can still keep the promise.
- `uninstall_watchers()` — a production unhook symmetric with the two
  `ensure_*` calls; the borrowed logger level is reconciled on every
  ensure (operator verbose-logging toggles are honored both directions).

## [0.6.0] — 2026-08-03

The release where delivery confirmation stopped having a blind spot, and
the plugin gained a stronger verb than a queued note. Gate chain: 368
tests + ruff → two Codex adversarial rounds (six findings, all
reconciled) → Kimi K3 design gate PASS — "every mechanism degrades toward
less information, never toward a wrong claim."

### Added
- **Second delivery artifact**: a watcher on the host's pre-API steer
  drain. Notes delivered right before a model request used to terminate
  as false "unconfirmed"; they now land. Attribution is by frame identity
  against the agent captured at steer time — exact, never heuristic.
- **Push lifecycle**: `subagent_start`/`subagent_stop` hooks roster
  children by session id; completions are announced into the live call
  the moment the host reports them (injection-contained: system-role
  item, `tool_choice: "none"`, self-deleting in the same batch).
- **`redirect_agent`** — interrupt a child's current step and re-aim it
  now ("stop, wrong repo"), on the 0.20-public `AIAgent.redirect()`. The
  receipt comes from the return value; the wording never claims more than
  the host guarantees. Degrades to the steer queue on pre-0.20 hosts.
- **Correlation tokens** (closes #1): every note travels as
  `[tk-xxxxxxxx] note` and delivery matching is token-first — two agents
  holding identical text can never land each other's receipts.

### Fixed
- The v0.5 wheel silently omitted `talk_steer` from `py-modules` — the
  exact trap the pyproject comment warns about, caught in the wild.
- The dashboard manifest version had been stuck at 0.3.0.

## [0.5.0] — 2026-08-03

`steer_run` was retired for telling comfortable lies; this is the surface
that replaced it. Three reviews, one verdict: it claimed delivery the
substrate cannot know. v2 speaks only what an artifact proves.

### Added
- Run-control surface: `list_agents` (discovery-first ids), `steer_agent`
  (queued is the only call-time claim), `stop_work` (the one verb every
  lane supports — and every "want me to stop it?" offer is real).
- The receipt ledger (`talk_steer`): queued → landed / unconfirmed /
  missed / superseded, each state upgraded only by a named artifact
  (the host's own drain log line, watched in-process).

### Removed
- `steer_run` — replaced by the surface above.

## [0.4.0] — 2026-08-02

The under-named release: its title was the steering verb, but the same
window shipped the browser dashboard and the api-server agent lane.

### Added
- `steer_run` — redirect a live background agent by voice (superseded in
  0.5.0 by the honest surface).
- **Dashboard tab**: the browser voice page — WebRTC audio, tool relay,
  run watcher, voice picker, and its own token gate
  (`TALK_DASHBOARD_TOKEN`; loopback-only when unset). Four backend routes
  under `/api/plugins/hermes-talk/`, every one auth-gated by construction.
- **Three-tier agent chain**: attached in-process loop → a real agent
  over the api_server platform → detached `hermes -z` one-shot; every
  fall-through announced, the active lane reported by `talk_status`.

## [0.3.0] — 2026-08-02

Sessions that start already knowing you: the host's identity files ride
the session instructions, budgeted and trimmed, so the first sentence out
of the model isn't from a stranger.

### Added
- Voice identity assembly with per-section budgets and the
  `TALK_IDENTITY_INCLUDE` knob (REPLACES the default set — the trap is
  documented where the knob is).
- The autoplaying dashboard demo in the README, recorded from a real
  session (hosted on this release's page).

## [0.2.0] — 2026-08-02

Background delegation that speaks its results: hand work off mid-sentence,
keep talking, and hear the result the moment it lands — even if you went
quiet.

### Added
- `delegate_task` / `check_work` and the async-run registry with a
  durable, honest history tail (a run from a dead process reports `lost`,
  never "still running").
- Dual-lane credentials: an OpenAI API key or a ChatGPT subscription via
  Codex OAuth — resolved fail-closed, and only the ephemeral session
  secret ever touches the socket.
- The offline test suite (82 tests then) and CI across
  {ubuntu, windows} × {3.11, 3.12, 3.13} — no secrets, no network, no
  audio device.

### Fixed
- GA Realtime protocol compliance (session.type on every update; the
  retired beta header dropped) and honest process exit codes.

## [0.1.0] — 2026-08-02

Repo foundation: the plugin manifest, call-time config resolution, and a
pure Realtime wire layer that knows the OpenAI protocol and nothing about
the host.

[0.8.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.6.1...v0.7.0
[0.6.1]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/TheSmokeDev/hermes-talk/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TheSmokeDev/hermes-talk/releases/tag/v0.1.0
