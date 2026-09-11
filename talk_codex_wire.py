"""Bounded local stdio client for the pinned Codex app-server protocol."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import uuid
from concurrent.futures import Future, TimeoutError
from contextlib import suppress

SUPPORTED_CODEX_VERSION = "0.154.0"
MAX_FRAME_BYTES = 4 * 1024 * 1024


class CodexWorkerError(Exception):
    """Fixed diagnostic codes only; never serialize subprocess output or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class CodexAppServer:
    def __init__(self, command, *, cwd, timeout=10.0, version_command=None):
        self.command = tuple(command)
        self.version_command = tuple(version_command or (self.command[0], "--version"))
        self.cwd, self.timeout = cwd, timeout
        self.process = None
        self.epoch = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._writes = queue.Queue(maxsize=16)
        self._pending = {}
        self._next_id = 0
        self._events = queue.Queue(maxsize=256)
        self._failure = None

    def start(self):
        if self.process is not None:
            raise CodexWorkerError("already_started")
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else {}
        )
        try:
            version = subprocess.run(
                self.version_command,
                cwd=self.cwd,
                capture_output=True,
                timeout=5,
                check=False,
                **options,
            )
            expected = f"codex-cli {SUPPORTED_CODEX_VERSION}"
            if version.returncode or version.stdout.decode("utf-8").strip() != expected:
                raise CodexWorkerError("unsupported_version")
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **options,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            raise CodexWorkerError("process_unavailable") from None
        threading.Thread(target=self._write, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        try:
            initialized = self.request(
                "initialize",
                {"clientInfo": {"name": "hermes_talk", "title": "Hermes Talk", "version": "1"}},
            )
            if not isinstance(initialized.get("userAgent"), str):
                raise CodexWorkerError("invalid_protocol")
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise
        return self

    def _drain_stderr(self):
        while self.process.stderr.read(4096):
            pass

    def _fail(self, code):
        with self._lock:
            self._failure = code
            pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(CodexWorkerError(code))

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                    raise CodexWorkerError("invalid_protocol")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise CodexWorkerError("invalid_protocol")
                if "method" in message:
                    if not isinstance(message["method"], str):
                        raise CodexWorkerError("invalid_protocol")
                    if message["method"].endswith("/delta"):
                        continue  # Keep full items; partial text is never authority.
                    try:
                        self._events.put_nowait(message)
                    except queue.Full:
                        raise CodexWorkerError("event_capacity") from None
                    continue
                request_id = message.get("id")
                if type(request_id) not in (int, str):
                    raise CodexWorkerError("invalid_protocol")
                with self._lock:
                    future = self._pending.pop(request_id, None)
                if future is None:
                    continue
                if "error" in message:
                    future.set_exception(CodexWorkerError("rpc_refused"))
                elif isinstance(message.get("result"), dict):
                    future.set_result(message["result"])
                else:
                    future.set_exception(CodexWorkerError("invalid_protocol"))
        except (OSError, ValueError, UnicodeError, CodexWorkerError) as exc:
            self._fail(exc.code if isinstance(exc, CodexWorkerError) else "invalid_protocol")
        finally:
            self._fail(self._failure or "disconnected")

    def _write(self):
        try:
            while True:
                entry = self._writes.get()
                if entry is None:
                    return
                frame, future, authorize = entry
                if self._failure:
                    future.set_exception(CodexWorkerError(self._failure))
                    continue
                if authorize is not None:
                    try:
                        allowed = authorize() is True
                    except Exception:  # noqa: BLE001 - an unavailable owner cannot grant a write
                        allowed = False
                    if not allowed:
                        future.set_exception(CodexWorkerError("owner_retired"))
                        continue
                try:
                    self.process.stdin.write(frame)
                    self.process.stdin.flush()
                    future.set_result(None)
                except (OSError, ValueError):
                    future.set_exception(CodexWorkerError("disconnected"))
                    self._fail("disconnected")
                    return
        finally:
            with suppress(OSError, ValueError):
                self.process.stdin.close()

    def send(self, message, *, authorize=None):
        frame = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(frame) > MAX_FRAME_BYTES:
            raise CodexWorkerError("frame_capacity")
        if self._failure or self.process is None:
            raise CodexWorkerError(self._failure or "disconnected")
        future = Future()
        try:
            self._writes.put_nowait((frame, future, authorize))
        except queue.Full:
            raise CodexWorkerError("request_capacity") from None
        try:
            future.result(timeout=self.timeout)
        except TimeoutError:
            raise CodexWorkerError("outcome_unknown") from None

    def request(self, method, params, *, authorize=None):
        future = Future()
        with self._lock:
            if self._failure:
                raise CodexWorkerError(self._failure)
            if len(self._pending) >= 16:
                raise CodexWorkerError("request_capacity")
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = future
        try:
            self.send({"id": request_id, "method": method, "params": params}, authorize=authorize)
            return future.result(timeout=self.timeout)
        except TimeoutError:
            raise CodexWorkerError("outcome_unknown") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def event(self, timeout=0.2):
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            if self._failure:
                raise CodexWorkerError(self._failure) from None
            return None

    def close(self):
        self._fail("disconnected")
        process = self.process
        if process is None:
            return
        with suppress(queue.Full):
            self._writes.put_nowait(None)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
