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
