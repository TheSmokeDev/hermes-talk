"""Bounded Dograh REST access. No redirects, retries, logging, or artifact downloads."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .policy import DISCLOSURE, PhoneError, Settings, origin, positive_int

MAX_RESPONSE = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DograhClient:
    def __init__(self, settings: Settings, secret: str):
        if not secret or not isinstance(secret, str) or "\n" in secret or "\r" in secret:
            raise PhoneError("Set DOGRAH_API_KEY through Hermes's secret settings.")
        self.base = origin(settings.base_url)
        self.secret = secret
        self.settings = settings
        self.opener = build_opener(NoRedirect())

    def request(self, method: str, path: str, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        req = Request(
            self.base + path,
            data=payload,
            method=method,
            headers={
                "X-API-Key": self.secret,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"hermes-dograh/{__version__}",
            },
        )
        try:
            with self.opener.open(req, timeout=15) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise PhoneError("Dograh response exceeded the size limit.")
            return json.loads(raw)
        except HTTPError as exc:
            raise PhoneError(f"Dograh returned HTTP {exc.code}; response body withheld.") from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            if isinstance(exc, PhoneError):
                raise
            raise PhoneError(
                "Dograh request failed or returned invalid JSON; no automatic retry."
            ) from None

    def preflight(self):
        s = self.settings
        workflow = self.request("GET", f"/api/v1/workflow/fetch/{s.workflow_id}")
        if not isinstance(workflow, dict) or workflow.get("workflow_uuid") != s.agent_uuid:
            raise PhoneError("workflow_id does not match the configured Agent UUID.")
        versions = self.request(
            "GET", f"/api/v1/workflow/{s.workflow_id}/versions?status=published&limit=1"
        )
        if not isinstance(versions, list) or len(versions) != 1:
            raise PhoneError("The test agent needs one published workflow version.")
        version = versions[0]
        if not isinstance(version, dict) or version.get("status") != "published":
            raise PhoneError("Could not verify a published workflow.")
        configs = version.get("workflow_configurations") or {}
        cap = configs.get("max_call_duration")
        positive_int(cap, "Published max_call_duration", maximum=s.max_duration_seconds)
        nodes = (version.get("workflow_json") or {}).get("nodes", [])
        starts = [n for n in nodes if n.get("type") == "startCall"]
        if len(starts) != 1:
            raise PhoneError("Published workflow needs exactly one startCall node.")
        data = starts[0].get("data", {})
        if data.get("greeting_type") != "text" or data.get("greeting") != DISCLOSURE:
            raise PhoneError(
                "Published startCall text greeting must exactly match the documented AI disclosure."
            )
        if data.get("tool_uuids") or data.get("add_global_prompt", True):
            raise PhoneError("Disclosure startCall must disable global prompts and have no tools.")
        if data.get("allow_greeting_interrupt", True) is not False:
            raise PhoneError("Disable greeting interruptions on the disclosure startCall node.")
        return {"definition_id": version["id"], "max_duration_seconds": cap}

    def dial(self, draft):
        s = self.settings
        return self.request(
            "POST",
            f"/api/v1/public/agent/workflow/{s.agent_uuid}",
            {
                "phone_number": draft["phone_number"],
                "initial_context": {
                    "hermes_call_id": draft["call_id"],
                    "purpose": draft["purpose"],
                    "owner_instructions": draft["instructions"],
                },
                "telephony_configuration_id": s.telephony_configuration_id,
                "from_phone_number_id": s.from_phone_number_id,
            },
        )

    def check_caller(self):
        """Read metadata only. Do not fetch carrier credentials or sync webhooks."""
        s = self.settings
        configs = self.request("GET", "/api/v1/organizations/telephony-configs")
        rows = configs.get("configurations", []) if isinstance(configs, dict) else []
        if not isinstance(rows, list):
            raise PhoneError("Dograh returned invalid carrier metadata.")
        selected = [
            r for r in rows if isinstance(r, dict) and r.get("id") == s.telephony_configuration_id
        ]
        if (
            len(selected) != 1
            or selected[0].get("inactive") is not False
            or selected[0].get("supports_outbound") is not True
            or selected[0].get("is_ready_for_outbound") is not True
        ):
            raise PhoneError(
                "The selected carrier is missing, inactive, or not ready for outbound."
            )
        phones = self.request(
            "GET",
            f"/api/v1/organizations/telephony-configs/{s.telephony_configuration_id}/phone-numbers",
        )
        rows = phones.get("phone_numbers", []) if isinstance(phones, dict) else []
        if not isinstance(rows, list):
            raise PhoneError("Dograh returned invalid caller metadata.")
        selected = [
            r for r in rows if isinstance(r, dict) and r.get("id") == s.from_phone_number_id
        ]
        if (
            len(selected) != 1
            or selected[0].get("telephony_configuration_id") != s.telephony_configuration_id
            or selected[0].get("is_active") is not True
            or selected[0].get("address_type") != "pstn"
        ):
            raise PhoneError("The selected caller must be an active phone number on that carrier.")
        return {"carrier_ready": True, "caller_active": True}

    def run(self, run_id):
        positive_int(run_id, "run_id")
        value = self.request("GET", f"/api/v1/workflow/{self.settings.workflow_id}/runs/{run_id}")
        if (
            not isinstance(value, dict)
            or value.get("id") != run_id
            or value.get("workflow_id") != self.settings.workflow_id
        ):
            raise PhoneError("Dograh returned a different run than requested.")
        if type(value.get("is_completed")) is not bool:
            raise PhoneError("Dograh did not return a valid completion state.")
        # Do not return public download URLs, access tokens, logs, or the complete provider body.
        return {
            "run_id": run_id,
            "is_completed": value["is_completed"],
            "cost_info": value.get("cost_info"),
            "gathered_context": value.get("gathered_context"),
            "has_transcript": bool(
                value.get("transcript_url") or value.get("transcript_public_url")
            ),
            "has_recording": bool(value.get("recording_url") or value.get("recording_public_url")),
        }

    def verify_bridge_run(self, run_id, call_id):
        positive_int(run_id, "run_id")
        value = self.request("GET", f"/api/v1/workflow/{self.settings.workflow_id}/runs/{run_id}")
        if (
            not isinstance(value, dict)
            or value.get("id") != run_id
            or value.get("workflow_id") != self.settings.workflow_id
            or value.get("is_completed") is not False
            or not isinstance(value.get("initial_context"), dict)
            or value["initial_context"].get("hermes_call_id") != call_id
        ):
            raise PhoneError(
                "Dograh did not confirm an active run belonging to this approved call."
            )
