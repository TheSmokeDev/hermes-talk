"""Concrete consumer of Hermes' optional generic, profile-scoped task worker hook."""

from __future__ import annotations

import queue
import threading
from contextlib import suppress

try:
    from .talk_codex_store import CodexJobs
    from .talk_codex_wire import CodexWorkerError
    from .talk_codex_worker import CodexWorker, CodexWorkerConfig
    from .talk_outbox import HistoryOutbox
    from .talk_passive import HistoryOwner, digest
except ImportError:
    from talk_codex_store import CodexJobs
    from talk_codex_wire import CodexWorkerError
    from talk_codex_worker import CodexWorker, CodexWorkerConfig
    from talk_outbox import HistoryOutbox
    from talk_passive import HistoryOwner, digest

WORKER_NAME = "hermes-talk-codex"


def build_provider(ctx):
    try:
        from agent.task_worker_provider import TaskWorkerProvider, TaskWorkerSession
    except ImportError:
        return None

    class Session(TaskWorkerSession):
        def __init__(self, request, config):
            self.request, self.config = request, config
            self.cancelled, self.closed = threading.Event(), threading.Event()
            self.controls = queue.Queue(maxsize=64)
            self.owner = HistoryOwner(
                digest(["task-worker", str(request.profile_home.resolve())]),
                request.profile,
                digest(request.owner_scope),
                request.parent_session_id,
            )
            self.jobs = CodexJobs(HistoryOutbox(request.profile_home, profile=request.profile))
            self.worker = CodexWorker(
                self.jobs,
                self.owner,
                request.run_id,
                {
                    "parent_session_id": request.parent_session_id,
                    "child_session_id": request.child_session_id,
                    "origin_turn_id": request.origin_turn_id,
                    "action_id": request.action_id,
                    "goal": request.goal,
                    "context": request.context,
                },
                config,
                cancel_event=self.cancelled,
                on_change=self.report,
                authorize=request.still_authorized,
            )

        def report(self, record):
            if not self.request.still_authorized():
                self.cancelled.set()
                raise CodexWorkerError("owner_retired")
            if record["state"] not in {"completed", "failed", "interrupted", "unknown"}:
                phase = "waiting_for_approval" if self.worker.approvals() else "running"
                self.request.report({"status": phase})

        def _pump(self):
            while not self.closed.is_set():
                try:
                    action_id, text, turn_id = self.controls.get(timeout=0.2)
                except queue.Empty:
                    continue
                with suppress(CodexWorkerError):
                    if (
                        self.request.still_authorized()
                        and self.worker.snapshot()["turn_id"] == turn_id
                    ):
                        self.worker.control(action_id, text=text)

        def run(self):
            thread = threading.Thread(target=self._pump, daemon=True)
            thread.start()
            try:
                record = self.worker.run()
                if (
                    record["state"] == "unknown"
                    and record["thread_id"]
                    and not self.cancelled.is_set()
                    and self.request.still_authorized()
                ):
                    # The old process is closed. Reconcile only this recorded thread once.
                    record = self.worker.run()
                if not self.request.still_authorized():
                    raise ValueError("Worker owner was retired")
                status = {
                    "completed": "completed",
                    "failed": "failed",
                    "interrupted": "cancelled",
                }.get(record["state"], "unknown")
                return {
                    "status": status,
                    "output": record["output"],
                    "artifacts": [item for item in record["items"] if item["type"] == "fileChange"],
                    "error": record["error"]
                    or ("worker_outcome_unknown" if status == "unknown" else None),
                }
            finally:
                self.closed.set()
                thread.join(timeout=3)
                if not self.request.still_authorized():
                    with self.jobs.outbox._db() as db:
                        db.execute(
                            "DELETE FROM codex_jobs WHERE owner=? AND job_id=?",
                            (self.owner.key, self.request.run_id),
                        )

        def cancel(self):
            self.cancelled.set()

        def steering(self):
            record = self.worker.snapshot()
            if self.closed.is_set() or record["state"] != "running" or self.cancelled.is_set():
                return {"supported": False, "reason": "worker_not_available"}
            return {"supported": True, "turn_id": record["turn_id"]}

        def steer(self, text, *, action_id, expected_session_id, expected_turn_id):
            target = self.steering()
            if (
                not target["supported"]
                or expected_session_id != self.request.child_session_id
                or expected_turn_id != target["turn_id"]
                or not self.request.still_authorized()
            ):
                return "rejected"
            try:
                self.controls.put_nowait((action_id, text, expected_turn_id))
            except queue.Full:
                return "rejected"
            return "queued"

        def approvals(self):
            if (
                self.cancelled.is_set()
                or self.closed.is_set()
                or not self.request.still_authorized()
            ):
                return []
            return self.worker.approvals()

        def approve(self, request_id, choice):
            if request_id not in {item["request_id"] for item in self.approvals()}:
                raise ValueError("Worker approval is not current")
            try:
                return self.worker.approve(request_id, choice)
            except CodexWorkerError:
                raise ValueError("Worker approval outcome is unconfirmed") from None

    class Provider(TaskWorkerProvider):
        name = WORKER_NAME

        def configuration(self):
            raw = ctx.get_config("codex_worker", {})
            if not isinstance(raw, dict):
                raise CodexWorkerError("worker_unconfigured")
            try:
                return CodexWorkerConfig(**raw)
            except TypeError:
                raise CodexWorkerError("worker_unconfigured") from None

        def available(self):
            try:
                self.configuration().validate()
                return True
            except CodexWorkerError:
                return False

        def open(self, request):
            if not request.still_authorized():
                raise ValueError("Task worker owner is not current")
            return Session(request, self.configuration())

    return Provider()
