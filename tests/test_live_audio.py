"""Local media acceptance: real aiortc/PyAV codecs, fake negotiation and media peers."""

from __future__ import annotations

import array
import asyncio
import sys
from fractions import Fraction
from types import SimpleNamespace

import httpx
import pytest
from test_live_config_protocol import fake_auth, setup_for
from test_live_transport import SDP, Socket, aiohttp_peer

import talk_live_audio as audio
import talk_realtime as rt
from talk_live_config import LiveConfig
from talk_live_realtime import LiveRealtimeSession


@pytest.fixture
def media_modules():
    return pytest.importorskip("aiortc"), pytest.importorskip("av")


def stereo_frame(av, samples=960, *, pts=0):
    frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
    values = array.array("h", [1000, 3000] * samples)
    frame.planes[0].update(values.tobytes())
    frame.sample_rate = 48000
    frame.time_base = Fraction(1, 48000)
    frame.pts = pts
    return frame


def test_missing_optional_dependency_is_actionable_without_api_fallback(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiortc", None)
    with pytest.raises(rt.RealtimeSessionError, match="optional media dependencies") as exc:
        audio.load_media_modules()
    assert "API mode was not selected automatically" in str(exc.value)


def test_queued_track_preserves_pcm_order_alignment_timestamps_and_silence(media_modules):
    async def run():
        rtc, av = media_modules
        track = audio.create_pcm_audio_track(rtc_module=rtc, av_module=av)
        pcm = array.array("h", list(range(600))).tobytes()
        track.append(pcm[:202])
        track.append(pcm[202:])
        first = await track.recv()
        second = await track.recv()
        assert first.sample_rate == 24000 and first.layout.name == "mono"
        assert first.format.name == "s16" and first.samples == 480
        assert first.time_base == Fraction(1, 24000)
        assert (first.pts, second.pts) == (0, 480)
        assert bytes(first.planes[0]) == pcm[:960]
        assert bytes(second.planes[0]) == pcm[960:] + bytes(720)
        with pytest.raises(rt.RealtimeSessionError, match="aligned"):
            track.append(b"x")
        with pytest.raises(rt.RealtimeSessionError, match="five seconds"):
            track.append(bytes(audio.MAX_PENDING_PCM_BYTES + 2))
        track.stop()
        with pytest.raises(rt.RealtimeSessionError, match="closed"):
            track.append(b"\x00\x00")
        with pytest.raises(rtc.mediastreams.MediaStreamError):
            await track.recv()

    asyncio.run(run())


def test_pyav_resamples_stereo_48k_to_mono_24k_without_plane_padding(media_modules):
    _, av = media_modules
    converter = audio.PcmOutputResampler(av_module=av)
    chunks = []
    for index in range(10):
        chunks.extend(converter.convert(stereo_frame(av, pts=index * 960)))
    chunks.extend(converter.convert(None))
    pcm = b"".join(chunks)
    assert len(pcm) == 10 * 480 * 2
    values = array.array("h")
    values.frombytes(pcm)
    assert all(1000 < sample < 4000 for sample in values[50:-50])
    assert len(set(values[50:-50])) <= 2
    assert all(len(chunk) % 2 == 0 for chunk in chunks)


def test_pcm_track_round_trips_through_aiortc_opus_and_talk_resampler(media_modules):
    async def run():
        rtc, av = media_modules
        from aiortc.codecs.opus import OpusDecoder, OpusEncoder
        from aiortc.jitterbuffer import JitterFrame

        track = audio.create_pcm_audio_track(rtc_module=rtc, av_module=av)
        track.append(array.array("h", [2000] * 4800).tobytes())
        encoder, decoder = OpusEncoder(), OpusDecoder()
        converter = audio.PcmOutputResampler(av_module=av)
        output = []
        timestamps = []
        for _ in range(10):
            frame = await track.recv()
            payloads, timestamp = encoder.encode(frame)
            if payloads:
                timestamps.append(timestamp)
            for payload in payloads:
                for decoded in decoder.decode(JitterFrame(payload, timestamp)):
                    assert decoded.sample_rate == 48000 and decoded.layout.name == "stereo"
                    output.extend(converter.convert(decoded))
        output.extend(converter.convert(None))
        pcm = b"".join(output)
        assert len(pcm) >= 8 * 480 * 2
        assert any(pcm)
        assert timestamps == list(range(0, len(timestamps) * 960, 960))
        track.stop()

    asyncio.run(run())


class FakePeer:
    def __init__(self):
        self.callbacks = {}
        self.connectionState = "new"
        self.localDescription = None
        self.track = None
        self.closed = False

    def on(self, event):
        def decorate(callback):
            self.callbacks[event] = callback
            return callback

        return decorate

    def addTrack(self, track):
        self.track = track

    async def createOffer(self):
        return SimpleNamespace(sdp=SDP, type="offer")

    async def setLocalDescription(self, description):
        self.localDescription = description

    async def setRemoteDescription(self, description):
        assert description.sdp == SDP and description.type == "answer"
        self.connectionState = "connected"
        await self.callbacks["connectionstatechange"]()

    async def close(self):
        self.closed = True
        self.connectionState = "closed"


class RemoteAudio:
    kind = "audio"

    def __init__(self):
        self.frames = asyncio.Queue()

    async def recv(self):
        return await self.frames.get()


def test_native_subscription_factory_negotiates_once_resamples_and_releases_peer(media_modules):
    async def run():
        rtc, av = media_modules
        peer = FakePeer()
        fake_rtc = SimpleNamespace(
            AudioStreamTrack=rtc.AudioStreamTrack,
            mediastreams=rtc.mediastreams,
            RTCPeerConnection=lambda: peer,
            RTCSessionDescription=lambda **values: SimpleNamespace(**values),
        )
        socket = Socket(auto_start=False)
        socket.emit({"type": "session.started", "session": {"expires_at": 9000}})
        module, client, _ = aiohttp_peer(socket)
        requests = []

        def negotiate(request):
            requests.append(request)
            return httpx.Response(201, text=SDP, headers={"openai-session-id": "rtc_test"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(negotiate)) as http:
            session = LiveRealtimeSession(
                auth=fake_auth(),
                config=LiveConfig(),
                http_client=http,
                aiohttp_module=module,
                rtc_module=fake_rtc,
                av_module=av,
            )
            await session.connect(setup_for())
            assert (await anext(session)).session_id == "rtc_test"
            assert len(requests) == 1
            assert client.calls[0][0] == "wss://api.openai.com/v1/live/rtc_test"
            for name in ("session-id", "thread-id", "x-session-id"):
                assert client.calls[0][1]["headers"][name] == requests[0].headers[name]
            assert not socket.sent
            await session.send([rt.AppendInputAudio(b"\x01\x00" * 480)])
            frame = await peer.track.recv()
            assert bytes(frame.planes[0]) == b"\x01\x00" * 480
            incoming = RemoteAudio()
            peer.callbacks["track"](incoming)
            incoming.frames.put_nowait(stereo_frame(av))
            event = await anext(session)
            assert isinstance(event, rt.OutputAudio)
            assert event.data and len(event.data) % 2 == 0
            socket.emit({"type": "output_audio.delta", "audio": "AAAAAA=="})
            socket.emit({"type": "input_transcript.added", "item": {"text": "caption"}})
            assert isinstance(await anext(session), rt.Transcript)
            await session.close()
            assert session.finalized
            assert peer.closed and socket.closed and client.closed
            assert peer.track.readyState == "ended"
            assert not session._transport._tasks

    asyncio.run(run())


def test_media_queue_overflow_is_terminal_instead_of_silent_audio_loss():
    async def run():
        wire = audio.LiveWebRTCTransport(auth=fake_auth(), config=LiveConfig())
        for _ in range(257):
            wire._put({"type": "talk.media.pcm", "data": b"\x00\x00"})
        with pytest.raises(rt.RealtimeSessionError, match="overflow"):
            await anext(wire)
        waiter = asyncio.create_task(anext(wire))
        await asyncio.sleep(0)
        await wire.close()
        with pytest.raises(StopAsyncIteration):
            await waiter

    asyncio.run(run())
