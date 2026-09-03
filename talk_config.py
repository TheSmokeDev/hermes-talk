"""hermes-talk configuration — TALK_* env namespace and host paths.

Every knob is resolved at CALL time, never bound at import time, so a test
(or a live operator) can flip an env var and the very next call sees it.

One knob is more than a knob. ``TALK_AGENT_PROFILE`` decides which Hermes
profile the detached background agent runs under, and getting it wrong is not
a degradation — the spawn dies with ``Invalid length for parameter modelId,
value: 0`` because no model resolved. Hermes keeps its model config either in
the root ``config.yaml`` or in a profile under ``<home>/profiles/<name>/``,
and an install whose model lives only in a profile CANNOT run a bare
``hermes -z``. :func:`agent_profile` therefore auto-detects rather than
requiring a knob nobody knows to set — see :func:`detect_agent_profile` for
the rule and its deliberate limits.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_TALK_MODEL = "gpt-realtime-2.1"
DEFAULT_TALK_VOICE = "cedar"
DEFAULT_AGENT_TIMEOUT_S = 1_800

#: Realtime voice providers selectable through ``TALK_PROVIDER``. The list is
#: fail-closed on purpose: a provider knob that guesses silently would spend
#: the wrong metered key, which is a billing error, not a UX nicety.
TALK_PROVIDERS = ("openai", "grok", "gemini")
DEFAULT_TALK_PROVIDER = "openai"
#: xAI Grok Voice model alias, handshake-verified against the live endpoint
#: 2026-08-28 (the alias resolved to a concrete voice model at session
#: create). The model rides the socket URL query, never the session update.
DEFAULT_GROK_MODEL = "grok-voice-latest"
DEFAULT_GROK_VOICE = "ara"
#: Friendly Grok voice names, WITHOUT the wire prefix — the adapter adds
#: ``xai_`` at encode time so operators never configure wire vocabulary.
GROK_REALTIME_VOICES = ("ara", "rex", "sal", "eve", "leo")
#: Gemini Live native-audio preview model, probed against the live endpoint
#: 2026-08-28 (setup accepted, tool loop round-tripped). The adapter adds the
#: ``models/`` wire prefix; operators configure the bare id. Fallback line:
#: ``gemini-2.5-flash-native-audio-latest``.
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_GEMINI_VOICE = "Puck"
#: Gemini Live prebuilt voices are CASE-SENSITIVE on the wire per Google's
#: docs, so this list keeps the canonical casing and :func:`talk_gemini_voice`
#: refuses to case-fold: silently "fixing" ``puck`` would guess at a wire
#: value the API may reject.
GEMINI_LIVE_VOICES = ("Puck", "Charon", "Kore", "Fenrir", "Aoede")

#: Voice modes selectable through ``TALK_VOICE_MODE``. ``native`` is today's
#: behaviour — the provider synthesizes its own voice. ``cascade`` keeps the
#: provider as the brain and hands speech synthesis to a streaming TTS the
#: operator chooses. Fail-closed like the provider list: a mode knob that
#: guesses silently would spend the wrong metered TTS key.
TALK_VOICE_MODES = ("native", "cascade")
DEFAULT_VOICE_MODE = "native"
#: Cascade TTS providers selectable through ``TALK_CASCADE_TTS``. One value
#: today; the list exists so a typo refuses instead of silently selecting.
TALK_CASCADE_TTS_PROVIDERS = ("elevenlabs",)
DEFAULT_CASCADE_TTS = "elevenlabs"
#: ElevenLabs TTS model for the cascade lane, probed against the live
#: stream-input endpoint 2026-08-28 (first audio ~490ms, PCM 24kHz out).
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"

#: Where Hermes's api_server gateway platform listens by default
#: (gateway/platforms/api_server.py DEFAULT_HOST/DEFAULT_PORT).
DEFAULT_API_SERVER_URL = "http://127.0.0.1:8642"
#: Availability probe budget. Tight on purpose: this runs inside a tool call,
#: and a tool call runs on the same event loop as the microphone.
DEFAULT_API_SERVER_PROBE_TIMEOUT_S = 1.5
#: How long a probe verdict is trusted. A stale-but-present verdict is served
#: immediately and refreshed off the hot path, so only a COLD probe can wait.
DEFAULT_API_SERVER_PROBE_TTL_S = 30.0
#: Poll interval while waiting on a /v1/runs run inside a worker thread.
DEFAULT_API_SERVER_POLL_S = 1.0
#: How long a resolved capability catalog is trusted. Same 30s class as the
#: probe verdict above, and for the same reason: what a Hermes install has
#: installed changes on the timescale of a restart, not of a sentence.
DEFAULT_CAPABILITY_CATALOG_TTL_S = 30.0
DEFAULT_CATALOG_STARTUP_WAIT_S = 2.5
#: Hard wait bound for one in-process remembered-context (Honcho) lookup.
#: Long enough for a slow index, short enough that a wedged plugin cannot
#: hold the serialized tool pipeline for the life of the call.
DEFAULT_MEMORY_SEARCH_TIMEOUT_S = 10.0
#: How long a spoken approval stays live. The window runs from the moment
#: the operator's approving speech ended — never from permit mint — sized
#: to one spoken exchange: long enough to act on a fresh yes, short enough
#: that a stale yes cannot fire into a conversation that has moved on.
DEFAULT_APPROVAL_PERMIT_TTL_S = 30.0
#: How long a spoken approval PROMPT stays open for an answer before the
#: bridge denies it (fail closed). Sized well under the host's own approval
#: wait (300s by default) so the voice lane's deny lands first and the run
#: unwinds on the operator's answer-or-silence, not on a host timer nobody
#: on the call can hear.
DEFAULT_APPROVAL_PROMPT_TIMEOUT_S = 60.0
OPENAI_REALTIME_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)
MODEL_COMPATIBILITY_POLICY_VERSION = 1
# Exact aliases/snapshots whose published contracts include audio input,
# audio output, and function calling: all three are required by Talk.
DUPLEX_TOOL_COMPATIBLE_MODELS = frozenset(
    {
        "gpt-realtime-2.1",
        "gpt-realtime-2.1-mini",
        "gpt-realtime-2",
        "gpt-realtime-1.5",
        "gpt-realtime",
        "gpt-realtime-mini",
        "gpt-realtime-mini-2025-10-06",
        "gpt-realtime-mini-2025-12-15",
        "gpt-4o-realtime-preview",
        "gpt-4o-realtime-preview-2024-10-01",
        "gpt-4o-realtime-preview-2024-12-17",
        "gpt-4o-realtime-preview-2025-06-03",
        "gpt-4o-mini-realtime-preview",
        "gpt-4o-mini-realtime-preview-2024-12-17",
    }
)
KNOWN_INCOMPATIBLE_REALTIME_MODELS = frozenset(
    {
        # Streaming transcription: audio input only, no function calling.
        "gpt-realtime-whisper",
        # Dedicated translation: no function calling.
        "gpt-realtime-translate",
    }
)
_REALTIME_MODEL_SYNTAX_RE = re.compile(
    r"^(?:gpt-realtime(?:-[A-Za-z0-9.]+)*|gpt-4o-realtime-preview(?:-[A-Za-z0-9.-]+)?)$"
)

# Discord snowflakes are unsigned 64-bit decimal integers. Treating the whole
# list as one configuration unit is intentional: a typo must narrow authority
# to nobody, never silently leave the other entries active.
_MAX_DISCORD_USER_ID = (1 << 64) - 1


class TalkConfigError(Exception):
    """A Talk configuration value is unusable."""


def get_hermes_home() -> Path:
    """Hermes home, preferring the host's own resolver when importable."""

    try:
        from hermes_constants import get_hermes_home as _host_home

        return Path(_host_home())
    except Exception:  # noqa: BLE001 - any host failure falls back to the env
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path.home() / ".hermes"


def state_dir() -> Path:
    """Where hermes-talk keeps durable state (run history, flush dedup)."""

    path = get_hermes_home() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


#: A Hermes config is only usable for a headless run if it names a default
#: model. Matched at column 0 (top-level ``model:``) then within its indented
#: block, so a commented-out example or a nested ``model:`` under some other
#: key cannot be mistaken for the real thing.
_MODEL_KEY_RE = re.compile(r"^model:\s*(.*)$")
_MODEL_DEFAULT_RE = re.compile(r"^\s+default:\s*(\S.*)$")
_CONFIG_SCAN_MAX_BYTES = 256_000


#: Hermes's own per-section identity budgets, read from the SAME keys the text
#: agent honors (``memory.memory_char_limit`` / ``memory.user_char_limit``).
#: Matched at column 0 then inside the indented block, exactly like
#: :data:`_MODEL_KEY_RE` — a ``memory_char_limit`` nested under some other key
#: is not this key.
_MEMORY_SECTION_RE = re.compile(r"^memory:\s*$")
_TOP_LEVEL_KEY_RE = re.compile(r"^\S")


def identity_char_limit(key: str) -> int:
    """The host's own budget for one identity section, or ``0`` for unset.

    ``0`` means "no host opinion" and the caller applies its own cap; it never
    means "emit nothing". A text scan rather than a YAML parse for the reason
    :func:`_has_model_default` gives — PyYAML is not a dependency of this
    plugin, and being wrong here costs a section trimmed at the plugin's own
    cap instead of the host's, which :mod:`talk_identity` enforces anyway.
    """

    pattern = re.compile(r"^\s+" + re.escape(key) + r":\s*(\d+)\s*$")
    try:
        text = (get_hermes_home() / "config.yaml").read_text(
            encoding="utf-8", errors="replace"
        )[:_CONFIG_SCAN_MAX_BYTES]
    except OSError:
        return 0

    in_block = False
    for line in text.splitlines():
        if in_block:
            match = pattern.match(line)
            if match:
                try:
                    return max(0, int(match.group(1)))
                except ValueError:  # pragma: no cover - the regex proves digits
                    return 0
            if _TOP_LEVEL_KEY_RE.match(line):
                break  # left the memory block without finding the key
        elif _MEMORY_SECTION_RE.match(line):
            in_block = True
    return 0


def _has_model_default(config_path: Path) -> bool:
    """True when ``config_path`` names ``model.default``.

    A deliberately small text scan, not a YAML parse: PyYAML is not a
    dependency of this plugin and will not become one to answer a yes/no
    question about a single key. Being wrong here costs a missing (or
    unnecessary) ``--profile`` flag, and the run's own error text says so.
    """

    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")[
            :_CONFIG_SCAN_MAX_BYTES
        ]
    except OSError:
        return False

    in_block = False
    for line in text.splitlines():
        if not in_block:
            match = _MODEL_KEY_RE.match(line)
            if match:
                inline = match.group(1).strip()
                if inline and inline not in ("{}", "null", "~"):
                    # Inline mapping — ``model: {default: gpt-5.5}``.
                    return "default:" in inline
                in_block = True
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[:1].isspace():
            break  # dedent — the model block ended without a default
        if _MODEL_DEFAULT_RE.match(line):
            return True
    return False


def detect_agent_profile() -> str | None:
    """The profile a detached ``hermes -z`` needs here, or ``None``.

    The rule, and it is deliberately timid:

    - Root ``config.yaml`` names a model → ``None``. A bare invocation works
      and adding a flag could only change behavior the operator did not ask
      to change.
    - Root does not, and EXACTLY ONE profile does → that profile.
    - Zero candidates, or two or more → ``None``. Picking one of several
      profiles would be guessing at which agent the operator meant, and the
      guess is invisible until the wrong agent has already run. The bare
      spawn's own failure ("Invalid length for parameter modelId") names the
      problem better than a silent wrong choice would.

    Note ``HERMES_HOME`` may itself point AT a profile directory, in which
    case there is no ``profiles/`` beneath it, no candidates are found, and
    no flag is added — correct, since that home is already the profile.
    """

    try:
        home = get_hermes_home()
        if _has_model_default(home / "config.yaml"):
            return None
        profiles_dir = home / "profiles"
        if not profiles_dir.is_dir():
            return None
        candidates = sorted(
            entry.name
            for entry in profiles_dir.iterdir()
            if entry.is_dir() and _has_model_default(entry / "config.yaml")
        )
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def agent_profile() -> str | None:
    """Hermes profile for the detached background agent, or ``None``.

    ``TALK_AGENT_PROFILE`` wins when set. Set-but-BLANK is an explicit opt
    out — it suppresses detection and forces the bare invocation — so an
    operator whose auto-detect guesses wrong has a way to say "no flag"
    without editing any config.
    """

    raw = os.environ.get("TALK_AGENT_PROFILE")
    if raw is not None:
        return raw.strip() or None
    return detect_agent_profile()


def identity_include() -> tuple[str, ...] | None:
    """Which identity sections may ride the voice prompt, or ``None`` for all.

    ``TALK_IDENTITY_INCLUDE`` is a comma-separated list of section names
    (case-insensitive). Unset or blank means every section the host can
    resolve.

    **The trap:** this list REPLACES the default, it does not extend it.
    ``TALK_IDENTITY_INCLUDE=MEMORY`` means memory AND NOTHING ELSE — the
    persona section stops travelling, and the only visible symptom is a voice
    session that has quietly stopped knowing who it is talking to.

    **The upgrade trap is the same trap, aged:** a list pinned before a
    section existed (e.g. ``MEMORY,PERSONA`` from before ``WORKING``) keeps
    working verbatim and silently drops the new section forever.
    ``HostAdapter.identity_sections`` logs one warning per session mint when
    a pinned list lacks ``WORKING``.

    Unknown names are dropped rather than raising: a typo in a knob must
    narrow the prompt, never take the voice surface down with it.
    """

    raw = (os.environ.get("TALK_IDENTITY_INCLUDE") or "").strip()
    if not raw:
        return None
    names = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return names or None


def session_key() -> str | None:
    """Stable key scoping api-server runs across ``/clear``, or ``None``.

    ``TALK_SESSION_KEY`` is operator-set and static, the same shape as
    ``TALK_AGENT_PROFILE``; unset or blank sends no header at all, which is
    exactly today's behaviour.

    **Never derive a default.** A key computed from the hostname, the PID, or
    the clock would change between runs, so the one property the knob exists
    to provide — the same scope before and after a ``/clear`` — would be
    silently absent, and the operator would have no symptom to read it off.
    Deriving a distinct key per Discord channel or dashboard session is a
    real feature, and a separate one: it needs channel identity plumbed in
    from the surfaces, against a host header contract this repo cannot see.

    **This key is an operator scope, NOT a session boundary — and every
    voice-channel participant shares it.** In a Discord voice channel any
    participant can drive a memory lookup, and the run it starts reads and
    writes under THIS key: the operator-authority ledger gates mutating
    tools, never memory reads (``talk_operator_auth.READ_ONLY_TALK_TOOLS``),
    and speaker identity does not reach the dispatch point (the relay's tool
    executor receives only the tool name and the model's arguments). Do not
    set this in a multi-user channel unless you accept that everyone present
    can read from — and via delegated runs, write into — your memory scope;
    per-speaker scoping is a follow-up, not shipped.
    """

    return (os.environ.get("TALK_SESSION_KEY") or "").strip() or None


def talk_provider() -> str:
    """Realtime voice provider, resolved at call time. Fail-closed.

    ``TALK_PROVIDER`` names the provider explicitly; it is NEVER inferred
    from which API keys happen to be set. An operator holding both keys who
    mistypes the knob gets an error naming the valid values, not a silent
    switch to a different metered account.
    """

    raw = (
        (os.environ.get("TALK_PROVIDER") or DEFAULT_TALK_PROVIDER).strip().lower()
        or DEFAULT_TALK_PROVIDER
    )
    if raw not in TALK_PROVIDERS:
        raise TalkConfigError(
            f"TALK_PROVIDER '{raw}' is not a realtime voice provider "
            f"({', '.join(TALK_PROVIDERS)})"
        )
    return raw


def talk_model() -> str:
    """Realtime model, resolved at call time."""

    return (os.environ.get("TALK_MODEL") or DEFAULT_TALK_MODEL).strip() or DEFAULT_TALK_MODEL


def talk_grok_model() -> str:
    """Grok realtime model, resolved at call time (``TALK_GROK_MODEL``)."""

    return (os.environ.get("TALK_GROK_MODEL") or DEFAULT_GROK_MODEL).strip() or DEFAULT_GROK_MODEL


def talk_grok_voice() -> str:
    """Grok realtime voice, fail-closed on unknown ids (``TALK_GROK_VOICE``)."""

    raw = (os.environ.get("TALK_GROK_VOICE") or DEFAULT_GROK_VOICE).strip().lower()
    raw = raw or DEFAULT_GROK_VOICE
    if raw not in GROK_REALTIME_VOICES:
        raise TalkConfigError(
            f"TALK_GROK_VOICE '{raw}' is not a Grok voice "
            f"({', '.join(GROK_REALTIME_VOICES)})"
        )
    return raw


def talk_gemini_model() -> str:
    """Gemini Live model, resolved at call time (``TALK_GEMINI_MODEL``)."""

    return (
        (os.environ.get("TALK_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
        or DEFAULT_GEMINI_MODEL
    )


def talk_gemini_voice() -> str:
    """Gemini Live voice, fail-closed on unknown ids (``TALK_GEMINI_VOICE``).

    Unlike the other voice knobs this one does NOT case-fold: Live voice
    names are case-sensitive on the wire, so a lowercase typo must refuse
    with the canonical names rather than guess at a casing the API may not
    accept.
    """

    raw = (os.environ.get("TALK_GEMINI_VOICE") or DEFAULT_GEMINI_VOICE).strip()
    raw = raw or DEFAULT_GEMINI_VOICE
    if raw not in GEMINI_LIVE_VOICES:
        raise TalkConfigError(
            f"TALK_GEMINI_VOICE '{raw}' is not a Gemini Live voice "
            f"({', '.join(GEMINI_LIVE_VOICES)}; case-sensitive)"
        )
    return raw


def realtime_model_compatibility(model: str) -> str:
    """Return ``compatible``, ``incompatible``, or honest syntax-only ``unknown``.

    This is a bounded local policy, not a provider availability probe. Only
    exact ids with a published duplex-audio and function-calling contract are
    certified compatible. Realtime-shaped ids outside that set remain usable
    at runtime but are reported as unknown by diagnostics.
    """

    candidate = model.strip()
    if candidate in DUPLEX_TOOL_COMPATIBLE_MODELS:
        return "compatible"
    if candidate in KNOWN_INCOMPATIBLE_REALTIME_MODELS:
        return "incompatible"
    if _REALTIME_MODEL_SYNTAX_RE.fullmatch(candidate):
        return "unknown"
    return "incompatible"


def realtime_model_valid(model: str) -> bool:
    """Backward-compatible boolean for the bounded compatibility policy."""

    return realtime_model_compatibility(model) == "compatible"


def talk_voice() -> str:
    """Realtime voice, fail-closed on unknown ids."""

    raw = (os.environ.get("TALK_VOICE") or DEFAULT_TALK_VOICE).strip().lower() or DEFAULT_TALK_VOICE
    if raw not in OPENAI_REALTIME_VOICES:
        raise TalkConfigError(
            f"TALK_VOICE '{raw}' is not a built-in Realtime voice "
            f"({', '.join(OPENAI_REALTIME_VOICES)})"
        )
    return raw


def resolve_openai_key() -> str:
    """The OpenAI Platform key for Talk. Fail-closed, never silent.

    Order: TALK_OPENAI_API_KEY (Talk-scoped) -> OPENAI_API_KEY. A key that is
    SET but blank is a hard refusal, not a fall-through — an operator who
    scoped a key expects that key to be used or the surface to say why not.
    """

    scoped = os.environ.get("TALK_OPENAI_API_KEY")
    if scoped is not None:
        if not scoped.strip():
            raise TalkConfigError(
                "TALK_OPENAI_API_KEY is set but empty — set a real key or unset it"
            )
        return scoped.strip()
    shared = os.environ.get("OPENAI_API_KEY")
    if shared is not None:
        if not shared.strip():
            raise TalkConfigError("OPENAI_API_KEY is set but empty — set a real key or unset it")
        return shared.strip()
    raise TalkConfigError(
        "no OpenAI key for Talk: set TALK_OPENAI_API_KEY or OPENAI_API_KEY"
    )


def resolve_xai_key() -> str:
    """The xAI key for the Grok provider. Fail-closed, never silent.

    Order: TALK_XAI_API_KEY (Talk-scoped) -> XAI_API_KEY. Same rule as
    :func:`resolve_openai_key`: a key that is SET but blank is a hard
    refusal, not a fall-through. This is the metered-key half of Grok auth;
    the subscription lane lives in ``talk_grok_auth.resolve_grok_auth``.
    """

    scoped = os.environ.get("TALK_XAI_API_KEY")
    if scoped is not None:
        if not scoped.strip():
            raise TalkConfigError(
                "TALK_XAI_API_KEY is set but empty — set a real key or unset it"
            )
        return scoped.strip()
    shared = os.environ.get("XAI_API_KEY")
    if shared is not None:
        if not shared.strip():
            raise TalkConfigError("XAI_API_KEY is set but empty — set a real key or unset it")
        return shared.strip()
    raise TalkConfigError("no xAI key for Talk: set TALK_XAI_API_KEY or XAI_API_KEY")


def resolve_gemini_key() -> str:
    """The Gemini API key for the Gemini Live provider. Fail-closed, never silent.

    Order: TALK_GEMINI_API_KEY (Talk-scoped) -> GEMINI_API_KEY. Same rule as
    :func:`resolve_openai_key`: a key that is SET but blank is a hard
    refusal, not a fall-through. On this lane the key rides the socket URL
    query, so it is treated as a URL-embedded secret end to end — never
    logged, and scrubbed out of transport errors before they surface.
    """

    scoped = os.environ.get("TALK_GEMINI_API_KEY")
    if scoped is not None:
        if not scoped.strip():
            raise TalkConfigError(
                "TALK_GEMINI_API_KEY is set but empty — set a real key or unset it"
            )
        return scoped.strip()
    shared = os.environ.get("GEMINI_API_KEY")
    if shared is not None:
        if not shared.strip():
            raise TalkConfigError("GEMINI_API_KEY is set but empty — set a real key or unset it")
        return shared.strip()
    raise TalkConfigError(
        "no Gemini key for Talk: set TALK_GEMINI_API_KEY or GEMINI_API_KEY"
    )


def voice_mode() -> str:
    """Voice synthesis mode, resolved at call time. Fail-closed.

    ``TALK_VOICE_MODE`` = ``native`` (default; the provider synthesizes its
    own voice, exactly the pre-cascade behaviour) or ``cascade`` (the
    provider thinks in text, a streaming TTS speaks). Any other value
    refuses with the valid names rather than silently picking one — a
    misread mode would spend the wrong metered key or mute the call.
    """

    raw = (
        (os.environ.get("TALK_VOICE_MODE") or DEFAULT_VOICE_MODE).strip().lower()
        or DEFAULT_VOICE_MODE
    )
    if raw not in TALK_VOICE_MODES:
        raise TalkConfigError(
            f"TALK_VOICE_MODE '{raw}' is not a voice mode "
            f"({', '.join(TALK_VOICE_MODES)})"
        )
    return raw


def cascade_tts() -> str:
    """Cascade TTS provider, resolved at call time. Fail-closed.

    Only ``elevenlabs`` exists today; the knob is still validated so a typo
    refuses with the valid values instead of guessing at a provider (and its
    metered key) the operator did not name.
    """

    raw = (
        (os.environ.get("TALK_CASCADE_TTS") or DEFAULT_CASCADE_TTS).strip().lower()
        or DEFAULT_CASCADE_TTS
    )
    if raw not in TALK_CASCADE_TTS_PROVIDERS:
        raise TalkConfigError(
            f"TALK_CASCADE_TTS '{raw}' is not a cascade TTS provider "
            f"({', '.join(TALK_CASCADE_TTS_PROVIDERS)})"
        )
    return raw


def resolve_elevenlabs_key() -> str:
    """The ElevenLabs key for the cascade TTS leg. Fail-closed, never silent.

    Order: TALK_ELEVENLABS_API_KEY (Talk-scoped) -> ELEVENLABS_API_KEY. Same
    rule as :func:`resolve_openai_key`: a key that is SET but blank is a
    hard refusal, not a fall-through. On this lane the key rides the
    ``xi-api-key`` WebSocket header — never the URL, never a log line.
    """

    scoped = os.environ.get("TALK_ELEVENLABS_API_KEY")
    if scoped is not None:
        if not scoped.strip():
            raise TalkConfigError(
                "TALK_ELEVENLABS_API_KEY is set but empty — set a real key or unset it"
            )
        return scoped.strip()
    shared = os.environ.get("ELEVENLABS_API_KEY")
    if shared is not None:
        if not shared.strip():
            raise TalkConfigError(
                "ELEVENLABS_API_KEY is set but empty — set a real key or unset it"
            )
        return shared.strip()
    raise TalkConfigError(
        "no ElevenLabs key for Talk: set TALK_ELEVENLABS_API_KEY or ELEVENLABS_API_KEY"
    )


def elevenlabs_voice_id() -> str:
    """The ElevenLabs voice the cascade speaks with. REQUIRED in cascade mode.

    Callers invoke this only after :func:`voice_mode` returned ``cascade``,
    so unset is never a silent default here — a cascade with no voice id
    would have nothing to synthesize against, and guessing at an account's
    voices would speak with a voice the operator did not choose. The id is
    an identifier, not a secret; the KEY is the secret.
    """

    raw = (os.environ.get("TALK_ELEVENLABS_VOICE_ID") or "").strip()
    if not raw:
        raise TalkConfigError(
            "TALK_VOICE_MODE=cascade needs TALK_ELEVENLABS_VOICE_ID — set it to a "
            "voice id from your ElevenLabs account (VoiceLab -> your voice -> ID)"
        )
    return raw


def elevenlabs_model() -> str:
    """ElevenLabs TTS model for the cascade lane (``TALK_ELEVENLABS_MODEL``)."""

    return (
        (os.environ.get("TALK_ELEVENLABS_MODEL") or DEFAULT_ELEVENLABS_MODEL).strip()
        or DEFAULT_ELEVENLABS_MODEL
    )


def cascade_voice_config(provider: str) -> tuple[str, str, str]:
    """The resolved cascade TTS triple — (key, voice id, model). Fail-closed.

    Every lane that opens a cascade session resolves through HERE so the
    refusal rules — and their messages — exist exactly once: cascade requires
    the openai provider (its text-output mode is the only one wired and
    verified; guessing at grok/gemini text modes would mute the call), the TTS
    knob validates, the key refuses set-but-blank, and the voice id is
    required. Callers invoke this only after :func:`voice_mode` returned
    ``cascade``, before a single secret or socket is spent.
    """

    if provider != "openai":
        raise TalkConfigError(
            f"TALK_VOICE_MODE=cascade requires TALK_PROVIDER=openai, but "
            f"'{provider}' is configured — grok/gemini text-output modes are "
            "not wired into the cascade yet"
        )
    cascade_tts()  # fail-closed; elevenlabs is the only value today
    return (
        resolve_elevenlabs_key(),
        elevenlabs_voice_id(),
        elevenlabs_model(),
    )


def agent_timeout_s() -> int:
    """Wall-clock budget for one detached background agent run.

    Bounds both the child process and the watcher that speaks its result, so
    a wedged agent cannot leave a poll loop running for the whole session.
    """

    raw = (os.environ.get("TALK_AGENT_TIMEOUT_S") or "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_AGENT_TIMEOUT_S


def _positive_float(name: str, default: float) -> float:
    """A positive float knob. Junk and non-positive values take the default."""

    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            return default
        if parsed > 0:
            return parsed
    return default


def api_server_url() -> str:
    """Base URL of the Hermes api_server gateway platform, no trailing slash."""

    raw = (os.environ.get("TALK_API_SERVER_URL") or "").strip()
    return (raw or DEFAULT_API_SERVER_URL).rstrip("/")


def api_server_key() -> str | None:
    """Bearer key for the api_server, or ``None`` to send no Authorization.

    ``TALK_API_SERVER_KEY`` wins; unset falls back to ``API_SERVER_KEY``, the
    same variable the gateway itself reads (gateway/config.py:1618), so an
    operator who configured the api_server does not configure it twice.

    Set-but-BLANK is an explicit opt out — send no key, do not fall through —
    matching ``TALK_AGENT_PROFILE``. The api_server does accept unauthenticated
    requests when it holds no key of its own (``_check_auth`` returns early),
    so "no key" is a real, reachable configuration rather than a broken one.
    """

    scoped = os.environ.get("TALK_API_SERVER_KEY")
    if scoped is not None:
        return scoped.strip() or None
    return (os.environ.get("API_SERVER_KEY") or "").strip() or None


def api_server_probe_timeout_s() -> float:
    """Budget for one availability probe."""

    return _positive_float(
        "TALK_API_SERVER_PROBE_TIMEOUT_S", DEFAULT_API_SERVER_PROBE_TIMEOUT_S
    )


def api_server_probe_ttl_s() -> float:
    """How long a probe verdict is trusted before it is refreshed."""

    return _positive_float("TALK_API_SERVER_PROBE_TTL_S", DEFAULT_API_SERVER_PROBE_TTL_S)


def api_server_poll_s() -> float:
    """Poll interval while a worker thread waits on an api_server run."""

    return _positive_float("TALK_API_SERVER_POLL_S", DEFAULT_API_SERVER_POLL_S)


def capability_catalog_ttl_s() -> float:
    """How long a resolved capability catalog snapshot is trusted."""

    return _positive_float(
        "TALK_CAPABILITY_CATALOG_TTL_S", DEFAULT_CAPABILITY_CATALOG_TTL_S
    )


def catalog_startup_wait_s() -> float:
    """How long a session start may wait for the FIRST catalog read.

    Bounds the head start that makes the live-catalog prompt section
    deterministic on a cold process. ``0`` is honored and disables the wait
    entirely (the pre-#F8 fire-and-forget behavior); junk and negative
    values take the default. On expiry the session starts exactly as before
    — section omitted, never a stall.
    """

    raw = (os.environ.get("TALK_CATALOG_STARTUP_WAIT_S") or "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            return DEFAULT_CATALOG_STARTUP_WAIT_S
        if parsed >= 0:
            return parsed
    return DEFAULT_CATALOG_STARTUP_WAIT_S


def memory_search_timeout_s() -> float:
    """Wait bound for the in-process remembered-context (Honcho) lookup.

    Only the Honcho tier of ``search_memory`` is bounded: it is a
    network-backed plugin dispatched from inside the serialized tool
    pipeline, where an unbounded wait holds every later tool call hostage.
    ``session_search`` stays unbounded — it is a local FTS5 read.
    """

    return _positive_float(
        "TALK_MEMORY_SEARCH_TIMEOUT_S", DEFAULT_MEMORY_SEARCH_TIMEOUT_S
    )


def audio_input_device() -> str | None:
    """Optional sounddevice input override (Windows/WASAPI proofing)."""

    raw = (os.environ.get("TALK_INPUT_DEVICE") or "").strip()
    return raw or None


def audio_output_device() -> str | None:
    """Optional sounddevice output override."""

    raw = (os.environ.get("TALK_OUTPUT_DEVICE") or "").strip()
    return raw or None


def discord_operator_user_ids() -> frozenset[int]:
    """Immutable Discord user IDs allowed to run state-changing Talk tools.

    Unset, blank, or any malformed comma-separated entry returns an empty set.
    A partially valid list never partially authorizes.
    """

    raw = os.environ.get("TALK_DISCORD_OPERATOR_USER_IDS")
    if raw is None or not raw.strip():
        return frozenset()
    resolved: set[int] = set()
    for part in raw.split(","):
        candidate = part.strip()
        if (
            not candidate
            or len(candidate) > 20
            or not candidate.isascii()
            or not candidate.isdecimal()
        ):
            return frozenset()
        try:
            user_id = int(candidate)
        except ValueError:
            return frozenset()
        if user_id <= 0 or user_id > _MAX_DISCORD_USER_ID:
            return frozenset()
        resolved.add(user_id)
    return frozenset(resolved)


def approval_permit_ttl_s() -> float:
    """How long a spoken approval stays live, measured on the monotonic
    clock from the moment the approving speech ended. Junk or non-positive
    overrides take the default; a permit is never valid forever.
    """

    return _positive_float("TALK_APPROVAL_PERMIT_TTL_S", DEFAULT_APPROVAL_PERMIT_TTL_S)


def approval_prompt_timeout_s() -> float:
    """How long a spoken approval prompt stays open before it is denied.

    Fail closed: an unanswered approval question on a live call resolves as
    deny after this window — silence is not consent. Junk or non-positive
    overrides take the default.
    """

    return _positive_float(
        "TALK_APPROVAL_PROMPT_TIMEOUT_S", DEFAULT_APPROVAL_PROMPT_TIMEOUT_S
    )


def trust_declared_read_only() -> bool:
    """Whether a model's ``parallel_read_only`` declaration is believed.

    Default **off** (hermes-talk#101): the declaration is the delegating
    model's own claim about work it has not done yet — policy input, not a
    sandbox — so by default every delegated run is treated as ``exclusive``
    and two runs sharing a resource key never overlap. Only an explicit
    ``TALK_TRUST_DECLARED_READ_ONLY=true`` lets read-only runs on a shared
    key run together; anything else, junk included, keeps the fence.
    """

    raw = (os.environ.get("TALK_TRUST_DECLARED_READ_ONLY") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


__all__ = [
    "DEFAULT_AGENT_TIMEOUT_S",
    "DEFAULT_API_SERVER_POLL_S",
    "DEFAULT_API_SERVER_PROBE_TIMEOUT_S",
    "DEFAULT_API_SERVER_PROBE_TTL_S",
    "DEFAULT_API_SERVER_URL",
    "DEFAULT_APPROVAL_PERMIT_TTL_S",
    "DEFAULT_APPROVAL_PROMPT_TIMEOUT_S",
    "DEFAULT_CAPABILITY_CATALOG_TTL_S",
    "DEFAULT_CASCADE_TTS",
    "DEFAULT_CATALOG_STARTUP_WAIT_S",
    "DEFAULT_ELEVENLABS_MODEL",
    "DEFAULT_GEMINI_MODEL",
    "DEFAULT_GEMINI_VOICE",
    "DEFAULT_GROK_MODEL",
    "DEFAULT_GROK_VOICE",
    "DEFAULT_MEMORY_SEARCH_TIMEOUT_S",
    "DEFAULT_TALK_MODEL",
    "DEFAULT_TALK_PROVIDER",
    "DEFAULT_TALK_VOICE",
    "DEFAULT_VOICE_MODE",
    "DUPLEX_TOOL_COMPATIBLE_MODELS",
    "GEMINI_LIVE_VOICES",
    "GROK_REALTIME_VOICES",
    "KNOWN_INCOMPATIBLE_REALTIME_MODELS",
    "MODEL_COMPATIBILITY_POLICY_VERSION",
    "OPENAI_REALTIME_VOICES",
    "TALK_CASCADE_TTS_PROVIDERS",
    "TALK_PROVIDERS",
    "TALK_VOICE_MODES",
    "TalkConfigError",
    "agent_profile",
    "agent_timeout_s",
    "api_server_key",
    "api_server_poll_s",
    "api_server_probe_timeout_s",
    "api_server_probe_ttl_s",
    "api_server_url",
    "approval_permit_ttl_s",
    "approval_prompt_timeout_s",
    "audio_input_device",
    "audio_output_device",
    "cascade_tts",
    "cascade_voice_config",
    "catalog_startup_wait_s",
    "detect_agent_profile",
    "discord_operator_user_ids",
    "elevenlabs_model",
    "elevenlabs_voice_id",
    "get_hermes_home",
    "identity_include",
    "memory_search_timeout_s",
    "realtime_model_compatibility",
    "realtime_model_valid",
    "resolve_elevenlabs_key",
    "resolve_gemini_key",
    "resolve_openai_key",
    "resolve_xai_key",
    "state_dir",
    "talk_gemini_model",
    "talk_gemini_voice",
    "talk_grok_model",
    "talk_grok_voice",
    "talk_model",
    "talk_provider",
    "talk_voice",
    "trust_declared_read_only",
    "voice_mode",
]
