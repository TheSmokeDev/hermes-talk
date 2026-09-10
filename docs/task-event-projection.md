# Shared task observations

`TaskEvents` is a worker-side library for restoring task/work state. It uses the
same profile-resolved derived SQLite file and immutable `CaptureToken` owner as
the passive attachment client. Event insertion, observation cursors and delivery
updates check the current connection generation inside one writer transaction.
It neither creates work nor consumes the host API event stream.

Construct it after a successful attachment: `TaskEvents(outbox, attachment.capture_token)`.
Authenticated host integration supplies the sources and accepted-run records;
model arguments, arbitrary browser owner IDs and transcript text are not authority.
`bind_run` verifies an existing ticket's exact canonical session, profile, operator
and request ID. The operator mapping is supplied by the authenticated integration.
Missing canonical ticket ownership refuses. Worker-session/API-run/origin links
are immutable, explicitly supplied accepted-work facts; selection never rewrites them.

| Source | Input | Recovery semantics |
|---|---|---|
| RPC | Actual `session.events.since` result: events/epoch/latest_seq/truncated/count | Per-session source seq and epoch, contiguous cursor, missing-sequence/truncation gaps |
| API polling | Existing `get_run`/progress callback status snapshot | Snapshot-only; source epoch/seq unavailable; old snapshots do not reopen terminal state |
| Hooks | Existing post_tool_call/pre_approval_request callback | Exact bound worker session; caller retains observation ID; source ordering/replay unavailable |
| Saved dialogue | Verified P2a `HistoryDelivery` plus its existing saved outbox entry | Reference to original origin/event and canonical receipt revision; no new history write |

`open_source` takes the actual RPC handshake epoch. API polls and hooks take no
invented epoch. Poll and worker sources require their bound local run ID. Keep
the returned `SourceLease`; changes use its `previous` value for a compare-and-swap.
`resume_source` retrieves the stored cursor scope on reconnect; authenticate and
check the actual current host epoch before supplying its frames. A changed epoch
resets the source cursor and reports a gap. Old source leases cannot overwrite it.

`observe_rpc`, `observe_poll` and `observe_hook` persist only normalized references,
fixed state/labels and causal metadata. Raw transcript text, tool arguments/results,
approval commands, credentials and worker output are not copied. RPC message deltas
are excluded. Unsupported RPC event kinds advance only the observed source cursor;
the projection does not claim it understood their contents.

`page(after=..., limit=...)` returns a local **observation** cursor. It is never a
host event sequence or canonical message order. Each event separately preserves
supplied source occurrence/sequence/epoch, action/origin links and verified canonical
revision when available. Missing timing stays unknown. Delayed observations append;
they never rewrite earlier messages or fabricate capture order. Truncation/reset
requires snapshot refetch; refetching current state cannot prove missing history.
API polling remains snapshot-only after stream loss: no extra SSE consumer and no
`Last-Event-ID` claim. Call `source_unavailable` to expose a failed source explicitly.
Source availability is separate from replay integrity. Restored connectivity does
not clear an unrecovered truncation or epoch-reset gap.

Replay always returns `speak: false`. Completed result references can call
`result_view`, which uses the existing registry/history through `resolve_run_record`, rechecks the immutable
ticket, caps display output and does not persist that output. It never acknowledges
the existing run's delivery or invokes its worker.
The exact durable lookup is bounded by the existing history tail, not the UI's
100-row listing cap; nonterminal history without a live worker is explicitly lost.

Speech is a separate explicit surface action. Only a first live observation from
the current connection can call `queue_speech`; replay cannot promote eligibility.
Retain its attempt token and record `sent` only after transport handoff. Record
`playback_acknowledged` only when that surface supplied a real playback acknowledgement
and declared support. A crash around queued/sent state becomes `unknown` on reconnect;
it is never automatically requeued. Unsupported playback evidence stays unsupported.

An approval event is only a reference. Every `approval_view` calls an owner-bound
`ApprovalReader` supplied by authenticated host integration, using actual
`approval.pending` or `list_gateway_approvals`. Poll metadata or replay caches are
not replacements. Missing/failed authority reads cannot restore permission. Views
return `actionable: false`, even for a currently observed pending request. Submission
stays at the existing host resolver, with its exact request ID and current authorization;
the resolver may reject a request resolved/revoked after the view. This library adds
no approval decision service, permanent grant, or permission token.

Default retention is 256 events per owner, bounded to 512 per profile and 1 MiB of
normalized payloads, 32 owners, 64 sources and 128 run bindings. Individual records
are capped at 4 KiB; TTL is at most 24 hours. Limits may be lowered. Capacity pressure
evicts only the inserting owner's oldest events; it never deletes a foreign owner
to make room. Expiration reports missing source/retention state, not an empty proof
of complete history. Confirmed canonical target deletion cascades the authorized
owner's event/source/run/delivery references through the P2a invalidation path.
Receipt-specific retirement drops that canonical receipt's derived reference and
retains unrelated task observations.
Before global admission checks, expired derived records are reclaimed across the
profile; unexpired foreign state remains intact. Source and run-binding inactivity
TTLs are independent of owner activity. Only observations or revalidation for that
specific source/run refresh its retention, so an active owner cannot pin abandoned
source/run records indefinitely.

Dashboard/terminal/Discord wiring and dispatch-origin/linked-child integration remain
separate. No remote/Codex or provider capability is inferred from these local modules.
