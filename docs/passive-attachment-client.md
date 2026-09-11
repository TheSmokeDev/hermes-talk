# Shared passive attachment client

`TalkAttachment` implements the passive-history v1 protocol for a configured
Hermes host. It is a shared Python library; dashboard, terminal and Discord
lifecycle wiring and execution-origin integration are separate work.

Trusted host code supplies the exact selected session, resolved profile name and
absolute profile home. Construct `HistoryTransport` with the configured host
origin and credential; it captures these once and calls only fixed passive-history
routes. `configured_gateway(profile=...)` uses existing Talk API-server config.
Named gateway profiles require `named_profile=True`. Dashboard transport uses
`surface="dashboard"` and the existing session token. OAuth wiring is not supplied
by this client. URLs, credentials, profile homes and owner identity must never come
from model tool arguments, transcript text or unverified client request fields.

```python
from talk_attachment import TalkAttachment
from talk_outbox import HistoryOutbox
from talk_passive import HistoryMessage, HistoryTransport

# These values are already resolved by authenticated host integration code.
transport = HistoryTransport.configured_gateway(profile=resolved_profile)
outbox = HistoryOutbox(resolved_profile_home, profile=resolved_profile)
attachment = TalkAttachment(transport, outbox, selected_session=selected_session)
token = attachment.attach()
# Keep the utterance's original identity across reconnect and retry.
event = attachment.enqueue(
    token, (HistoryMessage("user", finalized_text),), origin_turn_id=origin_turn_id,
    finalized=True, disposition="dialogue",
)
result = attachment.flush(event, token)
```

Every method that performs I/O belongs in a worker, such as an `asyncio.to_thread`
call. There is no retry loop, inference, tool dispatch or automatic chat fallback.
The realtime manager and existing provider/surface behavior remain unchanged.

The caller must wait until the **whole interaction** is known to be ordinary
dialogue before enqueueing. A finalized user transcript alone does not prove that
its realtime response will not dispatch work. Pass `disposition="dialogue"` only
after that classification; incomplete fragments, tool/worker results and utterances
that may enter execution or steering are refused. This client does not adopt
execution-origin receipts even if a newer host advertises that additional operation.

`attach()` negotiates version 1 and required operations/limits, reconciles pending
events for the original owner, then obtains a fresh snapshot/attachment. The outer
selected session remains the target even when the snapshot names a compression
successor. `capture_token` fences callbacks; use the new token after reconnect.
`flush()` returns pending, saved, conflicted or failed. A repeated attempt reconciles
the original event and owner; unknown requires reattachment before committing.
That may advance `capture_token` during flush, invalidating earlier callbacks.
Keep the original event ID for every retry. A new task uses a new attachment;
recovery must return to the original owner, never retarget pending speech.

`refresh_snapshot()` detects host invalidation. `close(token)` fences local
callbacks and discards context before exact-generation detach, even if its response
is lost. Host restart, deletion and message retirement have no push notification in
v1; refresh/reconcile is required to learn that state. Invalid attachments discard
cached context; deletion/retirement scrub derived owner references and queued text.
Receipt retirement scrubs only that event; unrelated pending dialogue survives.
Only confirmed target deletion purges the owner's queue. Reconnect and error
callbacks compare the expected generation inside the outbox writer transaction.

The derived SQLite outbox lives at `state/talk-history-outbox.sqlite3` beneath the
explicit profile home. Admission is transactional across processes and bounded to
128 entries, 1 MiB pending UTF-8 JSON and a 24-hour TTL (configurable downward).
Successful or terminal delivery removes text. Expiration leaves content-free status
for another TTL; terminal entries may be evicted for new dialogue. It is not a
history archive or an execution/approval ledger. The resolved profile home must be
operator-private; file permission tightening supplements that host responsibility.
`diagnostics()` exposes counts and protocol/attachment state only. Errors expose a
closed vocabulary, never raw transport bodies, credentials, transcripts or paths.

The client targets an additive host contract that must actually be advertised.
Missing support is explicit; local tests/builds do not establish installed or live
host availability, and do not complete the broader Voice Manager feature.
