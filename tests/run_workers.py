"""Own test workers until their final registry/history writes have finished."""

from __future__ import annotations

import threading
import time

import wallclock


class RunWorkers:
    def __init__(self):
        self.threads = []
        self.releases = []

    def track(self, thread):
        self.threads.append(thread)
        return thread

    def thread(self, *args, **kwargs):
        # Track at construction, before start: teardown must also own a worker
        # which has been dispatched but has not entered its target yet.
        return self.track(threading.Thread(*args, **kwargs))

    def release_on_exit(self, event):
        self.releases.append(event)

    def join_all(self):
        for event in self.releases:
            event.set()
        deadline = time.monotonic() + wallclock.stretch(5.0)
        # A tracked dispatch thread can append a run worker while it drains.
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), f"test worker did not finish: {thread.name}"
