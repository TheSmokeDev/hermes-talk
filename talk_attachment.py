"""Shared canonical Talk attachment lifecycle, independent of voice providers.

Use from a worker. Nothing here dispatches tools, starts inference or wires a
voice surface. Trusted surface code classifies finalized ordinary dialogue;
execution-origin input, tool results and fragments have different owners.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass

try:
    from .talk_outbox import HistoryOutbox, PendingHistory
    from .talk_passive import (
        HistoryCapabilities,
        HistoryError,
        HistoryMessage,
        HistoryOwner,
        HistoryReceipt,
        HistorySnapshot,
        HistoryTransport,
        dialogue_messages,
        identifier,
    )
except ImportError:  # pragma: no cover - flat Hermes plugin load
    from talk_outbox import HistoryOutbox, PendingHistory
    from talk_passive import (
        HistoryCapabilities,
        HistoryError,
        HistoryMessage,
        HistoryOwner,
        HistoryReceipt,
        HistorySnapshot,
        HistoryTransport,
        dialogue_messages,
        identifier,
    )


@dataclass(frozen=True, slots=True)
class CaptureToken:
    owner: HistoryOwner
    connection_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class AttachmentAuthority:
    capture: CaptureToken
    attachment_id: str
    generation: int

    def wire(self) -> dict:
        return {
            "tab_id": self.capture.connection_id,
            "attachment_id": self.attachment_id,
            "generation": self.generation,
            "session_id": self.capture.owner.session_id,
        }


@dataclass(frozen=True, slots=True)
class HistoryDelivery:
    state: str
    code: str = ""
    receipt: HistoryReceipt | None = None


class TalkAttachment:
    """One immutable host/profile/principal/selected-task owner for its lifetime.

    Reuse ``connection_id`` only to reconnect the same trusted surface/tab. A
    different task requires a new instance; pending events never follow selection.
    All methods are worker calls, serialized per instance; the durable generation
    also fences separate instances/processes sharing this connection's identity.
    """

    def __init__(
        self,
        transport: HistoryTransport,
        outbox: HistoryOutbox,
        *,
        selected_session: str,
        connection_id: str | None = None,
    ):
        self._transport, self._outbox = transport, outbox
        self._owner = transport.owner(selected_session)
        self._connection = identifier(connection_id or uuid.uuid4().hex)
        self._token: CaptureToken | None = None
        self._authority: AttachmentAuthority | None = None
        self._snapshot: HistorySnapshot | None = None
        self._caps: HistoryCapabilities | None = None
        self._lock = threading.RLock()

    @property
    def owner(self) -> HistoryOwner:
        return self._owner

    @property
    def capture_token(self) -> CaptureToken | None:
        return self._token

    @property
    def snapshot(self) -> HistorySnapshot | None:
        with self._lock:
            if self._token is not None:
                try:
                    self._check(self._token)
                except HistoryError:
                    self._snapshot = None
                    self._authority = None
                    raise
            return self._snapshot

    def _check(self, token: CaptureToken):
        if token != self._token or token.owner != self._owner:
            raise HistoryError("stale_generation")
        self._outbox.check(self._owner, self._connection, token.generation)

    def _forget_context(self):
        self._authority = None
        self._snapshot = None

    def _host_error(self, exc: HistoryError, token: CaptureToken, event_id: str | None = None):
        self._check(token)
        if exc.code in {"target_missing", "target_unavailable", "retired"}:
            # A retired receipt says only that this event's canonical rows are gone.
            # Only confirmed target deletion invalidates the whole owner's queue.
            if exc.code == "target_missing" or event_id is not None:
                self._outbox.invalidate(
                    self._owner,
                    code=exc.code,
                    connection_id=self._connection,
                    generation=token.generation,
                    event_id=None if exc.code == "target_missing" else event_id,
                )
            self._forget_context()
            if exc.code == "target_missing":
                self._token = None
        elif exc.code in {"stale_attachment", "unauthorized", "malformed_response"}:
            self._forget_context()

    def _profile(self, data: dict):
        if data.get("profile") != self._owner.profile:
            raise HistoryError("malformed_response")

    def _mark(self, event_id: str, token: CaptureToken, **change) -> str:
        return self._outbox.mark(
            self._owner,
            event_id,
            connection_id=self._connection,
            generation=token.generation,
            **change,
        )

    def _advance(self, expected: CaptureToken | None = None) -> CaptureToken:
        generation = self._outbox.begin(
            self._owner,
            self._connection,
            expected_generation=expected.generation if expected else None,
        )
        self._forget_context()
        token = CaptureToken(self._owner, self._connection, generation)
        self._token = token
        return token

    def _reattach(self, token: CaptureToken) -> CaptureToken:
        self._check(token)
        data = self._transport.request(
            "attach",
            {"tab_id": self._connection, "session_id": self._owner.session_id},
        )
        self._check(token)
        self._profile(data)
        if (
            data.get("tab_id") != self._connection
            or data.get("session_id") != self._owner.session_id
            or type(data.get("generation")) is not int
            or data["generation"] < 1
        ):
            raise HistoryError("malformed_response")
        try:
            attachment_id = identifier(data.get("attachment_id"))
        except HistoryError:
            raise HistoryError("malformed_response") from None
        snapshot = HistorySnapshot.parse(data.get("snapshot"))
        self._authority = AttachmentAuthority(token, attachment_id, data["generation"])
        self._snapshot = snapshot
        return token

    def attach(self) -> CaptureToken:
        """Negotiate, reconcile this original owner's pending events, then attach.

        Used for first attachment and reconnect. No commits happen here. Unknown
        receipts remain pending for explicit ``flush``; a retry never changes owner.
        """
        with self._lock:
            self._forget_context()
            self._caps = None
            token = self._advance()
            try:
                self._caps = HistoryCapabilities.parse(self._transport.request("capabilities"))
                self._check(token)
                for event_id in self._outbox.pending(self._owner):
                    event = self._outbox.get(self._owner, event_id)
                    try:
                        receipt = self._reconcile(event)
                    except HistoryError as exc:
                        if exc.code != "retired":
                            raise
                        self._host_error(exc, token, event_id)
                        continue
                    self._check(token)
                    if receipt is not None:
                        self._mark(event_id, token, state="saved")
                token = self._advance(token)
                return self._reattach(token)
            except HistoryError as exc:
                self._host_error(exc, token)
                raise

    def refresh_snapshot(self, token: CaptureToken) -> HistorySnapshot:
        with self._lock:
            self._check(token)
            if self._authority is None:
                raise HistoryError("not_attached")
            try:
                data = self._transport.request("snapshot", self._authority.wire())
                self._check(token)
                self._profile(data)
                snapshot = HistorySnapshot.parse(data)
                if self._snapshot and snapshot.conversation_id != self._snapshot.conversation_id:
                    raise HistoryError("malformed_response")
                self._snapshot = snapshot
                return snapshot
            except HistoryError as exc:
                self._host_error(exc, token)
                raise

    def enqueue(
        self,
        token: CaptureToken,
        messages: tuple[HistoryMessage, ...],
        *,
        origin_turn_id: str,
        finalized: bool,
        disposition: str,
        event_id: str | None = None,
    ) -> str:
        """Explicit admission, with no NLP guessing and no default disposition.

        Only finalized ``dialogue`` is admitted. Callers must withhold utterances
        that may also enter execution/steering, even if origin adoption is advertised.
        Wait until the whole realtime interaction can be classified: a finalized
        user transcript alone does not prove that its response will not dispatch work.
        Retain the returned event ID for retries; never mint a new event to retry.
        """
        if finalized is not True or disposition != "dialogue":
            raise HistoryError("not_passive")
        with self._lock:
            self._check(token)
            if self._authority is None or self._snapshot is None or self._caps is None:
                raise HistoryError("not_attached")
            rows = dialogue_messages(messages, max_bytes=self._caps.max_message_bytes)
            if len(rows) > self._caps.max_messages:
                raise HistoryError("invalid_input")
            event_id = identifier(event_id or uuid.uuid4().hex)
            origin_turn_id = identifier(origin_turn_id)
            body = {
                **self._authority.wire(),
                "event_id": event_id,
                "origin_turn_id": origin_turn_id,
                "messages": [row.wire() for row in rows],
            }
            if (
                len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
                > self._caps.max_request_bytes
            ):
                raise HistoryError("payload_too_large")
            self._outbox.add(
                PendingHistory(
                    event_id,
                    origin_turn_id,
                    self._owner,
                    self._snapshot.conversation_id,
                    self._connection,
                    token.generation,
                    rows,
                    "pending",
                    0,
                    "",
                )
            )
            return event_id

    def _reconcile(self, event: PendingHistory) -> HistoryReceipt | None:
        data = self._transport.request(
            "reconcile",
            {
                "session_id": event.owner.session_id,
                "event_id": event.event_id,
            },
        )
        self._profile(data)
        if data.get("status") == "unknown" and data.get("receipt", "missing") is None:
            return None
        if data.get("status") != "saved":
            raise HistoryError("malformed_response")
        return self._receipt(data, event)

    def _receipt(self, data: dict, event: PendingHistory) -> HistoryReceipt:
        self._profile(data)
        return HistoryReceipt.parse(
            data.get("receipt"),
            event_id=event.event_id,
            origin_turn_id=event.origin_turn_id,
            conversation_id=event.conversation_id,
            message_count=len(event.messages),
        )

    def flush(self, event_id: str, token: CaptureToken) -> HistoryDelivery:
        """One bounded delivery attempt. The caller owns scheduling/backoff.

        Before every repeated/older-generation commit, reconcile the same owner and
        event. Unknown requires a fresh attachment to that owner. Never auto-loop.
        """
        with self._lock:
            self._check(token)
            event = self._outbox.get(self._owner, identifier(event_id))
            if event.state != "pending":
                return HistoryDelivery(event.state, event.code)
            if self._caps is None:
                raise HistoryError("not_attached")
            try:
                if event.attempts or event.generation != token.generation:
                    receipt = self._reconcile(event)
                    self._check(token)
                    if receipt is not None:
                        self._mark(event_id, token, state="saved")
                        return HistoryDelivery("saved", receipt=receipt)
                    token = self._advance(token)
                    self._reattach(token)
                if self._authority is None:
                    raise HistoryError("not_attached")
                if (
                    self._snapshot is None
                    or self._snapshot.conversation_id != event.conversation_id
                ):
                    raise HistoryError("event_conflict")
                self._check(token)
                # Durable attempt precedes HTTP so a crash/lost response always reconciles.
                if self._mark(event_id, token, attempted=True) != "pending":
                    current = self._outbox.get(self._owner, event_id)
                    return HistoryDelivery(current.state, current.code)
                data = self._transport.request(
                    "commit",
                    {
                        **self._authority.wire(),
                        "event_id": event.event_id,
                        "origin_turn_id": event.origin_turn_id,
                        "messages": [row.wire() for row in event.messages],
                    },
                )
                self._check(token)
                if data.get("status") not in {"saved", "already_saved"}:
                    raise HistoryError("malformed_response")
                receipt = self._receipt(data, event)
                self._mark(event_id, token, state="saved")
                return HistoryDelivery("saved", receipt=receipt)
            except HistoryError as exc:
                self._host_error(exc, token, event_id)
                if exc.code in {"target_missing", "target_unavailable", "retired"}:
                    return HistoryDelivery("failed", exc.code)
                if exc.code in {"outbox_unavailable", "stale_generation"}:
                    raise
                if exc.code == "event_conflict":
                    state = "conflicted"
                elif exc.retryable or exc.code in {"malformed_response", "stale_attachment"}:
                    state = "pending"
                else:
                    state = "failed"
                state = self._mark(event_id, token, state=state, code=exc.code)
                return HistoryDelivery(state, exc.code)

    def close(self, token: CaptureToken):
        """Fence local callbacks before requesting exact remote-generation detach.

        Pending events remain bound to the original owner for later reconciliation.
        A lost detach response never restores local authority or cached context.
        """
        with self._lock:
            self._check(token)
            authority = self._authority
            self._outbox.end(self._owner, self._connection, token.generation)
            self._token = None
            self._forget_context()
            if authority is None:
                return
            data = self._transport.request("detach", authority.wire())
            if data != {"status": "detached"}:
                raise HistoryError("malformed_response")

    def diagnostics(self) -> dict:
        with self._lock:
            attached = False
            if self._token is not None and self._authority is not None:
                try:
                    self._check(self._token)
                    attached = True
                except HistoryError:
                    self._forget_context()
            return {
                "attached": attached,
                "protocol": 1 if self._caps else None,
                **self._outbox.diagnostics(),
            }
