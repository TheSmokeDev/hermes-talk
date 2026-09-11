# Dashboard task continuity

The Talk dashboard can join an existing canonical Hermes task, continue it with
voice or typed input, and inspect the same history and linked work after reconnect.
Legacy unbound Talk remains an explicit option; a failed bound join never falls
back to a different task or a detached execution process.

Choose an existing task or canonical Bot Chat from the authorized target catalog,
then start Talk. The page shows the selected task, verified host context, canonical
history, staged interactions, action receipts and task jobs. The typed input field
uses the same interaction path as speech. Stop ends the voice connection; accepted
child jobs keep their original owner and continue. Rejoin that original task to
recover pending receipts and inspect current results. Completed job replay does
not automatically speak or submit an approval.

## Required host support

Bound mode requires the server-only
`hermes_cli.dashboard_task_context.resolve_dashboard_task_context` helper, passive
history v1 with authenticated `store_id`, and linked-child dispatch v1 with durable
run idempotency. The helper establishes the verified actor, canonical profile home
and catalog store. The configured gateway must report the same opaque store ID
before attachment, persistence or work. Matching profile/session strings alone
are insufficient. An unverified remote URL remains unsupported; remote continuity
uses only an explicitly registered peer as described below. The configured key
must authorize the selected profile.

Outbound calls use only the existing configured gateway URL/key. Browser OAuth
credentials, body actor fields and Host headers do not establish outbound authority.
Local derived ownership includes both the verified dashboard actor and immutable
gateway host/auth/profile/task. Host tab IDs are actor-scoped. Legacy dashboard-token
mode is explicitly a shared operator role, not an individual identity. Profile homes
and credentials remain server-only, including in error responses.
Derived ownership includes the verified canonical store generation, preventing old
pending input from being silently replayed into a replaced database.

The task's bounded canonical snapshot supplies manager context. Unavailable workspace
metadata is labelled unavailable; the server does not substitute its own working
directory. An explicitly selected page URL/title is a labelled reference, with query
and fragment removed; the page is not fetched or treated as instructions. Bound mint
does not load the web process's potentially unrelated profile memory/identity.

## Input, dialogue and work

Provider VAD still detects speech, but bound mint sets automatic response creation
off. The browser stages a finalized original input before explicitly requesting its
response. Provider item/response/call IDs and response metadata link each response
and tool continuation to that input. Ambiguous linkage is refused; generated task
arguments and arrival timing never stand in for the original utterance.

Verified linked-child v1 shares the passive singleton-user origin contract. This
specific bound path therefore saves genuine finalized user input immediately after
durable staging. Child admission reuses the matching receipt, or races the same
event/origin/exact user bytes safely. Later child actions use a proven receipt ID
when available. User input survives response interruption or terminal work refusal.
This exception does not change P2a's normal guard or legacy parent-model origin rules.

Generated child goals and bounded context are separate from original user input.
Each provider call gets an immutable action ID, exact durable request and idempotency
key before dispatch. A lost response retries that original request/key. It never
authorizes a replacement job. An old connection cannot update a newer presentation,
but a late receipt can still be recorded against its existing original action.

Final assistant outputs use their own event IDs, never an extension of the user event
to a pair. They save only after the explicit response/tool continuation graph settles.
Partial outputs and synthetic background-result inputs are excluded. Canonical order,
source observations and action/receipt links remain distinct; pending text is visibly
pending rather than presented as saved history. Prior completed provider interaction
groups are bounded and retained for current-call continuity.

Bound tools include target listing/switch intents/Return, linked delegation/search,
current work/status checks, owning-run
stop, origin-linked steering and current approval resolution. Resource-admission
declarations remain unavailable in this bound slice; legacy unbound behavior is unchanged.
The voice manager remains available while child work runs. State refresh rotates
through at most four jobs per request; all known job references remain visible with
last-observation labels when not refreshed. Result links retrieve the full currently
available host result, bounded by the 2 MiB transport response cap, and render inert text.

Fresh approval views use authenticated `GET /v1/runs/{id}/approval`, not cached run
metadata or another process's local approval registry. Views create no permission.
Voice choices remain once/session/deny; the existing owning-run POST rechecks the
exact request and stopping/terminal state before resolving. Missing read support,
resolved/revoked requests and ambiguous multiple approvals are explicit refusals.

## Local state and limits

The shared profile-derived SQLite store contains bounded interaction staging and
immutable action requests/references. Limits are 128 interactions, 512 actions,
4 MiB total staged JSON, 24-hour staging TTL, 16 responses per interaction and eight
final outputs per response. It is a recovery buffer, not a second canonical archive.
The shared task-event projection retains its separate bounded observation and speech
records. Canonical owner deletion cascades derived references; late responses cannot
recreate deleted state. Reconnect always returns to the original immutable owner.

This integration adds no provider, audio recording, remote host discovery or automatic
approval service. Testing with fixture audio/provider events does not establish a live
microphone/provider deployment; installation and live verification remain separate.

## Switching tasks and Bots

The picker uses authenticated gateway session lists and the exact existing `Bot Chat`
title lookup. It never creates a missing Bot Chat or guesses from a recent session.
Local profiles come from Hermes' profile catalog and are validated by the dashboard
context helper. Named profiles use their own scoped gateway credentials.

Choose a target and press **Switch target**, or ask for an exact target name. The model
can list authorized targets and request a switch; ambiguity requires an explicit
choice. A tool intent takes effect only after its original response/tool batch ends.
The server prepares the target's snapshot and new voice descriptor before activation.
A failed different-target preparation keeps the current selection. Rejoining the same
target explicitly starts a new voice generation; it does not add a Return entry.

**Return to previous** reauthorizes the saved immutable target. Each task keeps its
canonical history, pending jobs and approvals. Old callbacks cannot update the selected
task, while an already authorized job's late receipt remains associated with its original
action. Returning to a local task does not require the departed peer to be online.
Cancelling an in-flight switch only cancels the browser's pending operation; an already
accepted activation is visible through **Refresh targets / selection** and can be rejoined.

## Explicit peer setup

Use Hermes' existing `hermes peer` CLI to register a named peer and its server credential
(see `hermes peer --help`). Talk reads the existing `bot_peers` registry; it does not write
or discover peers. For example, a registered name `research` has a configured origin URL
and uses the host-managed `HERMES_PEER_RESEARCH_KEY` secret. No gateway keys, profile
filesystem paths or arbitrary endpoint fields are accepted from the browser/model.
This integration accepts HTTP(S) origins without URL paths, queries or user information.

Select the peer by its registered name, then explicitly specify the remote profile.
`default` is an explicit profile route too. Remote profile discovery is unavailable.
Catalog, snapshot, persistence, jobs and controls all use that same authenticated peer
and profile. The authenticated catalog's opaque store ID is pinned and checked again
before activation and work. Changed credentials, route or store require a fresh catalog
selection; pending ownership is never migrated automatically. Operator identity remains
the verified dashboard actor, scoped together with that gateway credential and target.

Offline peers, denied credentials, missing tasks and unsupported hosts are refusals,
with no local substitute. History truncation stays visible, and completed job results
do not automatically speak. Steering requires the same peer's advertised run-control
contract and an authoritative live target. Actual remote/provider acceptance is a separate live
verification step, not established by local fixture tests.

Selection state stores only opaque target references, safe labels and a Return stack
of at most eight entries. The actor/tab selector expires after seven days; catalog
entries expire after ten minutes. Global bounds are 64 selectors and 1,024 catalog
entries. Pending preparation leases expire after 90 seconds and use revision/nonce
checks; stale preparations cannot replace a newer selection. Remote derived buffers
live under the authenticated local profile's state directory, not a remote-provided path.

## Correcting an existing job

Ask to correct a known task job or provide its exact API run ID. `steer_work` uses
the original linked user input, its canonical origin and the existing owning run.
The generated tool arguments select only the job; they cannot replace your words.
Talk first verifies the owning session, current target and host's version-1
`features.run_steering` capability, then freezes the complete control request.
The same authenticated `/v1/runs/{id}/steer` route handles local and configured-peer
controls. No run-list discovery, replacement job or implicit stop is involved.

Recorded origins need `passive_receipt` support for that runtime. An ordinary run
may also advertise `pending`: its host can bind the already staged event and origin
atomically while the execution lease holds up passive persistence. The returned
canonical receipt is reconciled into the existing input; no second parent request
is created. Older hosts and linked children without an original receipt remain
explicitly unavailable or pending. Origins are never dropped to bypass a refusal.

The action panel distinguishes host receipts from client observations. **Queued**
means backend queue admission; it does not prove delivery or application. Rejected
and unsupported controls say why. **Unconfirmed** means acceptance is unknown.
After a lost response, reconnect and state refresh only read the original action.
An explicit retry may send the identical frozen body and action ID only after the
host returns the exact receipt-not-found result. A durable unknown receipt never
causes another submission. Late responses stay with their original action and cannot
update a switched connection. State replay does not speak, submit tools or grant approvals.

Stopping playback or muting affects audio. Interrupting the current voice response
uses the existing provider transport. `stop_work` cancels the owning job. Steering
does none of these and never silently falls back to cancellation or restart.


## Full results and spoken updates

Each task saves an update frequency: **Completion and important updates** (default),
**Completion only**, or **Include meaningful milestones**. Change it in the task panel
or ask the bound voice manager to change it. The setting belongs to the verified
host/profile/actor/task, survives reconnect and event-cache expiry, and is available to
other adapters using the same `HistoryOwner`. It does not enable an unimplemented surface.
Voice setting changes and their retry receipts commit atomically. Concurrent setting
changes follow SQLite transaction order; retrying an older action returns its recorded
outcome without applying the old setting again. The profile supports 256 saved task
settings and refuses new entries at capacity. Canonical task deletion removes its setting.

A live status change can request a short spoken takeaway while the full available result
stays in the task panel. Initial/recovered observations remain silent. Optional milestone
speech follows the saved setting; failure and terminal results remain eligible in every
mode. Completion-only suppresses optional pending-approval speech, but the current approval
view remains visible and host-owned. Heartbeats with unchanged phase/status do not create
new speech opportunities. Each speech preparation rereads the original owning run and,
when relevant, its current approval state. A foreign or revoked target is refused.

The full-result endpoint preserves all available text, including embedded artifact
references, or serializes a multipart result as complete inert JSON. Results remain subject
to the existing 2 MiB gateway response cap; this does not add an arbitrary file-download
proxy. Empty and failed results retain their real status. The speech input is a separately
labelled excerpt capped at 12,000 characters; it never replaces the original result.

Summary responses use Realtime `conversation: none`, explicit input, empty tools and
`tool_choice: none`. Their metadata and response IDs are separate from ordinary dialogue.
They cannot stage user input, satisfy a conversation-response barrier, call a tool, or
launch follow-on work. The server requests at most 220 output tokens and one or two
sentences; speech quality and model compliance still need live acceptance. A transport
handoff is recorded as sent, never as proof the operator heard it. Durable attempts prevent
duplicate and reconnect speech. Comprehensive speech/playback gating, stale-event recovery
and interruption timing belong to the following timing slice.

Protocol: [OpenAI Realtime client events](https://platform.openai.com/docs/api-reference/realtime-client-events/conversation/item/create).
Presentation design reference: [Codex backend prompt](https://github.com/openai/codex/blob/94697375cb9d2aa8ae74d61957c6b396819bec94/codex-rs/prompts/templates/realtime/backend_prompt.md).
The implementation and instructions above are original; no Codex source text was copied.
