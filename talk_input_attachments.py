"""Owner- and input-bound upload adapter in front of the host's attachment ingress.

``POST /attachments/upload`` is the only way a browser reaches an attachment. It
authenticates first, binds the current connection generation, reads the host's LIVE
capability (never a cached claim and never a constant), forwards one file to the host
with the gateway credential this process owns, and answers with an opaque reference
pinned server-side to ``(owner, connection_id, generation, input_id)``.

The pin is what makes a reference admissible later: ``/text/input`` refuses any
reference that was not uploaded under that exact identity, before anything is posted.
Nothing here creates a store — the pin lives on the existing binding, whose lifetime
IS the owner/connection/generation tuple, so a new generation cannot inherit one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

try:
    from starlette.requests import Request
    from starlette.responses import JSONResponse
except ImportError:  # pragma: no cover - plugin import without the dashboard extra
    Request = object
    JSONResponse = None

try:
    from .talk_dashboard_gateway import (
        ATTACHMENT_RECEIPT_FIELDS,
        INPUT_ATTACHMENT_PATH,
        DashboardTaskError,
    )
    from .talk_passive import HistoryError, digest, identifier
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import (
        ATTACHMENT_RECEIPT_FIELDS,
        INPUT_ATTACHMENT_PATH,
        DashboardTaskError,
    )
    from talk_passive import HistoryError, digest, identifier

UPLOAD_FIELDS = frozenset(
    {"connection_id", "generation", "input_id", "filename", "content_type", "bytes_base64"}
)
REFERENCE_FIELDS = ("attachment_id", "sha256")
#: Structural ceilings, not policy. A ceiling cannot be read from the host because the
#: body has to be bounded BEFORE it is parsed, and the reference list has to be bounded
#: before it is walked. Every semantic bound below comes from the host's live limits.
MAX_FILE_BYTES_CEILING = 10 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_FILE_BYTES_CEILING + 2) // 3)
MAX_UPLOAD_BODY_BYTES = MAX_BASE64_CHARS + 8192
MAX_REFERENCES_CEILING = 8
#: Per-input pins retained on one binding. Drafts are few and short-lived; this only
#: stops an unbounded walk of abandoned input ids from accumulating on a long session.
MAX_PINNED_INPUTS = 16
_ATTACHMENT_ID = re.compile(r"att_[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTENT_TYPE = re.compile(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+")
_LIMIT_FIELDS = ("max_files_per_input", "max_file_bytes", "max_input_bytes")


def feature(capabilities):
    """The host's verified ``features.input_attachments``, or ``None``.

    ``None`` is the only "unsupported" answer: a malformed descriptor, a missing
    limit, or an endpoint that is not the route this process actually calls all
    resolve to it rather than to a half-trusted capability.
    """
    if not isinstance(capabilities, dict):
        return None
    features = capabilities.get("features")
    if not isinstance(features, dict):
        return None
    published = features.get("input_attachments")
    endpoint = published.get("endpoint") if isinstance(published, dict) else None
    if (
        not isinstance(published, dict)
        or type(published.get("version")) is not int
        or published["version"] != 1
        or published.get("supported") is not True
        or published.get("dispatch_field") != "child.attachments"
        or not isinstance(endpoint, dict)
        or endpoint.get("method") != "POST"
        or endpoint.get("path") != INPUT_ATTACHMENT_PATH
        or sorted(published.get("reference_fields") or []) != sorted(REFERENCE_FIELDS)
    ):
        return None
    limits = published.get("limits")
    if not isinstance(limits, dict) or any(
        type(limits.get(key)) is not int or limits[key] < 1 for key in _LIMIT_FIELDS
    ):
        return None
    return published


def supported(capabilities):
    return feature(capabilities) is not None


def require_feature(capabilities):
    published = feature(capabilities)
    if published is None:
        raise DashboardTaskError("attachments_unsupported", 409)
    return published


def delivers_to_child(capabilities):
    """Whether the host says a Hermes child can actually RECEIVE these bytes.

    Stored is not delivered. The host publishes per-backend delivery availability and
    refuses the dispatch itself when it is false; refusing here keeps the operator's
    words and files un-posted instead of accepted-then-rejected.
    """
    published = feature(capabilities)
    delivery = (published or {}).get("delivery")
    child = delivery.get("hermes_child") if isinstance(delivery, dict) else None
    return isinstance(child, dict) and child.get("available") is True


def normalize_references(value, *, maximum=MAX_REFERENCES_CEILING):
    """Shape only: opaque ids, no duplicates, sorted so order is not identity."""
    if not isinstance(value, list) or len(value) > min(maximum, MAX_REFERENCES_CEILING):
        raise DashboardTaskError("invalid_event", 400)
    seen, normalized = set(), []
    for reference in value:
        if (
            not isinstance(reference, dict)
            or set(reference) != set(REFERENCE_FIELDS)
            or not isinstance(reference["attachment_id"], str)
            or not _ATTACHMENT_ID.fullmatch(reference["attachment_id"])
            or not isinstance(reference["sha256"], str)
            or not _SHA256.fullmatch(reference["sha256"])
            or reference["attachment_id"] in seen
        ):
            raise DashboardTaskError("invalid_event", 400)
        seen.add(reference["attachment_id"])
        normalized.append({key: reference[key] for key in REFERENCE_FIELDS})
    return sorted(normalized, key=lambda reference: reference["attachment_id"])


def validate(body):
    """Pure shape validation: no binding, no store read, no host I/O."""
    if not isinstance(body, dict) or set(body) != UPLOAD_FIELDS:
        raise DashboardTaskError("invalid_event", 400)
    try:
        input_id = identifier(body["input_id"])
    except HistoryError:
        raise DashboardTaskError("invalid_event", 400) from None
    filename, content_type, encoded = (
        body["filename"], body["content_type"], body["bytes_base64"],
    )
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename.encode("utf-8", "surrogatepass")) > 240
        or not isinstance(content_type, str)
        or not _CONTENT_TYPE.fullmatch(content_type)
        or not isinstance(encoded, str)
        or not encoded
    ):
        raise DashboardTaskError("invalid_event", 400)
    if len(encoded) > MAX_BASE64_CHARS:
        raise DashboardTaskError("attachment_size_limit", 413)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise DashboardTaskError("invalid_event", 400) from None
    if not data:
        raise DashboardTaskError("invalid_event", 400)
    return {
        "input_id": input_id,
        "filename": filename,
        "content_type": content_type,
        "content_base64": encoded,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def upload(tasks, request, body, fields):
    """Bind, read the live capability, forward one file, pin its opaque reference."""
    bound = tasks.binding(request, body, write=True)
    bound.capabilities = tasks.capabilities(bound, refresh=True)
    limits = require_feature(bound.capabilities)["limits"]
    input_id = fields["input_id"]
    # A stable id per (owner, connection, generation, input, filename, bytes): a retry of
    # the identical file replays the host's own receipt instead of storing a second copy,
    # and a different file under the same draft is a different upload by construction.
    upload_id = digest([
        bound.attachment.owner.key, bound.connection_id, bound.generation,
        input_id, fields["filename"], fields["sha256"],
    ])
    pinned = tasks.pinned_attachment(bound, input_id, upload_id)
    if pinned is not None:
        tasks.binding(request, body)
        return {"ok": True, "input_id": input_id, **pinned}
    if fields["bytes"] > limits["max_file_bytes"]:
        raise DashboardTaskError("attachment_size_limit", 413)
    tasks.check_attachment_room(bound, input_id, upload_id, fields["bytes"], limits)
    receipt = bound.gateway.upload_attachment({
        "session_id": bound.attachment.owner.session_id,
        "upload_id": upload_id,
        "filename": fields["filename"],
        "content_type": fields["content_type"],
        "content_base64": fields["content_base64"],
    })
    if receipt["bytes"] != fields["bytes"] or receipt["sha256"] != fields["sha256"]:
        # The receipt must describe the bytes THIS call sent, or it is not this file's.
        raise DashboardTaskError("gateway_response_invalid", 502)
    stored = tasks.pin_attachment(bound, input_id, upload_id, receipt, limits)
    tasks.binding(request, body)
    return {"ok": True, "input_id": input_id, **stored}


def mount_attachment_routes(router, *, require_auth, task_call, tasks, http_exception):
    async def read_upload(request):
        """Auth has already run. Bound before parsing: an oversized body never lands."""
        raw = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_UPLOAD_BODY_BYTES:
                    raise DashboardTaskError("attachment_size_limit", 413)
                raw.extend(chunk)
            payload = json.loads(raw)
        except DashboardTaskError as error:
            raise http_exception(status_code=error.status, detail=error.detail()) from None
        except (ValueError, TypeError, UnicodeError):
            raise http_exception(
                status_code=400, detail=DashboardTaskError("invalid_event", 400).detail(),
            ) from None
        return payload if isinstance(payload, dict) else {}

    @router.post("/attachments/upload")
    async def attachment_upload(request: Request):
        require_auth(request)
        body = await read_upload(request)
        fields = await task_call(lambda _request, value: validate(value), request, body)
        result = await task_call(
            lambda current, value: upload(tasks, current, value, fields), request, body,
        )
        if JSONResponse is None:  # pragma: no cover - no dashboard response implementation
            raise http_exception(
                status_code=503, detail=DashboardTaskError("context_unavailable", 503).detail(),
            )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return (attachment_upload,)


__all__ = [
    "ATTACHMENT_RECEIPT_FIELDS",
    "MAX_REFERENCES_CEILING",
    "MAX_UPLOAD_BODY_BYTES",
    "REFERENCE_FIELDS",
    "delivers_to_child",
    "feature",
    "mount_attachment_routes",
    "normalize_references",
    "require_feature",
    "supported",
    "upload",
    "validate",
]
