"""Fail-closed dialing policy. These checks never make network requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from uuid import UUID

DISCLOSURE = "Hi, I'm an AI assistant calling on behalf of my owner."


class PhoneError(ValueError):
    """A safe, user-visible failure without provider response bodies or credentials."""


def origin(value: str) -> str:
    if not isinstance(value, str):
        raise PhoneError("Dograh base_url must be an HTTPS origin.")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise PhoneError("Invalid Dograh origin.") from exc
    local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or "\\" in value
        or any(ord(c) < 33 for c in value)
        or not (parts.scheme == "https" or (parts.scheme == "http" and local))
    ):
        raise PhoneError("Use an HTTPS origin without credentials, path, query, or fragment.")
    return value.rstrip("/")


def positive_int(value, label: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise PhoneError(
            f"{label} must be a positive integer"
            + (f" at most {maximum}." if maximum is not None else ".")
        )
    return value


def text(value, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PhoneError(f"{label} must be nonempty text of at most {limit} characters.")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise PhoneError(f"{label} contains control characters.")
    return value.strip()


def validate_number(value: str, allowed: tuple[str, ...]) -> str:
    import phonenumbers

    if not allowed:
        raise PhoneError("No test numbers are allowlisted; dialing is disabled.")
    if not isinstance(value, str) or value not in allowed:
        raise PhoneError("That exact E.164 number is not on the owner's test allowlist.")
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException as exc:
        raise PhoneError("Use a valid full E.164 number; short codes are not supported.") from exc
    regular = {
        phonenumbers.PhoneNumberType.FIXED_LINE,
        phonenumbers.PhoneNumberType.MOBILE,
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
    }
    if (
        not phonenumbers.is_valid_number(parsed)
        or parsed.extension
        or phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164) != value
        or phonenumbers.number_type(parsed) not in regular
    ):
        raise PhoneError(
            "Only valid fixed-line or mobile test numbers are allowed; no premium or short codes."
        )
    return value


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://app.dograh.com"
    workflow_id: int = 0
    agent_uuid: str = ""
    allowed_numbers: tuple[str, ...] = ()
    owner_user_ids: tuple[str, ...] = ()
    max_duration_seconds: int = 120
    dialing_enabled: bool = False
    telephony_configuration_id: int = 0
    from_phone_number_id: int = 0
    bridge_enabled: bool = False
    bridge_public_url: str = ""
    delegation_toolsets: tuple[str, ...] = ("web",)

    @classmethod
    def read(cls, get_config):
        defaults = cls()
        values = {key: get_config(key, default=value) for key, value in asdict(defaults).items()}
        for key in ("allowed_numbers", "owner_user_ids", "delegation_toolsets"):
            value = values[key]
            if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
                raise PhoneError(f"{key} must be a list of strings.")
            values[key] = tuple(value)
        if type(values["dialing_enabled"]) is not bool:
            raise PhoneError("dialing_enabled must be a boolean.")
        if type(values["bridge_enabled"]) is not bool:
            raise PhoneError("bridge_enabled must be a boolean.")
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self):
        origin(self.base_url)
        if type(self.max_duration_seconds) is not int or not 15 <= self.max_duration_seconds <= 300:
            raise PhoneError("max_duration_seconds must be between 15 and 300.")

    def ready(self):
        self.validate()
        if not self.dialing_enabled:
            raise PhoneError("Dialing is disabled in plugin settings.")
        self.connection_settings()

    def connection_settings(self):
        """Validate the configured connection without enabling or attempting a call."""
        self.validate()
        positive_int(self.workflow_id, "workflow_id")
        positive_int(self.telephony_configuration_id, "telephony_configuration_id")
        positive_int(self.from_phone_number_id, "from_phone_number_id")
        try:
            if str(UUID(self.agent_uuid)) != self.agent_uuid:
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise PhoneError(
                "Configure the canonical Agent UUID (not an API-trigger UUID)."
            ) from exc

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()
