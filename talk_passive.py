"""Passive history v1 wire contract. Trusted host code configures this transport.

Calls are synchronous and belong in a worker, never the realtime audio loop.
There is deliberately no chat, execution, provider or database fallback here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

OPERATIONS = frozenset({"attach", "snapshot", "commit", "reconcile", "detach"})
MAX_RESPONSE_BYTES = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_CODES = frozenset(
    {
        "unsupported",
        "unavailable",
        "unauthorized",
        "invalid_input",
        "malformed_response",
        "busy",
        "store_unavailable",
        "stale_attachment",
        "target_missing",
        "target_unavailable",
        "event_conflict",
        "retired",
        "invalid_request",
        "payload_too_large",
        "denied",
        "stale_generation",
        "not_attached",
        "outbox_unavailable",
        "outbox_full",
        "expired",
        "owner_mismatch",
        "unknown_event",
        "not_passive",
    }
)


class HistoryError(Exception):
    """Only a closed vocabulary crosses diagnostics, never server/transport text."""

    def __init__(self, code: str, *, retryable: bool = False):
        self.code = code if code in _CODES else "malformed_response"
        self.retryable = retryable and self.code in {"busy", "store_unavailable", "unavailable"}
        super().__init__(self.code)


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise HistoryError("invalid_input")
    return value


def session_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise HistoryError("invalid_input")
    return value


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryOwner:
    host: str
    profile: str
    principal: str
    session_id: str

    @property
    def key(self) -> str:
        return digest([self.host, self.profile, self.principal, self.session_id])


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: str
    content: str = field(repr=False)

    def wire(self) -> dict:
        return {"role": self.role, "content": self.content}


def dialogue_messages(rows: object, *, max_bytes: int = 65536) -> tuple[HistoryMessage, ...]:
    if not isinstance(rows, (tuple, list)) or not 1 <= len(rows) <= 2:
        raise HistoryError("invalid_input")
    result = []
    for row in rows:
        if not isinstance(row, HistoryMessage) or row.role not in {"user", "assistant"}:
            raise HistoryError("not_passive")
        try:
            valid = isinstance(row.content, str) and row.content.strip()
            valid = valid and len(row.content.encode("utf-8")) <= max_bytes
        except UnicodeEncodeError:
            valid = False
        if not valid:
            raise HistoryError("invalid_input")
        result.append(row)
    if len(result) == 2 and [row.role for row in result] != ["user", "assistant"]:
        raise HistoryError("invalid_input")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HistoryCapabilities:
    max_request_bytes: int
    max_message_bytes: int
    max_messages: int
    max_snapshot_messages: int
    max_snapshot_bytes: int

    @classmethod
    def parse(cls, data: object) -> HistoryCapabilities:
        if not isinstance(data, dict):
            raise HistoryError("unsupported")
        operations = data.get("operations")
        if (
            type(data.get("version")) is not int
            or data["version"] != 1
            or data.get("passive_only") is not True
            or type(data.get("origin_adoption")) is not bool
            or data.get("restart_requires_reattach") is not True
            or not isinstance(operations, list)
            or any(not isinstance(op, str) for op in operations)
            or not OPERATIONS.issubset(operations)
        ):
            raise HistoryError("unsupported")
        limits = {
            "max_request_bytes": 160 * 1024,
            "max_message_bytes": 64 * 1024,
            "max_messages": 2,
            "max_snapshot_messages": 20,
            "max_snapshot_bytes": 32 * 1024,
        }
        for key, bound in limits.items():
            if type(data.get(key)) is not int or not 1 <= data[key] <= bound:
                raise HistoryError("unsupported")
        return cls(**{key: data[key] for key in limits})


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    conversation_id: str
    session_id: str
    messages: tuple[HistoryMessage, ...] = field(repr=False)
    truncated: bool

    @classmethod
    def parse(cls, data: object) -> HistorySnapshot:
        try:
            if not isinstance(data, dict) or type(data.get("truncated")) is not bool:
                raise HistoryError("malformed_response")
            caps = HistoryCapabilities.parse(data.get("capabilities"))
            rows = data.get("messages")
            if not isinstance(rows, list) or len(rows) > caps.max_snapshot_messages:
                raise HistoryError("malformed_response")
            messages = []
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != {"id", "role", "content"}
                    or type(row["id"]) is not int
                    or row["id"] <= 0
                ):
                    raise HistoryError("malformed_response")
                messages.extend(dialogue_messages([HistoryMessage(row["role"], row["content"])]))
            if sum(len(row.content.encode("utf-8")) for row in messages) > caps.max_snapshot_bytes:
                raise HistoryError("malformed_response")
            return cls(
                session_id(data["conversation_id"]),
                session_id(data["session_id"]),
                tuple(messages),
                data["truncated"],
            )
        except (KeyError, HistoryError):
            raise HistoryError("malformed_response") from None


@dataclass(frozen=True, slots=True)
class HistoryReceipt:
    event_id: str
    origin_turn_id: str
    conversation_id: str
    session_id: str
    message_ids: tuple[int, ...]
    revision: int

    @classmethod
    def parse(
        cls,
        data: object,
        *,
        event_id: str,
        origin_turn_id: str,
        conversation_id: str,
        message_count: int,
    ) -> HistoryReceipt:
        if not isinstance(data, dict):
            raise HistoryError("malformed_response")
        ids = data.get("message_ids")
        if (
            data.get("producer") != "passive.ingress.v1"
            or data.get("event_id") != event_id
            or data.get("origin_turn_id") != origin_turn_id
            or data.get("conversation_id") != conversation_id
            or type(data.get("revision")) is not int
            or data["revision"] <= 0
            or type(data.get("replayed")) is not bool
            or not isinstance(ids, list)
            or len(ids) != message_count
            or any(type(value) is not int or value <= 0 for value in ids)
            or len(set(ids)) != len(ids)
        ):
            raise HistoryError("malformed_response")
        try:
            segment = session_id(data.get("session_id"))
        except HistoryError:
            raise HistoryError("malformed_response") from None
        return cls(event_id, origin_turn_id, conversation_id, segment, tuple(ids), data["revision"])


@dataclass(frozen=True, slots=True)
class HistoryTransport:
    """Operator-configured HTTP connection, including its fixed authentication scope.

    ``profile`` must already be resolved by the host. ``named_profile`` selects
    the gateway's /p/{profile} route; dashboard always sends its profile selector.
    Credentials are captured once, so a queued event cannot drift with env changes.
    OAuth adapters and surface wiring are outside this module's current contract.
    """

    base_url: str = field(repr=False)
    profile: str
    credential: str = field(repr=False)
    surface: str = "gateway"
    named_profile: bool = False
    timeout_s: float = 3.0
    _http_transport: httpx.BaseTransport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or self.surface not in {"gateway", "dashboard"}
            or (self.surface == "dashboard" and self.named_profile)
            or not isinstance(self.credential, str)
            or not self.credential.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in self.credential)
            or not math.isfinite(self.timeout_s)
            or not 0 < self.timeout_s <= 30
        ):
            raise HistoryError("invalid_input")
        identifier(self.profile)

    @classmethod
    def configured_gateway(cls, *, profile: str, named_profile: bool = False) -> HistoryTransport:
        try:
            from . import talk_config
        except ImportError:  # pragma: no cover - flat Hermes plugin load
            import talk_config
        return cls(
            talk_config.api_server_url(),
            profile,
            talk_config.api_server_key() or "",
            named_profile=named_profile,
        )

    @property
    def prefix(self) -> str:
        if self.surface == "dashboard":
            return "/api/passive-history"
        return (f"/p/{self.profile}" if self.named_profile else "") + "/v1/passive-history"

    def owner(self, selected_session: str) -> HistoryOwner:
        return HistoryOwner(
            digest([self.base_url.rstrip("/"), self.prefix]),
            self.profile,
            digest([self.surface, self.credential]),
            session_id(selected_session),
        )

    def request(self, operation: str, body: dict | None = None) -> dict:
        if operation not in OPERATIONS | {"capabilities"}:
            raise HistoryError("unsupported")
        header = "Authorization" if self.surface == "gateway" else "X-Hermes-Session-Token"
        value = f"Bearer {self.credential}" if self.surface == "gateway" else self.credential
        params = {"profile": self.profile} if self.surface == "dashboard" else None
        try:
            content = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            if content is not None and len(content) > 160 * 1024:
                raise HistoryError("payload_too_large")
            with (
                httpx.Client(
                    timeout=self.timeout_s,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self._http_transport,
                ) as client,
                client.stream(
                    "GET" if operation == "capabilities" else "POST",
                    self.base_url.rstrip("/") + self.prefix + "/" + operation,
                    headers={header: value, "Content-Type": "application/json"},
                    params=params,
                    content=content,
                ) as response,
            ):
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise HistoryError("malformed_response")
                    raw.extend(chunk)
                status = response.status_code
            if status in {401, 403}:
                raise HistoryError("unauthorized")
            if operation == "capabilities" and status in {404, 405}:
                raise HistoryError("unsupported")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise HistoryError("malformed_response")
            if status != 200:
                code = data.get("error")
                if not isinstance(code, str) or type(data.get("retryable")) is not bool:
                    raise HistoryError("malformed_response")
                raise HistoryError(code, retryable=data["retryable"])
            return data
        except (httpx.HTTPError, OSError):
            raise HistoryError("unavailable", retryable=True) from None
        except (ValueError, TypeError, UnicodeError):
            raise HistoryError("malformed_response") from None
