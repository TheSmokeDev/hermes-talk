"""Bounded observations from existing host contracts; no transport or authority creation."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

try:
    from .talk_passive import HistoryOwner, digest, identifier, session_id
    from .talk_progress import phase_for_api_event, tool_label
except ImportError:  # pragma: no cover - flat plugin load
    from talk_passive import HistoryOwner, digest, identifier, session_id
    from talk_progress import phase_for_api_event, tool_label


class TaskEventError(Exception):
    """Fixed diagnostic codes; source text, credentials and paths never become errors."""

    CODES = frozenset(
        {
            "invalid_event",
            "foreign_owner",
            "stale_source",
            "event_conflict",
            "unavailable",
            "unsupported",
            "missing_reference",
            "capacity",
            "delivery_exists",
            "invalid_delivery",
            "replay_not_speakable",
        }
    )

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "invalid_event"
        super().__init__(self.code)


def integer(value, *, minimum=0):
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise TaskEventError("invalid_event")
    return value


def timestamp(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise TaskEventError("invalid_event")
    return value


@dataclass(frozen=True, slots=True)
class RunReference:
    """An existing accepted run, bound once by trusted authenticated integration code."""

    local_run_id: int
    request_id: str
    worker_session_id: str | None
    api_run_id: str | None
    origin_turn_id: str | None

    @classmethod
    def from_ticket(
        cls,
        owner: HistoryOwner,
        run: dict,
        *,
        operator: str,
        worker_session_id: str | None,
        api_run_id: str | None = None,
        origin_turn_id: str | None = None,
    ):
        ticket = run.get("ticket")
        if not isinstance(ticket, dict) or (
            ticket.get("hermesSessionId") != owner.session_id
            or ticket.get("profile") != owner.profile
            or ticket.get("operator") != operator
            or not operator
        ):
            raise TaskEventError("foreign_owner")
        return cls(
            integer(run.get("runId"), minimum=1),
            identifier(ticket.get("requestId")),
            session_id(worker_session_id) if worker_session_id is not None else None,
            identifier(api_run_id) if api_run_id is not None else None,
            identifier(origin_turn_id) if origin_turn_id is not None else None,
        )


@dataclass(frozen=True, slots=True)
class TaskObservation:
    event_id: str
    kind: str
    session_id: str
    run_id: int | None = None
    action_id: str | None = None
    origin_turn_id: str | None = None
    source_seq: int | None = None
    occurred_at: float | None = None
    state: str | None = None
    label: str | None = None
    approval_id: str | None = None
    canonical_revision: int | None = None

    def wire(self):
        return asdict(self)


_RPC_KINDS = {
    "tool.start": "tool_started",
    "tool.complete": "tool_completed",
    "approval.request": "approval_reference",
    "message.complete": "response_completed",
    "message.start": "response_started",
    "error": "source_error",
}
_RUN_STATES = frozenset(
    {"queued", "running", "waiting_for_approval", "completed", "failed", "cancelled", "lost"}
)


def rpc_observations(
    payload: dict, *, source_id: str, expected_epoch: str, source_session: str
) -> tuple[tuple[TaskObservation, ...], tuple[int, ...]]:
    """Normalize the result of session.events.since; text/deltas/args are not persisted."""
    if not isinstance(payload, dict) or payload.get("epoch") != expected_epoch:
        raise TaskEventError("stale_source")
    frames = payload.get("events")
    if (
        not isinstance(frames, list)
        or len(frames) > 512
        or type(payload.get("truncated")) is not bool
        or type(payload.get("count")) is not int
        or payload["count"] != len(frames)
    ):
        raise TaskEventError("invalid_event")
    latest = integer(payload.get("latest_seq"))
    result, sequences = [], []
    for frame in frames:
        if not isinstance(frame, dict) or frame.get("session_id") != source_session:
            raise TaskEventError("foreign_owner")
        seq = integer(frame.get("seq"), minimum=1)
        if seq > latest:
            raise TaskEventError("invalid_event")
        sequences.append(seq)
        if not isinstance(frame.get("type"), str):
            raise TaskEventError("invalid_event")
        kind = _RPC_KINDS.get(frame["type"])
        if kind is None:
            continue
        data = frame.get("payload") or {}
        if not isinstance(data, dict):
            raise TaskEventError("invalid_event")
        action = data.get("tool_id") or data.get("request_id")
        action = identifier(action) if action is not None else None
        result.append(
            TaskObservation(
                digest([source_id, expected_epoch, seq]),
                kind,
                source_session,
                action_id=action,
                source_seq=seq,
                label=tool_label(data.get("name")) if kind.startswith("tool_") else None,
                approval_id=identifier(data.get("request_id"))
                if kind == "approval_reference"
                else None,
            )
        )
    return tuple(result), tuple(sequences)


def api_poll_observation(
    payload: dict, binding: RunReference, *, source_id: str
) -> TaskObservation:
    """Snapshot identity, not an invented API event sequence or replay epoch."""
    if not isinstance(payload, dict) or not binding.api_run_id:
        raise TaskEventError("unsupported")
    if payload.get("run_id") != binding.api_run_id:
        raise TaskEventError("foreign_owner")
    observed_session = payload.get("child_session_id") or payload.get("session_id")
    if observed_session and observed_session != binding.worker_session_id:
        raise TaskEventError("foreign_owner")
    state = payload.get("status")
    if not isinstance(state, str) or state not in _RUN_STATES:
        raise TaskEventError("invalid_event")
    occurred = timestamp(payload.get("updated_at"))
    if occurred is None:
        raise TaskEventError("invalid_event")
    phase = phase_for_api_event(payload.get("last_event"))
    approval = payload.get("approval")
    request_id = approval.get("request_id") if isinstance(approval, dict) else None
    if request_id is not None:
        request_id = identifier(request_id)
    return TaskObservation(
        digest([source_id, binding.api_run_id, occurred, state, phase, request_id]),
        "run_state",
        binding.worker_session_id or "",
        binding.local_run_id,
        binding.request_id,
        binding.origin_turn_id,
        occurred_at=occurred,
        state=state,
        label=phase,
        approval_id=request_id if state == "waiting_for_approval" else None,
    )


def hook_observation(
    kind: str, payload: dict, *, observation_id: str, binding: RunReference
) -> TaskObservation:
    """The caller retains its observation ID on retry; hooks supply no replay sequence."""
    if not isinstance(payload, dict) or not binding.worker_session_id:
        raise TaskEventError("unsupported")
    if payload.get("session_id") != binding.worker_session_id:
        raise TaskEventError("foreign_owner")
    mapped = {"post_tool_call": "tool_completed", "pre_approval_request": "approval_reference"}
    if kind not in mapped:
        raise TaskEventError("unsupported")
    request_id = payload.get("request_id")
    return TaskObservation(
        identifier(observation_id),
        mapped[kind],
        binding.worker_session_id,
        binding.local_run_id,
        binding.request_id,
        binding.origin_turn_id,
        label=tool_label(payload.get("tool_name")) if kind == "post_tool_call" else None,
        approval_id=identifier(request_id) if request_id is not None else None,
    )
