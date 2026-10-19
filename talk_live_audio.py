"""Optional aiortc media transport for GPT-Live subscription sessions.

aiortc owns RTP, Opus, jitter handling and its outbound PyAV resampling.
This adapter supplies paced mono PCM16 frames and resamples received frames
back to the existing Talk 24-kHz mono PCM contract.
"""

from __future__ import annotations

import asyncio
import contextlib
from fractions import Fraction

try:
    from . import talk_realtime as rt
    from .talk_live_transport import (
        CONNECT_TIMEOUT_S,
        LiveWebSocketTransport,
        negotiate_live_browser,
    )
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_realtime as rt
    from talk_live_transport import (
        CONNECT_TIMEOUT_S,
        LiveWebSocketTransport,
        negotiate_live_browser,
    )

PCM_RATE = 24000
FRAME_SAMPLES = 480
MAX_PENDING_PCM_BYTES = PCM_RATE * 2 * 5


def load_media_modules():
    try:
        import aiortc
        import av
    except ImportError as exc:
        raise rt.RealtimeSessionError(
            "GPT-Live subscription on terminal/Discord needs the optional media dependencies: "
            'pip install "hermes-talk[live]". API mode was not selected automatically.'
        ) from exc
    return aiortc, av


def create_pcm_audio_track(*, rtc_module=None, av_module=None):
    if rtc_module is None or av_module is None:
        rtc_module, av_module = load_media_modules()

    class QueuedAudioStreamTrack(rtc_module.AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self._pending = bytearray()
            self._pts = 0
            self._deadline = None

        def append(self, pcm: bytes) -> None:
            if self.readyState != "live":
                raise rt.RealtimeSessionError("GPT-Live audio track is closed")
            if not isinstance(pcm, bytes) or len(pcm) % 2:
                raise rt.RealtimeSessionError("GPT-Live input must be aligned PCM16 bytes")
            if len(self._pending) + len(pcm) > MAX_PENDING_PCM_BYTES:
                raise rt.RealtimeSessionError("GPT-Live input audio queue exceeded five seconds")
            self._pending.extend(pcm)

        async def recv(self):
            if self.readyState != "live":
                raise rtc_module.mediastreams.MediaStreamError
            loop = asyncio.get_running_loop()
            if self._deadline is None:
                self._deadline = loop.time()
            await asyncio.sleep(max(0, self._deadline - loop.time()))
            if self.readyState != "live":
                raise rtc_module.mediastreams.MediaStreamError
            self._deadline = max(self._deadline + 0.020, loop.time())
            count = min(FRAME_SAMPLES * 2, len(self._pending))
            data = bytes(self._pending[:count]) + bytes(FRAME_SAMPLES * 2 - count)
            del self._pending[:count]
            frame = av_module.AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
            frame.planes[0].update(data)
            frame.sample_rate = PCM_RATE
            frame.time_base = Fraction(1, PCM_RATE)
            frame.pts = self._pts
            self._pts += FRAME_SAMPLES
            return frame

        def stop(self):
            self._pending.clear()
            super().stop()

    return QueuedAudioStreamTrack()


class PcmOutputResampler:
    def __init__(self, *, av_module=None):
        if av_module is None:
            _, av_module = load_media_modules()
        self._resampler = av_module.AudioResampler(format="s16", layout="mono", rate=PCM_RATE)

    def convert(self, frame) -> tuple[bytes, ...]:
        return tuple(
            bytes(part.planes[0])[: part.samples * 2]
            for part in self._resampler.resample(frame)
            if part.samples
        )


class LiveWebRTCTransport:
    def __init__(
        self,
        *,
        auth,
        config,
        http_client=None,
        aiohttp_module=None,
        rtc_module=None,
        av_module=None,
    ):
        self.auth = auth
        self.config = config
        self._http = http_client
        self._aiohttp = aiohttp_module
        self._rtc = rtc_module
        self._av = av_module
        self._peer = None
        self._track = None
        self._sideband = None
        self._tasks = set()
        self._queue = asyncio.Queue(maxsize=256)
        self._closed = False
        self.session_id = None
        self.finalized = False
        self.final_usage = None

    def _put(self, event):
        if self._closed:
            return
        if self._queue.full():
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(rt.RealtimeSessionError("GPT-Live media event queue overflow"))
            return
        self._queue.put_nowait(event)

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def connect(self, setup):
        if self._rtc is None or self._av is None:
            self._rtc, self._av = load_media_modules()
        self._peer = self._rtc.RTCPeerConnection()
        self._track = create_pcm_audio_track(rtc_module=self._rtc, av_module=self._av)
        self._peer.addTrack(self._track)
        connected = asyncio.Event()

        @self._peer.on("connectionstatechange")
        async def on_state():
            state = self._peer.connectionState
            if state == "connected":
                connected.set()
            elif state in {"failed", "closed"} and not self._closed:
                self._put(rt.RealtimeSessionError("GPT-Live WebRTC connection failed"))
                connected.set()

        @self._peer.on("track")
        def on_track(track):
            if track.kind == "audio":
                self._spawn(self._read_audio(track))

        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_S):
                await self._peer.setLocalDescription(await self._peer.createOffer())
                negotiated = await negotiate_live_browser(
                    self._peer.localDescription.sdp,
                    setup,
                    self.auth,
                    self.config,
                    http_client=self._http,
                    aiohttp_module=self._aiohttp,
                )
                self.session_id = negotiated.session_id
                self._sideband = LiveWebSocketTransport(
                    auth=self.auth,
                    config=self.config,
                    aiohttp_module=self._aiohttp,
                )
                await self._sideband.open(
                    url=negotiated.sideband_url,
                    headers=negotiated.request_headers,
                )
                self._spawn(self._read_sideband())
                await self._peer.setRemoteDescription(
                    self._rtc.RTCSessionDescription(
                        sdp=negotiated.answer_sdp,
                        type="answer",
                    )
                )
                await connected.wait()
                if self._peer.connectionState != "connected":
                    raise rt.RealtimeSessionError("GPT-Live media failed during startup")
        except BaseException:
            await self.close()
            raise

    async def _read_sideband(self):
        try:
            async for event in self._sideband:
                if event.get("type") in {"session.output_audio.delta", "output_audio.delta"}:
                    continue
                if event.get("type") == "session.started":
                    session = event.get("session")
                    if isinstance(session, dict) and not session.get("id"):
                        event = {**event, "session": {**session, "id": self.session_id}}
                if event.get("type") == "session.closed":
                    self.finalized = True
                    self.final_usage = self._sideband.final_usage
                self._put(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - close provider resources and emit a safe failure
            self._put(rt.RealtimeSessionError("GPT-Live sideband disconnected"))

    async def _read_audio(self, track):
        converter = PcmOutputResampler(av_module=self._av)
        try:
            while not self._closed:
                frame = await track.recv()
                for data in converter.convert(frame):
                    self._put({"type": "talk.media.pcm", "data": data})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - close provider resources and emit a safe failure
            if not self._closed:
                self._put(rt.RealtimeSessionError("GPT-Live remote audio track ended"))

    async def send_audio(self, data: bytes):
        if self._track is None or self._closed:
            raise rt.RealtimeSessionError("GPT-Live media transport is closed")
        self._track.append(data)

    async def send_json(self, event: dict):
        if self._sideband is None or self._closed:
            raise rt.RealtimeSessionError("GPT-Live sideband is closed")
        await self._sideband.send_json(event)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        event = await self._queue.get()
        if isinstance(event, Exception):
            raise event
        if event is None:
            raise StopAsyncIteration
        return event

    async def close(self):
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._track is not None:
            self._track.stop()
        if self._sideband is not None:
            with contextlib.suppress(Exception):
                await self._sideband.close()
        if self._peer is not None:
            await self._peer.close()
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


__all__ = [
    "LiveWebRTCTransport",
    "PcmOutputResampler",
    "create_pcm_audio_track",
    "load_media_modules",
]
