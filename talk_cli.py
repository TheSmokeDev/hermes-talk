"""``hermes talk`` — the terminal duplex voice session.

The glue layer, and the only place the three halves meet: microphone and
speaker (:mod:`talk_audio`), the Realtime WebSocket, and the event loop that
decides what to say back (:mod:`talk_relay`). Everything policy-shaped lives
in those modules; this file owns transport and lifecycle.

Two behaviours are worth stating because they are easy to get wrong:

- Instructions are assembled HERE and sent in ``session.update``. The model is
  never asked to bring its own identity.
- On barge-in the local playback queue is drained first, then the server is
  told to truncate at the millisecond the operator actually heard. Skipping
  the truncate leaves the model believing it said sentences nobody heard.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys

try:
    from . import talk_audio, talk_config, talk_host, talk_identity, talk_tools, talk_wire
    from .talk_relay import RealtimeRelay
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_audio
    import talk_config
    import talk_host
    import talk_identity
    import talk_tools
    import talk_wire
    from talk_relay import RealtimeRelay

#: How long the sender waits when the microphone queue is empty. One tenth of
#: a block: short enough that capture never falls behind, long enough that an
#: idle call is not a spin loop.
IDLE_POLL_S = 0.01
CONNECT_TIMEOUT_S = 30.0


def build_session_update(
    *, model: str, voice: str, instructions: str, tools: list[dict] | None
) -> dict:
    """The ``session.update`` message for an already-open socket.

    ``type`` and ``model`` come out of the mint payload: the model is already
    fixed by the socket URL, and the Realtime session object does not take
    either field on an update.
    """

    session = talk_wire.build_session_payload(
        model=model, voice=voice, instructions=instructions, tools=tools
    )
    return {
        "type": "session.update",
        "session": {k: v for k, v in session.items() if k not in ("type", "model")},
    }


def _import_aiohttp():
    try:
        import aiohttp
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise RuntimeError(
            "aiohttp is required for the voice session — run: pip install hermes-talk"
        ) from exc
    return aiohttp


async def run_talk_session() -> int:
    """Run one terminal voice session. Returns a process exit code."""

    try:
        api_key = talk_host.host().resolve_openai_key()
        model = talk_config.talk_model()
        voice = talk_config.talk_voice()
    except talk_config.TalkConfigError as exc:
        print(f"talk: {exc}", file=sys.stderr)
        return 1

    instructions = talk_identity.build_instructions(talk_host.host().identity_sections())
    tools = talk_tools.default_talk_tools()

    audio = talk_audio.DuplexAudio()
    try:
        audio.start()
    except talk_audio.TalkAudioError as exc:
        print(f"talk: {exc}", file=sys.stderr)
        return 1

    aiohttp = _import_aiohttp()
    pending: list[dict] = []
    spoken_item: str | None = None

    def on_barge_in() -> None:
        played = audio.played_ms
        audio.drain_playback()
        if relay.last_audio_item_id and played > 0:
            pending.append(
                {
                    "type": "conversation.item.truncate",
                    "item_id": relay.last_audio_item_id,
                    "content_index": 0,
                    "audio_end_ms": played,
                }
            )

    def on_caption(text: str) -> None:
        print(text, end="", flush=True)

    def on_error(text: str) -> None:
        print(f"\n[talk] {text}", file=sys.stderr, flush=True)

    relay = RealtimeRelay(
        on_audio=audio.queue_playback,
        on_caption=on_caption,
        on_barge_in=on_barge_in,
        on_error=on_error,
    )

    session_update = build_session_update(
        model=model, voice=voice, instructions=instructions, tools=tools
    )

    try:
        async with aiohttp.ClientSession() as http:  # noqa: SIM117 - flattening buries the socket args
            async with http.ws_connect(
                f"{talk_wire.OPENAI_REALTIME_WS_URL}?model={model}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Beta": "realtime=v1",
                },
                timeout=CONNECT_TIMEOUT_S,
                heartbeat=20.0,
            ) as ws:
                await ws.send_json(session_update)
                print(f"talk: connected ({model}, voice {voice}). Ctrl+C to hang up.\n")

                async def send_microphone() -> None:
                    while True:
                        chunk = audio.read_input_chunk()
                        if chunk is None:
                            await asyncio.sleep(IDLE_POLL_S)
                            continue
                        await ws.send_json(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(chunk).decode("ascii"),
                            }
                        )

                async def receive_events() -> None:
                    nonlocal spoken_item
                    async for message in ws:
                        if message.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            event = json.loads(message.data)
                        except ValueError:
                            continue
                        if not isinstance(event, dict):
                            continue
                        outgoing = relay.handle_event(event)
                        if pending:
                            outgoing = [*outgoing, *pending]
                            pending.clear()
                        for out in outgoing:
                            await ws.send_json(out)
                        if relay.last_audio_item_id != spoken_item:
                            spoken_item = relay.last_audio_item_id
                            audio.reset_played_ms()
                        if event.get("type") == "response.done":
                            print(flush=True)

                sender = asyncio.create_task(send_microphone())
                try:
                    await receive_events()
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — one line at the operator, not a traceback
        print(f"\ntalk: session ended: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        audio.stop()

    return 0


def setup_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes talk`` argparse tree. v0.1 takes no arguments."""

    subparser.set_defaults(talk_command="session")


def cli_entry(args: argparse.Namespace | None = None) -> int:
    """Synchronous entry point for ``hermes talk``."""

    try:
        return asyncio.run(run_talk_session())
    except KeyboardInterrupt:
        print("\ntalk: hung up.")
        return 0


__all__ = [
    "CONNECT_TIMEOUT_S",
    "IDLE_POLL_S",
    "build_session_update",
    "cli_entry",
    "run_talk_session",
    "setup_cli",
]
