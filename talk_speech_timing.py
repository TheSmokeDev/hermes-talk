"""Connection-fenced, content-free readiness observations for spoken task updates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import ClassVar

try:
    from .talk_task_sources import TaskEventError
except ImportError:
    from talk_task_sources import TaskEventError


@dataclass
class SpeechTiming:
    capture: object
    clock: object = time.monotonic
    sequence: int = -1
    observed_at: float | None = None
    blockers: dict = field(default_factory=dict)
    FIELDS: ClassVar[frozenset[str]] = frozenset({
        "operator_speaking", "playback_active", "response_pending", "input_pending", "tools_pending"
    })

    def observe(self, capture, snapshot):
        if capture != self.capture:
            raise TaskEventError("foreign_owner")
        if (not isinstance(snapshot, dict) or set(snapshot) != self.FIELDS | {"sequence"}
            or type(snapshot["sequence"]) is not int or not 0 <= snapshot["sequence"] < 2**53
            or any(type(snapshot[key]) is not bool for key in self.FIELDS)):
            raise TaskEventError("invalid_event")
        sequence = snapshot["sequence"]
        values = {key: snapshot[key] for key in self.FIELDS}
        if sequence < self.sequence:
            return False
        if sequence == self.sequence:
            if values != self.blockers:
                raise TaskEventError("event_conflict")
            return self.ready(capture)
        self.sequence = sequence
        self.blockers = values
        self.observed_at = self.clock()
        return self.ready(capture)

    def ready(self, capture):
        if capture != self.capture:
            raise TaskEventError("foreign_owner")
        return (self.observed_at is not None and self.clock() - self.observed_at <= 5.0
                and not any(self.blockers.values()))
