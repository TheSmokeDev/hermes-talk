from dataclasses import replace

import pytest

from talk_phone.policy import Settings
from talk_phone.service import PhoneService
from talk_phone.store import Store

# NANP 555-01xx fictional range. These numbers are never dialed.
NUMBER = "+12025550123"
SESSION = "synthetic-session-a"
OWNER = "local:synthetic-owner"


class FakeDograh:
    def __init__(self):
        self.posts = []
        self.completed = False
        self.fail = False
        self.preflight_error = False

    def preflight(self):
        if self.preflight_error:
            raise ValueError("Failed preflight")
        return {"definition_id": 1, "max_duration_seconds": 120}

    def dial(self, draft):
        self.posts.append(draft)
        if self.fail:
            raise TimeoutError("simulated response loss")
        return {"status": "success", "workflow_run_id": 42}

    def run(self, run_id):
        return {"run_id": run_id, "is_completed": self.completed, "cost_info": {"total": 0}}

    def verify_bridge_run(self, run_id, call_id):
        from talk_phone.policy import PhoneError

        if self.completed or run_id != 42 or self.posts[-1]["call_id"] != call_id:
            raise PhoneError("Fake provider refused the run binding.")


@pytest.fixture
def setup_phone(tmp_path):
    now = [1000.0]
    store = Store(tmp_path / "calls.db", clock=lambda: now[0])
    settings = Settings(
        workflow_id=1,
        agent_uuid="00000000-0000-4000-8000-000000000001",
        allowed_numbers=(NUMBER,),
        owner_user_ids=(OWNER,),
        dialing_enabled=True,
        telephony_configuration_id=1,
        from_phone_number_id=1,
    )
    fake = FakeDograh()
    api = PhoneService(settings, store, lambda _: fake)
    return api, fake, now


def prepared(api):
    return api.prepare(SESSION, NUMBER, "Synthetic local test", "Ask a test question")["call_id"]


def approved(api):
    call_id = prepared(api)
    api.approve(SESSION, call_id, OWNER)
    return call_id


def with_settings(api, **changes):
    return PhoneService(replace(api.settings, **changes), api.store, api.client_factory)
