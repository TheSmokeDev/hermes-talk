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

    Unknown names are dropped rather than raising: a typo in a knob must
    narrow the prompt, never take the voice surface down with it.
    """

    raw = (os.environ.get("TALK_IDENTITY_INCLUDE") or "").strip()
    if not raw:
        return None
    names = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return names or None


def talk_model() -> str:
    """Realtime model, resolved at call time."""

    return (os.environ.get("TALK_MODEL") or DEFAULT_TALK_MODEL).strip() or DEFAULT_TALK_MODEL


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


__all__ = [
    "DEFAULT_AGENT_TIMEOUT_S",
    "DEFAULT_API_SERVER_POLL_S",
    "DEFAULT_API_SERVER_PROBE_TIMEOUT_S",
    "DEFAULT_API_SERVER_PROBE_TTL_S",
    "DEFAULT_API_SERVER_URL",
    "DEFAULT_TALK_MODEL",
    "DEFAULT_TALK_VOICE",
    "OPENAI_REALTIME_VOICES",
    "TalkConfigError",
    "agent_profile",
    "agent_timeout_s",
    "api_server_key",
    "api_server_poll_s",
    "api_server_probe_timeout_s",
    "api_server_probe_ttl_s",
    "api_server_url",
    "audio_input_device",
    "audio_output_device",
    "detect_agent_profile",
    "discord_operator_user_ids",
    "get_hermes_home",
    "identity_include",
    "resolve_openai_key",
    "state_dir",
    "talk_model",
    "talk_voice",
]
