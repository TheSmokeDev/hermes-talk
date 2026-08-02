"""The Realtime event loop — transport-agnostic.

:class:`RealtimeRelay` takes one decoded Realtime server event and returns the
wire messages to send back. It never touches a socket, a microphone, or
asyncio, which is what lets the same loop serve the terminal session (aiohttp
WebSocket) and, later, a browser data channel — and what lets the whole thing
be tested against a scripted transcript with no network.

Audio, captions, barge-in, and errors leave through injected callbacks. Tool
results come back as ``function_call_output`` items; a tool that fails speaks
its failure, and only an unknown tool name (a client bug) is reported as an
error string rather than a result.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import ClassVar

try:
    from .talk_tools import TalkToolError, execute_talk_tool
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    from talk_tools import TalkToolError, execute_talk_tool

BARGE_IN_MESSAGE: dict = {"type": "response.cancel"}
UNPARSEABLE_ARGS_TEXT = (
    "I couldn't parse that tool call — the arguments weren't valid JSON. "
    "Say what you wanted and I'll try again."
)


def _noop(*_args) -> None:
    """Default callback: the relay works with no consumers attached."""


class RealtimeRelay:
    """Turns Realtime server events into the messages that answer them."""

    def __init__(
        self,
        *,
        on_audio: Callable[[bytes], None] | None = None,
        on_caption: Callable[[str], None] | None = None,
        on_barge_in: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        tool_executor: Callable[[str, dict], str] | None = None,
    ) -> None:
        self.on_audio = on_audio or _noop
        self.on_caption = on_caption or _noop
        self.on_barge_in = on_barge_in or _noop
        self.on_error = on_error or _noop
        self.tool_executor = tool_executor or execute_talk_tool
        self.session_id: str | None = None
        #: Item the model is currently speaking — the CLI needs it to truncate
        #: the server-side transcript at the point playback actually reached.
        self.last_audio_item_id: str | None = None

    def handle_event(self, event: dict) -> list[dict]:
        """Handle one server event; return messages to send back."""

        handler = self._DISPATCH.get(str(event.get("type") or ""))
        if handler is None:
            return []
        return handler(self, event)

    # -- event handlers -------------------------------------------------------

    def _on_session(self, event: dict) -> list[dict]:
        session = event.get("session")
        if isinstance(session, dict) and session.get("id"):
            self.session_id = str(session["id"])
        return []

    def _on_speech_started(self, _event: dict) -> list[dict]:
        # Barge-in. The server stops generating; the callback drains whatever
        # is still queued locally, which is the part every stalled port missed.
        self.on_barge_in()
        return [dict(BARGE_IN_MESSAGE)]

    def _on_audio_delta(self, event: dict) -> list[dict]:
        item_id = event.get("item_id")
        if item_id:
            self.last_audio_item_id = str(item_id)
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            try:
                pcm = base64.b64decode(delta)
            except (ValueError, TypeError):
                self.on_error("I dropped a piece of audio there.")
                return []
            self.on_audio(pcm)
        return []

    def _on_transcript_delta(self, event: dict) -> list[dict]:
        delta = event.get("delta")
        if isinstance(delta, str) and delta:
            self.on_caption(delta)
        return []

    def _on_function_call(self, event: dict) -> list[dict]:
        call_id = str(event.get("call_id") or "")
        name = str(event.get("name") or "")
        raw = event.get("arguments")
        try:
            arguments = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
        except ValueError:
            return self._function_output(call_id, UNPARSEABLE_ARGS_TEXT)
        if not isinstance(arguments, dict):
            return self._function_output(call_id, UNPARSEABLE_ARGS_TEXT)
        try:
            output = self.tool_executor(name, arguments)
        except TalkToolError as exc:
            # Unknown tool: the model asked for something that does not exist.
            # It still gets an answer, so the turn completes instead of hanging.
            output = f"That tool isn't available: {exc}"
        return self._function_output(call_id, output)

    def _on_error(self, event: dict) -> list[dict]:
        error = event.get("error")
        detail = ""
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("type") or "")
        elif isinstance(error, str):
            detail = error
        self.on_error(
            f"Something went wrong on the call: {detail}" if detail
            else "Something went wrong on the call."
        )
        return []

    def _on_response_done(self, _event: dict) -> list[dict]:
        self.last_audio_item_id = None
        return []

    @staticmethod
    def _function_output(call_id: str, output: str) -> list[dict]:
        return [
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            },
            {"type": "response.create"},
        ]

    _DISPATCH: ClassVar[dict[str, Callable[[RealtimeRelay, dict], list[dict]]]] = {
        "session.created": _on_session,
        "session.updated": _on_session,
        "input_audio_buffer.speech_started": _on_speech_started,
        "response.output_audio.delta": _on_audio_delta,
        "response.output_audio_transcript.delta": _on_transcript_delta,
        "response.function_call_arguments.done": _on_function_call,
        "response.done": _on_response_done,
        "error": _on_error,
    }


__all__ = [
    "BARGE_IN_MESSAGE",
    "UNPARSEABLE_ARGS_TEXT",
    "RealtimeRelay",
]
