"""Calling lifecycle independent of Hermes imports, so it can be exercised offline."""

from __future__ import annotations

from .policy import DISCLOSURE, PhoneError, Settings, positive_int, text, validate_number
from .store import Store


class PhoneService:
    def __init__(self, settings: Settings, store: Store, client_factory):
        self.settings, self.store, self.client_factory = settings, store, client_factory

    def prepare(self, session, phone_number, purpose, instructions=""):
        self.settings.validate()
        number = validate_number(phone_number, self.settings.allowed_numbers)
        purpose = text(purpose, "Purpose", 1000)
        if not isinstance(instructions, str) or len(instructions) > 4000:
            raise PhoneError("Instructions must be text of at most 4000 characters.")
        draft = {
            "phone_number": number,
            "purpose": purpose,
            "instructions": instructions,
            "config_fingerprint": self.settings.fingerprint(),
            "max_duration_seconds": self.settings.max_duration_seconds,
            "disclosure": DISCLOSURE,
        }
        call_id = self.store.prepare(session, draft)
        return {
            "call_id": call_id,
            "status": "prepared",
            "phone_number": number,
            "purpose": purpose,
            "instructions": instructions,
            "disclosure": DISCLOSURE,
            "max_duration_seconds": self.settings.max_duration_seconds,
            "cost_notice": "Carrier and voice/model charges may apply. No call has been placed.",
            "next_step": (
                f"Review in Hermes: /phone review {call_id}. "
                "The native picker can approve this exact card; then ask Hermes to call. "
                f"You can also type /phone approve {call_id}; approval expires in 120 seconds."
            ),
        }

    def approve(self, session, call_id, owner_id):
        if not owner_id or owner_id not in self.settings.owner_user_ids:
            raise PhoneError(
                "Approval requires an authenticated, allowlisted Hermes source user ID."
            )
        self.store.approve(call_id, session, owner_id, self.settings.fingerprint())
        return {
            "call_id": call_id,
            "status": "approved",
            "expires_in_seconds": 120,
            "next_step": "Ask Hermes to run phone_call with this call_id.",
        }

    def call(self, session, call_id):
        self.settings.ready()
        row = self.store.get(call_id, session)
        validate_number(row["draft"]["phone_number"], self.settings.allowed_numbers)
        # Consume approval BEFORE even a provider preflight; failures need a fresh reviewed card.
        draft = self.store.claim(call_id, session, self.settings.fingerprint())
        try:
            client = self.client_factory(self.settings)
            verified = client.preflight()
        except Exception:
            self.store.finish(call_id, session, "failed_preflight")
            raise
        draft["call_id"] = call_id
        try:
            # Recheck the switch after network preflight, immediately before the billable request.
            with self.store.connection() as db:
                self.store.check_switch(db)
        except PhoneError:
            self.store.finish(call_id, session, "cancelled")
            raise
        try:
            result = client.dial(draft)
            if not isinstance(result, dict):
                raise PhoneError("Invalid dial response.")
            run_id = positive_int(result.get("workflow_run_id"), "workflow_run_id")
        except Exception:  # noqa: BLE001 - external boundary; keep host/provider details private
            # Timeout/HTTP errors are ambiguous. Never retry and risk a duplicate paid call.
            self.store.finish(call_id, session, "unknown")
            raise PhoneError(
                "Dial outcome unknown. Check Dograh before doing anything else; "
                "this approval cannot be reused."
            ) from None
        self.store.finish(call_id, session, "started", run_id=run_id, result=verified)
        return {"call_id": call_id, "status": "started", "run_id": run_id, **verified}

    def status(self, session, call_id):
        row = self.store.get(call_id, session)
        if row["status"] == "started":
            if row["draft"]["config_fingerprint"] != self.settings.fingerprint():
                raise PhoneError(
                    "Restore the call's original settings before polling its provider."
                )
            result = self.client_factory(self.settings).run(row["run_id"])
            status = "completed" if result["is_completed"] else "started"
            self.store.finish(call_id, session, status, result=result)
            return {"call_id": call_id, "status": status, "untrusted_call_data": result}
        return {
            "call_id": call_id,
            "status": row["status"],
            "run_id": row["run_id"],
            "untrusted_call_data": row["result"],
        }
