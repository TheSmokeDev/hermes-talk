"""Fail-closed Discord authorization for state-changing Talk tools.

The model never supplies authorization data. This module binds Discord's
immutable user ID to the exact audio interval selected by server VAD, mints an
opaque response token, and later resolves a function call only through the
Realtime response ID and that token. Missing, stale, mixed, or unresolved
attribution denies mutation while leaving conversation and read-only tools
available.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
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
#: Why bind_tool_event refused to mint a permit, when the speaker binding
#: alone would not explain the denial. Written only by bind_tool_event
#: (cleared from inbound events first), read only for the denial log line.
_PERMIT_REFUSAL_EVENT_KEY = "_talk_permit_refusal"

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
#: How many finished spoken turns the target cross-check searches. Old turns
#: age out, so a target mentioned long ago cannot keep authorizing forever.
_MAX_SPOKEN_TURNS = 32
_MAX_SPOKEN_CHARS_PER_TURN = 4_096


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
    #: Monotonic instant the operator's authorizing speech ended. Set only on
    #: the resolved-speaker path (continuations inherit it); every unresolved
    #: or tainted binding carries None, so no permit can exist without a real
    #: approval moment behind it.
    authorized_at: float | None = None


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


#: Mutating tools name their subject differently. The permit records what a
#: spoken approval actually covered, so the audit trail says which agent or
#: run was approved, not merely that something was.
_TARGET_ARGUMENT_KEYS = {
    "steer_agent": "agent_id",
    "redirect_agent": "agent_id",
    "stop_work": "target",
}


def _normalize_json_numbers(value: Any) -> Any:
    """Collapse integral floats to ints before hashing.

    ``1`` and ``1.0`` are the same argument value; a provider re-serializing
    one as the other must not read as a changed request. Bools are ints in
    Python and must keep their own identity.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_numbers(item) for item in value]
    return value


def _normalized_spoken_form(text: str) -> str:
    """Case-folded, alphanumeric-only form for spoken-target matching.

    Transcripts render an id like ``sa-0-a1b2c3d4`` with whatever spacing and
    punctuation the speech model chose; the comparison has to survive that,
    not the exact serialization.
    """

    return "".join(ch for ch in text.casefold() if ch.isalnum())


def _canonical_call(name: Any, arguments: Any) -> tuple[str, str | None]:
    """Hash normalized arguments and read the target this tool acts on.

    Malformed or non-JSON arguments still hash deterministically instead of
    raising: a tampered or truncated payload should change the hash, which
    denies the permit, rather than throw out of the mint or authorize path.
    Keys are sorted, separators fixed, and integral floats collapsed to ints
    so the same arguments always hash the same way regardless of how the
    provider serialized them.
    """

    parsed: Any = None
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            parsed = None
    canonical = (
        json.dumps(
            _normalize_json_numbers(parsed), sort_keys=True, separators=(",", ":")
        )
        if isinstance(parsed, dict)
        # repr, not str: repr(None) and repr("None") differ, so a missing
        # arguments value never collides with the literal string.
        else repr(arguments)
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    target: str | None = None
    if isinstance(parsed, dict) and isinstance(name, str):
        key = _TARGET_ARGUMENT_KEYS.get(name)
        if key is not None:
            candidate = parsed.get(key)
            if isinstance(candidate, str) and candidate:
                target = candidate
    return digest, target


@dataclass(slots=True)
class _CallPermit:
    """Single-use execution permit for one accepted call ID.

    Each field covers one named threat, and no field claims more than its
    check can see:

    - ``expires_at`` runs from the moment the operator's authorizing speech
      ended (monotonic clock), never from this mint — a model that sits on
      an approved action cannot present a fresh permit for a stale yes.
    - ``target`` was cross-checked against the spoken exchange before this
      permit existed: a mutating tool whose canonical target never appeared
      in what was actually said gets no permit at all. That cross-check is
      the only mechanism here that can see summary-vs-emitted divergence,
      and only for tools that name a target — free-text arguments (a
      delegated task's wording, a steering note's text) are not covered.
    - ``action`` must equal the tool name presented at execution time.
    - ``args_hash`` is a relay-integrity tripwire, nothing more: minted from
      the model-emitted arguments, it detects this process rewriting the
      bound event between bind and authorize, and cannot tell whether those
      arguments match anything the operator heard or approved.
    - ``talk_session_id`` is recorded for the audit trail only.
    """

    authority: _ResponseAuthority
    action: str
    args_hash: str
    target: str | None
    talk_session_id: str | None
    expires_at: float
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
        self._talk_session_id: str | None = None
        # The spoken exchange as this connection heard it: finished turns
        # (operator and assistant), plus per-response buffers for assistant
        # transcript deltas still streaming. Bounded on every axis.
        self._spoken: deque[str] = deque(maxlen=_MAX_SPOKEN_TURNS)
        self._transcript_buffers: OrderedDict[str, str] = OrderedDict()

    def bind_session(self, talk_session_id: str) -> None:
        """Attach this ledger to the exact Talk connection minting permits.

        Recorded on every permit minted from here on, for the audit trail
        only. Cross-session reuse is already structurally impossible: a
        fresh ledger is built per connection, so one ledger's permit is
        never checked by another's authorize_tool. Invariant a refactor must
        keep: the ledger is per-connection state, never pooled or shared —
        pooling would let one connection's permit satisfy another's
        authorize_tool, and this audit field would start lying about it.
        """

        with self._lock:
            self._talk_session_id = talk_session_id

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

    def _new_binding(
        self,
        user_id: int | None,
        reason: str,
        authorized_at: float | None = None,
    ) -> SpeakerBinding:
        return SpeakerBinding(secrets.token_urlsafe(18), user_id, reason, authorized_at)

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
        # The approval moment: this binding is frozen when the speech-stop
        # event lands, so "now" is when the operator's authorizing speech
        # ended. Permit expiry runs from here, never from permit mint.
        return self._new_binding(
            next(iter(user_ids)),
            "resolved immutable Discord user ID",
            authorized_at=time.monotonic(),
        )

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

    def note_transcript(self, event: dict[str, Any]) -> None:
        """Fold one transcript fragment into the spoken-exchange window.

        The transport feeds every transcript here: the operator's own final
        turns and the assistant's streaming deltas and finals. The window is
        what the permit target cross-check searches — a mutating tool's
        target must have appeared somewhere in the spoken exchange before a
        permit is minted for it. Deltas buffer per response so an id split
        across fragments still matches; text is taken as emitted, which can
        lead what a barge-in let the operator finish hearing.
        """

        text = event.get("text")
        fragment = text if isinstance(text, str) else ""
        response_id = event.get("response_id")
        key = response_id if _valid_protocol_id(response_id) else ""
        with self._lock:
            if not event.get("final"):
                if not fragment:
                    return
                buffered = self._transcript_buffers.get(key, "")
                if len(buffered) < _MAX_SPOKEN_CHARS_PER_TURN:
                    self._transcript_buffers[key] = buffered + fragment
                    self._transcript_buffers.move_to_end(key)
                while len(self._transcript_buffers) > self._max_responses:
                    self._transcript_buffers.popitem(last=False)
                return
            buffered = self._transcript_buffers.pop(key, "")
            turn = fragment.strip() or buffered.strip()
            if turn:
                self._spoken.append(turn[:_MAX_SPOKEN_CHARS_PER_TURN])

    def _target_was_spoken(self, target: str) -> bool:
        """Whether the spoken exchange ever contained this canonical target.

        Caller holds the lock. A target that normalizes to nothing cannot be
        verified against speech at all, so it fails closed.
        """

        needle = _normalized_spoken_form(target)
        if not needle:
            return False
        if any(needle in _normalized_spoken_form(turn) for turn in self._spoken):
            return True
        return any(
            needle in _normalized_spoken_form(buffered)
            for buffered in self._transcript_buffers.values()
        )

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
        # Only this method may explain a refused permit; an inbound event
        # claiming a refusal reason is model/server data, not ours.
        bound.pop(_PERMIT_REFUSAL_EVENT_KEY, None)
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
            permit = None
            if call_once and authority is not None:
                args_hash, target = _canonical_call(bound.get("name"), bound.get("arguments"))
                # Expiry runs from the operator's approval moment, never from
                # this mint: a model that sits on an approved action must not
                # get a fresh window for a stale yes. No approval instant →
                # no permit; the binding's own reason already names why.
                authorized_at = authority.binding.authorized_at
                if authorized_at is not None:
                    if target is not None and not self._target_was_spoken(target):
                        # The operator can only have approved what the
                        # exchange actually said. An emitted target that was
                        # never spoken is the summary-vs-emitted divergence
                        # #37 exists to stop.
                        bound[_PERMIT_REFUSAL_EVENT_KEY] = (
                            "target was never spoken to the operator"
                        )
                        _log.warning(
                            "refused call permit for %s: target %r was never "
                            "spoken to the operator",
                            str(bound.get("name") or "tool"),
                            target,
                        )
                    else:
                        permit = _CallPermit(
                            authority,
                            str(bound.get("name") or ""),
                            args_hash,
                            target,
                            self._talk_session_id,
                            authorized_at + talk_config.approval_permit_ttl_s(),
                        )
            binding = authority.binding if authority is not None else None
            continuation = None
            if authority is not None:
                if authority.continuation is None:
                    authority.continuation = self._new_binding(
                        authority.binding.user_id,
                        "fresh continuation of response-bound speaker",
                        # A continuation extends the same spoken approval; it
                        # must not restart the approval clock.
                        authorized_at=authority.binding.authorized_at,
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
        now = time.monotonic()
        # Relay-integrity tripwire, not an approval check: this hash and the
        # minted one both come from the model-emitted event dict, so a
        # mismatch can only mean this process rewrote the bound event between
        # bind and authorize — never that the model diverged from what it
        # said out loud. That divergence is caught (for target-bearing tools
        # only) by the spoken-target cross-check at mint time.
        args_hash, _target = _canonical_call(name, event.get("arguments"))
        with self._lock:
            expired = isinstance(permit, _CallPermit) and permit.expires_at < now
            rewritten = isinstance(permit, _CallPermit) and not hmac.compare_digest(
                permit.args_hash, args_hash
            )
            renamed = isinstance(permit, _CallPermit) and permit.action != name
            trusted = (
                not self._poisoned
                and isinstance(authority, _ResponseAuthority)
                and isinstance(permit, _CallPermit)
                and not permit.consumed
                and permit.authority is authority
                and not authority.tainted
                and not authority.chain.tainted
                and binding is authority.binding
                and not expired
                and not rewritten
                and not renamed
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
            # Approvals are logged as well as denials: an audit trail that only
            # records refusals cannot show what was actually authorized. Never
            # the audio, only who approved what.
            _log.info(
                "approved Discord mutating Talk tool %s for operator %s (session %s, target %s)",
                name,
                binding.user_id,
                permit.talk_session_id,
                permit.target,
            )
            return None
        if expired:
            reason = "approval permit expired"
        elif rewritten:
            reason = "bound arguments were rewritten after mint (relay integrity)"
        elif renamed:
            reason = "action does not match approved permit"
        else:
            refusal = event.get(_PERMIT_REFUSAL_EVENT_KEY)
            if isinstance(refusal, str) and refusal:
                reason = refusal
            elif isinstance(binding, SpeakerBinding):
                reason = binding.reason
            else:
                reason = "unbound response"
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
            self._spoken.clear()
            self._transcript_buffers.clear()
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
