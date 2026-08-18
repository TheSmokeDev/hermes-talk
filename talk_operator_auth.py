"""Fail-closed Discord authorization for state-changing Talk tools.

The model never supplies authorization data. This module binds Discord's
immutable user ID to the exact audio interval selected by server VAD, mints an
opaque response token, and later resolves a function call only through the
Realtime response ID and that token. Missing, stale, mixed, or unresolved
attribution denies mutation while leaving conversation and read-only tools
available.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback
    import talk_config

_log = logging.getLogger(__name__)

BINDING_METADATA_KEY = "talk_speaker_binding"
TRUSTED_BINDING_EVENT_KEY = "_talk_speaker_binding"
TRUSTED_CONTINUATION_EVENT_KEY = "_talk_continuation"

READ_ONLY_TALK_TOOLS = frozenset(
    {
        "search_memory",
        "search_vault",
        "check_work",
        "list_agents",
        "talk_status",
        "talk_capabilities",
    }
)
MUTATING_TALK_TOOLS = frozenset(
    {"delegate_task", "steer_agent", "redirect_agent", "stop_work"}
)

MUTATION_DENIAL = (
    "I couldn't verify that this response belongs to a configured Discord "
    "operator, so the {tool} tool was not run. Read-only requests are still available."
)

_SESSION_SAMPLE_RATE = 24_000
_SAMPLES_PER_MS = _SESSION_SAMPLE_RATE // 1_000
_DEFAULT_MAX_SEGMENTS = 4_096
_DEFAULT_MAX_RESPONSES = 64
_DEFAULT_MAX_SEEN_ITEM_IDS = 4_096
_DEFAULT_MAX_SEEN_RESPONSE_IDS = 4_096
_DEFAULT_MAX_SEEN_CALL_IDS = 4_096
_MAX_PROTOCOL_ID_CHARS = 512


def _valid_protocol_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_PROTOCOL_ID_CHARS
        and value == value.strip()
    )


@dataclass(frozen=True, slots=True)
class SpeakerBinding:
    """Trusted, immutable attribution for one response chain."""

    token: str
    user_id: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class _AudioSegment:
    start: int
    end: int
    user_id: int | None
    unresolved: bool


@dataclass(slots=True)
class _AuthorityChain:
    """Shared revocation state for a response and all of its continuations."""

    tainted: bool = False


@dataclass(frozen=True, slots=True)
class _ExpectedResponse:
    binding: SpeakerBinding
    chain: _AuthorityChain | None = None


@dataclass(slots=True)
class _ResponseAuthority:
    """One response generation; reuse permanently taints outstanding permits."""

    binding: SpeakerBinding
    chain: _AuthorityChain
    tainted: bool = False
    continuation: SpeakerBinding | None = None


@dataclass(slots=True)
class _CallPermit:
    """Single-use execution permit for one accepted call ID."""

    authority: _ResponseAuthority
    consumed: bool = False


class DiscordToolAuthorizationLedger:
    """Bounded PCM → VAD item → response → tool-call authorization ledger."""

    def __init__(
        self,
        *,
        max_segments: int = _DEFAULT_MAX_SEGMENTS,
        max_responses: int = _DEFAULT_MAX_RESPONSES,
        max_seen_item_ids: int = _DEFAULT_MAX_SEEN_ITEM_IDS,
        max_seen_response_ids: int = _DEFAULT_MAX_SEEN_RESPONSE_IDS,
        max_seen_call_ids: int = _DEFAULT_MAX_SEEN_CALL_IDS,
    ) -> None:
        self._max_segments = max(1, int(max_segments))
        self._max_responses = max(1, int(max_responses))
        self._max_seen_item_ids = max(1, int(max_seen_item_ids))
        self._max_seen_response_ids = max(1, int(max_seen_response_ids))
        self._max_seen_call_ids = max(1, int(max_seen_call_ids))
        self._lock = threading.RLock()
        self._segments: deque[_AudioSegment] = deque()
        self._sample_cursor = 0
        self._speech_starts: OrderedDict[str, int] = OrderedDict()
        self._items: OrderedDict[str, SpeakerBinding] = OrderedDict()
        self._item_phases: OrderedDict[str, str] = OrderedDict()
        self._tainted_items: set[str] = set()
        self._bindings: OrderedDict[str, _ExpectedResponse] = OrderedDict()
        # Expected tokens are one-shot capabilities.  Response and call ID
        # tombstones are never evicted: exhaustion poisons mutation for the
        # rest of the session rather than reopening a replay window.
        self._responses: OrderedDict[str, _ResponseAuthority] = OrderedDict()
        self._seen_responses: OrderedDict[str, _ResponseAuthority | None] = OrderedDict()
        self._seen_call_ids: set[str] = set()
        self._poisoned = False

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    @property
    def binding_count(self) -> int:
        return len(self._bindings)

    @property
    def response_count(self) -> int:
        return len(self._responses)

    def record_packet(self, speaker: dict[str, Any] | None, pcm: bytes) -> None:
        """Record attribution adjacent to the exact PCM sent to Realtime."""

        frames, remainder = divmod(len(pcm), 2)
        if frames <= 0:
            return
        user_id: int | None = None
        unresolved = remainder != 0
        if speaker is not None:
            try:
                parsed = int(speaker.get("user_id"))
            except (TypeError, ValueError):
                parsed = 0
            if parsed > 0:
                user_id = parsed
            else:
                unresolved = True
        # speaker=None is synthesized Discord silence, not an unknown person.
        with self._lock:
            start = self._sample_cursor
            self._sample_cursor += frames
            self._segments.append(
                _AudioSegment(start, self._sample_cursor, user_id, unresolved)
            )
            while len(self._segments) > self._max_segments:
                self._segments.popleft()

    def _poison(self, reason: str) -> None:
        if not self._poisoned:
            _log.error("Discord authorization ledger poisoned: %s", reason)
        self._poisoned = True
        for authority in self._seen_responses.values():
            if authority is not None:
                authority.tainted = True
                authority.chain.tainted = True
        self._responses.clear()
        self._bindings.clear()

    def _new_binding(self, user_id: int | None, reason: str) -> SpeakerBinding:
        return SpeakerBinding(secrets.token_urlsafe(18), user_id, reason)

    def _reserve_item(self, item_id: str, phase: str) -> bool:
        """Reserve one VAD item ID without ever reopening a replay tombstone."""

        if item_id in self._item_phases:
            return False
        if len(self._item_phases) >= self._max_seen_item_ids:
            self._poison("VAD item-ID tombstone capacity exhausted")
            return False
        self._item_phases[item_id] = phase
        return True

    def _taint_item(self, item_id: str, reason: str) -> SpeakerBinding:
        self._tainted_items.add(item_id)
        self._speech_starts.pop(item_id, None)
        binding = self._new_binding(None, reason)
        self._items[item_id] = binding
        return binding

    def _expect_binding(
        self, binding: SpeakerBinding, chain: _AuthorityChain | None = None
    ) -> bool:
        """Register a token only at the point its response.create is minted."""

        if self._poisoned:
            return False
        if len(self._bindings) >= self._max_responses * 2:
            self._poison("expected response-token capacity exhausted")
            return False
        self._bindings[binding.token] = _ExpectedResponse(binding, chain)
        return True

    def _binding_for_interval(self, start_ms: Any, end_ms: Any) -> SpeakerBinding:
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            return self._new_binding(None, "invalid VAD interval")
        start = start_ms * _SAMPLES_PER_MS
        end = end_ms * _SAMPLES_PER_MS
        if end > self._sample_cursor or not self._segments:
            return self._new_binding(None, "VAD interval is outside recorded audio")

        cursor = start
        user_ids: set[int] = set()
        unresolved = False
        for segment in self._segments:
            if segment.end <= start:
                continue
            if segment.start >= end:
                break
            overlap_start = max(start, segment.start)
            overlap_end = min(end, segment.end)
            if overlap_start > cursor:
                unresolved = True
            cursor = max(cursor, overlap_end)
            if segment.unresolved:
                unresolved = True
            if segment.user_id is not None:
                user_ids.add(segment.user_id)
        if cursor < end:
            unresolved = True
        if unresolved:
            return self._new_binding(None, "missing or unresolved speaker attribution")
        if len(user_ids) != 1:
            reason = "ambiguous speakers" if user_ids else "no resolved speaker"
            return self._new_binding(None, reason)
        return self._new_binding(next(iter(user_ids)), "resolved immutable Discord user ID")

    def note_speech_started(self, event: dict[str, Any]) -> None:
        """Record the production VAD start timestamp for one future item."""

        item_id = event.get("item_id")
        start_ms = event.get("audio_start_ms")
        if (
            not _valid_protocol_id(item_id)
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or start_ms < 0
        ):
            return
        with self._lock:
            if not self._reserve_item(item_id, "started"):
                self._taint_item(item_id, "duplicate or recycled VAD item ID")
                return
            self._speech_starts[item_id] = start_ms
            self._speech_starts.move_to_end(item_id)
            while len(self._speech_starts) > self._max_responses:
                self._speech_starts.popitem(last=False)

    def note_speech_stopped(self, event: dict[str, Any]) -> None:
        """Join production VAD start/stop events and freeze exact attribution."""

        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            return
        with self._lock:
            phase = self._item_phases.get(item_id)
            if phase is None:
                self._reserve_item(item_id, "stopped")
                self._taint_item(item_id, "speech stop arrived without matching start")
                return
            if phase != "started" or item_id in self._tainted_items:
                self._taint_item(item_id, "duplicate, recycled, or out-of-order VAD item ID")
                return
            self._item_phases[item_id] = "stopped"
            start_ms = self._speech_starts.pop(item_id, None)
            binding = self._binding_for_interval(start_ms, event.get("audio_end_ms"))
            self._items[item_id] = binding
            while len(self._items) > self._max_responses:
                self._items.popitem(last=False)

    @staticmethod
    def _response_create(binding: SpeakerBinding | None) -> dict[str, Any]:
        message: dict[str, Any] = {"type": "response.create"}
        if binding is not None:
            message["response"] = {
                "metadata": {BINDING_METADATA_KEY: binding.token}
            }
        return message

    def response_for_commit(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Create exactly one response carrying trusted speaker metadata."""

        item_id = event.get("item_id")
        with self._lock:
            if not _valid_protocol_id(item_id):
                return None
            phase = self._item_phases.get(item_id)
            if phase == "committed":
                return None
            if phase is None:
                self._reserve_item(item_id, "committed")
                self._taint_item(item_id, "commit arrived without matching VAD item")
            elif phase != "stopped" or item_id in self._tainted_items:
                self._taint_item(item_id, "out-of-order or recycled VAD item commit")
                self._item_phases[item_id] = "committed"
            else:
                self._item_phases[item_id] = "committed"
            binding = self._items.pop(item_id, None)
            if binding is None:
                binding = self._new_binding(None, "missing VAD item attribution")
            self._expect_binding(binding)
            return self._response_create(binding)

    def note_response_created(self, event: dict[str, Any]) -> None:
        """Bind a server response ID only when its opaque metadata is valid."""

        response = event.get("response")
        if not isinstance(response, dict):
            return
        response_id = response.get("id")
        metadata = response.get("metadata")
        token = metadata.get(BINDING_METADATA_KEY) if isinstance(metadata, dict) else None
        with self._lock:
            # Consume an echoed expected token even when another field is bad.
            # A malformed event must not leave a capability reusable later.
            expected = self._bindings.pop(token, None) if isinstance(token, str) else None
            if not _valid_protocol_id(response_id):
                return
            if response_id in self._seen_responses:
                previous = self._seen_responses[response_id]
                if previous is not None:
                    previous.tainted = True
                    previous.chain.tainted = True
                    if previous.continuation is not None:
                        self._bindings.pop(previous.continuation.token, None)
                self._responses.pop(response_id, None)
                _log.error("denied recycled Realtime response ID %s", response_id)
                return
            if len(self._seen_responses) >= self._max_seen_response_ids:
                self._poison("response-ID tombstone capacity exhausted")
                return
            # Every syntactically valid response.created reserves its ID,
            # including an unauthorized one.  A later reuse can never upgrade
            # that address into identity proof.
            self._seen_responses[response_id] = None
            if self._poisoned or expected is None:
                return
            if expected.chain is not None and expected.chain.tainted:
                return
            if len(self._responses) >= self._max_responses:
                self._poison("active response capacity exhausted")
                return
            chain = expected.chain or _AuthorityChain()
            authority = _ResponseAuthority(expected.binding, chain)
            self._seen_responses[response_id] = authority
            self._responses[response_id] = authority

    def binding_for_response(self, response_id: Any) -> SpeakerBinding | None:
        if not _valid_protocol_id(response_id):
            return None
        with self._lock:
            authority = self._responses.get(response_id)
            if (
                authority is None
                or authority.tainted
                or authority.chain.tainted
                or self._poisoned
            ):
                return None
            return authority.binding

    def bind_tool_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Overwrite model/server data with trusted response-bound context."""

        bound = dict(event)
        response_id = bound.get("response_id")
        with self._lock:
            authority = (
                self._responses.get(response_id)
                if _valid_protocol_id(response_id)
                else None
            )
            call_id = bound.get("call_id")
            call_once = _valid_protocol_id(call_id)
            if call_once:
                if call_id in self._seen_call_ids:
                    call_once = False
                elif len(self._seen_call_ids) >= self._max_seen_call_ids:
                    self._poison("call-ID tombstone capacity exhausted")
                    call_once = False
                else:
                    self._seen_call_ids.add(call_id)
            if (
                authority is None
                or authority.tainted
                or authority.chain.tainted
                or self._poisoned
            ):
                authority = None
            permit = _CallPermit(authority) if call_once and authority is not None else None
            binding = authority.binding if authority is not None else None
            continuation = None
            if authority is not None:
                if authority.continuation is None:
                    authority.continuation = self._new_binding(
                        authority.binding.user_id,
                        "fresh continuation of response-bound speaker",
                    )
                    self._expect_binding(authority.continuation, authority.chain)
                if not self._poisoned:
                    continuation = authority.continuation
            bound[TRUSTED_BINDING_EVENT_KEY] = binding
            bound["_talk_response_authority"] = authority
            bound["_talk_call_permit"] = permit
            bound[TRUSTED_CONTINUATION_EVENT_KEY] = self._response_create(continuation)
        return bound

    def authorize_tool(self, name: str, event: dict[str, Any]) -> str | None:
        """Authorize at execution time; read-only tools always remain available."""

        binding = event.get(TRUSTED_BINDING_EVENT_KEY)
        authority = event.get("_talk_response_authority")
        permit = event.get("_talk_call_permit")
        operator_ids = talk_config.discord_operator_user_ids()
        with self._lock:
            trusted = (
                not self._poisoned
                and isinstance(authority, _ResponseAuthority)
                and isinstance(permit, _CallPermit)
                and not permit.consumed
                and permit.authority is authority
                and not authority.tainted
                and not authority.chain.tainted
                and binding is authority.binding
            )
            # Every terminal handling attempt consumes its call permit, even
            # for read-only, unclassified, malformed, queue-rejected, or
            # currently unauthorized events. The same already-bound event can
            # therefore never be replayed later under a different tool name or
            # configuration.
            if isinstance(permit, _CallPermit):
                permit.consumed = True
        if name in READ_ONLY_TALK_TOOLS:
            return None
        if name not in MUTATING_TALK_TOOLS:
            _log.error("denied unclassified Discord Talk tool %s", name)
            return MUTATION_DENIAL.format(tool=name)
        if trusted and binding.user_id is not None and binding.user_id in operator_ids:
            return None
        reason = binding.reason if isinstance(binding, SpeakerBinding) else "unbound response"
        _log.warning("denied Discord mutating Talk tool %s: %s", name, reason)
        return MUTATION_DENIAL.format(tool=name)

    def complete_response(self, response_id: Any, *, continued: bool) -> None:
        """Release completed response state, retaining a continued chain token."""

        with self._lock:
            authority = self._responses.pop(response_id, None)
            if authority is not None and not continued and authority.continuation is not None:
                self._bindings.pop(authority.continuation.token, None)

    def clear(self) -> None:
        """Drop every attribution artifact on session teardown."""

        with self._lock:
            # External event dictionaries can still hold exact authority and
            # permit objects after our indexes are cleared. Revoke their shared
            # chains first so teardown can never make a stale bound event live.
            for authority in self._seen_responses.values():
                if authority is not None:
                    authority.tainted = True
                    authority.chain.tainted = True
            self._segments.clear()
            self._speech_starts.clear()
            self._items.clear()
            self._item_phases.clear()
            self._tainted_items.clear()
            self._bindings.clear()
            self._responses.clear()
            self._seen_responses.clear()
            self._seen_call_ids.clear()
            self._poisoned = False
            self._sample_cursor = 0


__all__ = [
    "BINDING_METADATA_KEY",
    "MUTATING_TALK_TOOLS",
    "MUTATION_DENIAL",
    "READ_ONLY_TALK_TOOLS",
    "TRUSTED_BINDING_EVENT_KEY",
    "TRUSTED_CONTINUATION_EVENT_KEY",
    "DiscordToolAuthorizationLedger",
    "SpeakerBinding",
]
