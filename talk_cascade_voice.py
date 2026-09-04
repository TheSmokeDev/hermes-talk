"""Custom-voice cascade — the provider thinks in text, ElevenLabs speaks.

Native mode lets the realtime provider synthesize its own voice. Cascade mode
opens the provider session in TEXT-output mode instead: the model keeps
listening, thinking, calling tools, and owning turn-taking, while this module
turns its assistant text deltas into PCM24k speech through the ElevenLabs
stream-input WebSocket.

Design rules, each earned:

- The playback engine is NOT forked. Cascade PCM goes into the exact sink the
  relay uses for provider ``OutputAudio`` (the CLI passes the same
  ``audio.queue_playback`` here), so barge-in drain, device handling, and the
  played-ms counter behave identically no matter which side produced bytes.
- A cancelled sentence never speaks. ``SpeechStarted`` bumps a generation,
  cancels the in-flight TTS stream task, and drains pending chunks in one
  synchronous stretch on the session loop, so no audio can be emitted after
  the operator starts talking (the emission path re-checks the generation
  before every chunk as a belt over the cancellation).
- TTS failure degrades one response to text-only, never the session. One
  stream run == one response's chunks; a failure drains that response's
  remaining chunks and logs ONE receipt, and the next response starts fresh.
- The key rides the ``xi-api-key`` header only. It is never part of the URL,
  never logged, and never written into an error string — receipt text is
  composed here, not from upstream payloads.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

try:
    from . import talk_realtime as rt
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_realtime as rt

#: Long sentences must not make the operator wait for the full stop: past
#: this many buffered characters, the next clause boundary (comma, semicolon,
#: colon, dash, newline) also ends a chunk. Terminal punctuation always ends
#: a chunk, and a split never lands mid-word.
CLAUSE_BUDGET_CHARS = 120

CONNECT_TIMEOUT_S = 15.0

#: BOS defaults from the ElevenLabs stream-input probe (2026-08-28): the
#: single-space text opens the stream, voice settings pin deterministic
#: delivery for a cloned or stock voice.
_BOS_TEXT = " "
_VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.75}

#: Bounded memory of cancelled responses — the provider keeps emitting a
#: cancelled response's text tail, and those deltas must never reach the
#: chunker. Late tail arrives within seconds, so a short ledger is enough.
_MAX_CANCELLED_RESPONSES = 32

#: Abbreviations whose trailing period is NOT a sentence end. A period that
#: splits here would chop "Dr. Smith" into two TTS generations — wrong
#: prosody, worse latency, never wrong text — so the set errs toward fewer
#: splits. Acronym tokens (only uppercase letters and dots, e.g. "U.S.A.")
#: are refused by rule rather than enumerated.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc",
        "e.g", "i.e", "a.m", "p.m", "no", "fig", "approx", "dept", "est",
    }
)
_TERMINAL_CHARS = frozenset(".!?…")
# Em/en dashes as unicode escapes so the ambiguous-glyph rule (RUF001) stays green.
_CLAUSE_BREAK_CHARS = frozenset(",;:\n") | frozenset({"\u2014", "\u2013"})

_FLUSH = object()  # queue sentinel: the response's text is complete — send EOS


class CascadeTTSError(RuntimeError):
    """The ElevenLabs leg of the cascade failed; the response degrades to text."""


def _import_aiohttp():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise CascadeTTSError(
            "aiohttp is required for the voice session — run: pip install hermes-talk"
        ) from exc
    return aiohttp


def stream_input_url(voice_id: str, model: str, *, enable_ssml: bool = True) -> str:
    """The ElevenLabs stream-input endpoint for one voice and model.

    Voice id and model are identifiers, not secrets — they ride the URL
    query/path. The KEY never does: it is an ``xi-api-key`` header.

    ``enable_ssml_parsing`` is sent EXPLICITLY because its default is the one
    thing ElevenLabs does not state: every neighbouring query parameter
    declares a default in their schema and this one declares none. Unparsed,
    a ``<break time="0.4s" />`` is not a pause — it is dropped, silently, and
    the sentence runs on. Break tags are supported on this lane's model
    (Flash v2.5) and cap at 3 seconds.
    """

    return (
        "wss://api.elevenlabs.io/v1/text-to-speech/"
        f"{quote(voice_id, safe='')}/stream-input"
        f"?model_id={quote(model, safe='')}&output_format=pcm_24000"
        f"&enable_ssml_parsing={'true' if enable_ssml else 'false'}"
    )


class SentenceChunker:
    """Turn a stream of text deltas into speakable sentence chunks.

    Splits on terminal punctuation (. ! ? …) followed by whitespace, and on
    clause breaks once the buffer is past the length budget. Decimals
    (``3.5``), abbreviations (``Dr.``), initials (``J.``), acronyms
    (``U.S.A.``), and dotted words (``example.com``) never split; an ellipsis
    is consumed as one terminal unit, never mid-dots. A terminal mark at the
    very end of the streamed buffer is held until more text (or the flush)
    proves what follows it.
    """

    def __init__(self, *, budget: int = CLAUSE_BUDGET_CHARS) -> None:
        self._budget = budget
        self._buf = ""

    def reset(self) -> None:
        """Drop any partial sentence — called on barge-in and response start."""

        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        """Fold one delta in; return any chunks that are ready to speak."""

        if not delta:
            return []
        self._buf += delta
        return self._drain(final=False)

    def flush(self) -> list[str]:
        """End of the response's text: emit everything still buffered."""

        pieces = self._drain(final=True)
        tail = self._buf.strip()
        self._buf = ""
        if tail:
            pieces.append(tail)
        return pieces

    # -- internals ------------------------------------------------------------

    def _drain(self, *, final: bool) -> list[str]:
        pieces: list[str] = []
        while True:
            cut = self._find_split(self._buf, final=final)
            if cut is None:
                return pieces
            piece = self._buf[:cut].strip()
            self._buf = self._buf[cut:].lstrip()
            if piece:
                pieces.append(piece)

    def _find_split(self, text: str, *, final: bool) -> int | None:
        """End index of the first speakable chunk, or ``None`` to keep waiting."""

        n = len(text)
        i = 0
        while i < n:
            ch = text[i]
            if ch not in _TERMINAL_CHARS:
                i += 1
                continue
            # Coalesce runs ("...", "?!", "…!") so a split never lands
            # between the dots of one ellipsis.
            end = i + 1
            while end < n and text[end] in _TERMINAL_CHARS:
                end += 1
            prev_ch = text[i - 1] if i > 0 else ""
            next_ch = text[end] if end < n else ""
            if ch == "." and prev_ch.isdigit() and next_ch.isdigit():
                i = end  # decimal point: 3.5
                continue
            if next_ch and not next_ch.isspace():
                i = end  # dotted word (example.com) or mid-token punctuation
                continue
            if ch == "." and self._is_abbreviation(text, i):
                i = end
                continue
            if not final and end == n:
                # Terminal mark at the streamed buffer's edge: the next delta
                # decides whether this is a sentence end or a decimal/abbrev
                # prefix. Hold it.
                return None
            return end
        if n > self._budget:
            for i in range(self._budget, n):
                if text[i] in _CLAUSE_BREAK_CHARS:
                    following = text[i + 1] if i + 1 < n else ""
                    if not following or following.isspace():
                        return i + 1
        return None

    @staticmethod
    def _is_abbreviation(text: str, dot: int) -> bool:
        """Whether the token ending at ``text[dot]`` is abbreviation-shaped."""

        start = dot
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        token = text[start:dot]
        stripped = token.rstrip(".").lower()
        if stripped in _ABBREVIATIONS:
            return True
        if len(token) == 1 and token.isalpha():
            return True  # an initial: "J. Robert"
        # Acronym tokens ("U.S.A", "N.A.T.O") — uppercase letters and dots.
        return bool(token) and all(c.isupper() or c == "." for c in token)


class _OwnedWs:
    """A stream-input WebSocket plus the client session that owns it."""

    def __init__(self, ws: Any, session: Any) -> None:
        self._ws = ws
        self._session = session

    async def send_json(self, payload: dict) -> None:
        await self._ws.send_json(payload)

    def __aiter__(self):
        return self._ws.__aiter__()

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


class CascadeVoice:
    """Speak assistant text deltas through ElevenLabs into the playback sink.

    Lifecycle: :meth:`start` once the session loop exists, :meth:`handle_event`
    for every provider-neutral event (synchronous, on the session loop),
    :meth:`aclose` at session teardown. ``on_audio`` is the SAME callable the
    relay uses for provider audio; ``on_error`` takes one human sentence.
    ``on_stream_end`` is the relay-lane seam: a surface that streams one
    response's PCM back to a client (the dashboard route) needs to know when
    that response's TTS run settled; the CLI and Discord lanes never subscribe.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model: str,
        on_audio: Callable[[bytes], None],
        on_error: Callable[[str], None],
        on_stream_end: Callable[[], None] | None = None,
        aiohttp_module: Any = None,
        ws_connect: Callable[..., Any] | None = None,
        chunk_budget: int = CLAUSE_BUDGET_CHARS,
        voice_settings: dict | None = None,
        enable_ssml: bool = True,
    ) -> None:
        self._api_key = api_key
        self._url = stream_input_url(voice_id, model, enable_ssml=enable_ssml)
        #: Resolved by the CALLER and copied here, never read from a module
        #: default at send time, so an operator's live edit to the pace knob
        #: takes effect on the next session rather than the next process.
        self._voice_settings = (
            dict(voice_settings) if voice_settings else dict(_VOICE_SETTINGS)
        )
        self._on_audio = on_audio
        self._on_error = on_error
        self._on_stream_end = on_stream_end
        self._aiohttp = aiohttp_module
        #: Test seam: an async callable (url, headers) -> fake socket. The real
        #: path opens an aiohttp session per stream.
        self._ws_connect = ws_connect
        self._chunker = SentenceChunker(budget=chunk_budget)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        #: Bumped on every barge-in and at teardown. Emission re-checks it so
        #: audio already decoded when the operator starts talking is dropped
        #: rather than spoken over them.
        self._generation = 0
        self._active_response_id: str | None = None
        self._cancelled_response_ids: OrderedDict[str, None] = OrderedDict()
        self._unnamed_cancelled = False
        self._saw_delta = False
        self._flushed = True
        self._closed = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Start the chunk-consuming worker on the current loop."""

        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def aclose(self) -> None:
        """Abort any in-flight stream and stop the worker. Safe to call twice."""

        if self._closed:
            return
        self._closed = True
        self._generation += 1
        stream_task = self._stream_task
        if stream_task is not None:
            stream_task.cancel()
            await asyncio.wait({stream_task})
        worker = self._worker
        if worker is not None:
            worker.cancel()
            await asyncio.wait({worker})

    # -- event intake (synchronous, on the session loop) ----------------------

    def handle_event(self, event: rt.RealtimeEvent) -> None:
        """Observe one provider event; only text-relevant ones do anything."""

        if isinstance(event, rt.ResponseStarted):
            self._start_response(event.response_id)
        elif isinstance(event, rt.SpeechStarted):
            self._abort()
        elif isinstance(event, rt.Transcript):
            self._on_transcript(event)
        elif isinstance(event, rt.ResponseFinished):
            self._finish_response(event.response_id)

    def _start_response(self, response_id: str | None) -> None:
        if response_id is not None and response_id in self._cancelled_response_ids:
            return  # a replayed start must not un-cancel a settled response
        if response_id is None and self._active_response_id is not None:
            return  # an unnamed start cannot un-name a live response
        self._active_response_id = response_id
        self._unnamed_cancelled = False
        self._chunker.reset()
        self._saw_delta = False
        self._flushed = False

    def _belongs(self, response_id: str | None) -> bool:
        """Whether text stamped ``response_id`` may still be spoken.

        Mirrors the relay's policy: ambiguity fails OPEN (an unstamped event
        is spoken rather than dropped) because muting real speech is the
        worse failure, and the barge-in path clears in-flight text regardless.
        """

        if response_id is None:
            return not self._unnamed_cancelled
        if response_id in self._cancelled_response_ids:
            return False
        return self._active_response_id is None or response_id == self._active_response_id

    def _on_transcript(self, event: rt.Transcript) -> None:
        if event.provenance is not rt.TranscriptProvenance.OUTPUT_AUDIO:
            return  # the operator's own speech is never synthesized
        if not self._belongs(event.response_id):
            return  # tail text from a cancelled or superseded response
        if event.final:
            # The final carries the response's WHOLE text; the deltas already
            # fed the chunker. Only a response that skipped deltas (a
            # non-streaming provider turn) feeds the final text itself.
            if not self._saw_delta and event.text:
                self._chunker.feed(event.text)
            self._flush()
            return
        if event.text:
            self._saw_delta = True
            for piece in self._chunker.feed(event.text):
                self._queue.put_nowait(piece)
            self._flushed = False

    def _finish_response(self, response_id: str | None) -> None:
        if (
            response_id is not None
            and self._active_response_id is not None
            and response_id != self._active_response_id
        ):
            return  # a terminal for some other response closes nothing here
        if not self._flushed:
            self._flush()  # the final transcript never arrived; speak the tail
        self._active_response_id = None
        self._unnamed_cancelled = False

    def _flush(self) -> None:
        for piece in self._chunker.flush():
            self._queue.put_nowait(piece)
        self._queue.put_nowait(_FLUSH)
        self._flushed = True

    def _abort(self) -> None:
        """Barge-in: the operator is talking; nothing in flight may speak.

        One synchronous stretch: bump the generation (so a decoded-but-unplayed
        chunk drops), cancel the stream task, drain pending chunks, and reset
        the chunker. No awaits, so nothing can interleave between the decision
        and the effect.
        """

        self._generation += 1
        task = self._stream_task
        if task is not None and not task.done():
            task.cancel()
        self._drain_queue()
        self._chunker.reset()
        self._saw_delta = False
        self._flushed = True
        if self._active_response_id is not None:
            cancelled = self._active_response_id
            self._cancelled_response_ids[cancelled] = None
            if len(self._cancelled_response_ids) > _MAX_CANCELLED_RESPONSES:
                self._cancelled_response_ids.popitem(last=False)
            self._active_response_id = None
        else:
            self._unnamed_cancelled = True

    def _drain_queue(self) -> None:
        """Drop every queued chunk up to and including the next flush sentinel.

        Anything past the sentinel belongs to a LATER response and survives —
        a barge-in cancels the interrupted answer, not the conversation.
        """

        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is _FLUSH:
                return

    # -- worker ---------------------------------------------------------------

    async def _run(self) -> None:
        """Consume chunk batches; one stream run speaks one response."""

        while True:
            item = await self._queue.get()
            if item is _FLUSH:
                continue  # a flush with no chunks behind it speaks nothing
            await self._stream_safely(item)

    async def _stream_safely(self, first_chunk: str) -> None:
        """Run one response's TTS stream; a failure degrades it to text-only.

        The receipt fires ONCE per stream run — which is once per response —
        because the failure path also drains that response's remaining chunks.
        A barge-in cancellation is not an error and earns no receipt.
        """

        task = asyncio.create_task(self._stream(first_chunk))
        self._stream_task = task
        try:
            # wait(), not await: the worker must survive the stream task's own
            # cancellation (barge-in) without being cancelled itself.
            await asyncio.wait({task})
        finally:
            self._stream_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._drain_queue()
            self._chunker.reset()
            self._on_error(
                f"cascade voice failed for that answer — staying text-only "
                f"({type(exc).__name__})"
            )
        # One stream run == one response, so a settled task means that
        # response's audio is done — in success or in failure (the error
        # receipt above already fired). A CANCELLED task signals nothing:
        # barge-in and teardown are known to whoever cancelled.
        self._notify_stream_end()

    def _notify_stream_end(self) -> None:
        """Fire the relay-lane hook; a consumer bug must not kill the worker."""

        callback = self._on_stream_end
        if callback is None:
            return
        with contextlib.suppress(Exception):
            callback()

    async def _stream(self, first_chunk: str) -> None:
        """One stream-input exchange: BOS, chunks, EOS, audio until isFinal."""

        generation = self._generation
        ws = await self._connect()
        try:
            await ws.send_json(
                {"text": _BOS_TEXT, "voice_settings": dict(self._voice_settings)}
            )
            # Deliberately NO generation trigger: forcing one per chunk
            # defeats the buffer that gives the model its prosodic context,
            # which ElevenLabs calls lower quality audio and recommends
            # against. End-of-turn generation is triggered once, by the
            # final frame in _send_rest.
            await ws.send_json({"text": first_chunk})
            sender = asyncio.create_task(self._send_rest(ws))
            try:
                await self._read_audio(ws, generation)
            finally:
                sender.cancel()
                await asyncio.wait({sender})
        finally:
            # Teardown is best-effort; a broken socket must not mask the real exit.
            with contextlib.suppress(Exception):
                await ws.close()

    async def _connect(self):
        if self._ws_connect is not None:
            return await self._ws_connect(url=self._url, headers=self._headers())
        aiohttp = self._aiohttp or _import_aiohttp()
        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(
                self._url,
                headers=self._headers(),
                timeout=CONNECT_TIMEOUT_S,
            )
        except Exception:
            await session.close()
            raise
        return _OwnedWs(ws, session)

    def _headers(self) -> dict[str, str]:
        # The key rides this header and NOTHING else — never the URL, never a
        # log line, never an error message.
        return {"xi-api-key": self._api_key}

    async def _send_rest(self, ws: Any) -> None:
        """Send the response's remaining chunks, then the EOS that ends it."""

        while True:
            item = await self._queue.get()
            if item is _FLUSH:
                # The documented CloseConnection frame, which also flushes:
                # ElevenLabs generates whatever is still buffered when the
                # context closes. That is the whole end-of-turn trigger — no
                # `flush` key rides along, because their `flush: true` is
                # documented only ON a frame carrying real text, and an
                # empty-text frame with `flush` appears nowhere in their
                # schema (SendText requires `text`). Mixing the two variants
                # would work by accident today and break on their next
                # deploy.
                await ws.send_json({"text": ""})
                return
            await ws.send_json({"text": item})

    async def _read_audio(self, ws: Any, generation: int) -> None:
        """Emit stream-input audio frames until the terminal isFinal frame."""

        aiohttp = self._aiohttp or _import_aiohttp()
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                if message.type is aiohttp.WSMsgType.ERROR:
                    raise CascadeTTSError("TTS stream reported a transport error")
                continue  # ping/pong/close frames carry no audio
            try:
                frame = json.loads(message.data)
            except (TypeError, ValueError) as exc:
                raise CascadeTTSError("TTS stream sent malformed JSON") from exc
            if not isinstance(frame, dict):
                raise CascadeTTSError("TTS stream sent a non-object frame")
            if frame.get("isFinal"):
                return
            error = frame.get("error")
            if error:
                # Detail comes from upstream and is untrusted text; the receipt
                # names only that one arrived, never its content (which could
                # quote request material back at the logs).
                raise CascadeTTSError("TTS stream reported an error frame")
            audio = frame.get("audio")
            if audio is None:
                continue  # alignment/metronome frames carry no audio
            if not isinstance(audio, str):
                raise CascadeTTSError("TTS stream sent a non-string audio field")
            try:
                pcm = base64.b64decode(audio, validate=True)
            except (binascii.Error, ValueError, TypeError) as exc:
                raise CascadeTTSError("TTS stream sent malformed audio") from exc
            # The generation check is the barge-in belt under the cancellation
            # suspenders: a frame decoded before SpeechStarted but emitted
            # after must not speak.
            if pcm and generation == self._generation:
                self._on_audio(pcm)


__all__ = [
    "CLAUSE_BUDGET_CHARS",
    "CONNECT_TIMEOUT_S",
    "CascadeTTSError",
    "CascadeVoice",
    "SentenceChunker",
    "stream_input_url",
]
