from concurrent.futures import ThreadPoolExecutor

import pytest

from talk_phone.policy import PhoneError, origin, validate_number

from .conftest import NUMBER, OWNER, SESSION, approved, prepared, with_settings


def test_prepare_never_contacts_dograh(setup_phone):
    api, fake, _ = setup_phone
    call_id = prepared(api)
    assert api.status(SESSION, call_id)["status"] == "prepared"
    assert fake.posts == []


@pytest.mark.parametrize(
    "number,allowlist",
    [
        (NUMBER, ()),
        ("911", ("911",)),
        ("112", ("112",)),
        ("+19005550123", ("+19005550123",)),
        ("+12025550124", (NUMBER,)),
        ("2025550123", ("2025550123",)),
        (NUMBER + " ext 2", (NUMBER + " ext 2",)),
    ],
)
def test_rejects_numbers_before_network(number, allowlist):
    with pytest.raises(PhoneError):
        validate_number(number, allowlist)


def test_requires_human_approval(setup_phone):
    api, fake, _ = setup_phone
    with pytest.raises(PhoneError, match="approval"):
        api.call(SESSION, prepared(api))
    assert fake.posts == []


def test_approval_is_owner_and_session_bound(setup_phone):
    api, _, _ = setup_phone
    call_id = prepared(api)
    with pytest.raises(PhoneError):
        api.approve(SESSION, call_id, "local:someone-else")
    with pytest.raises(PhoneError):
        api.approve("other-session", call_id, OWNER)
    api.approve(SESSION, call_id, OWNER)
    with pytest.raises(PhoneError):
        api.call("other-session", call_id)


def test_one_use_approval_under_concurrency(setup_phone):
    api, fake, _ = setup_phone
    call_id = approved(api)

    def call():
        try:
            return api.call(SESSION, call_id)["status"]
        except PhoneError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: call(), range(8)))
    assert results.count("started") == 1
    assert len(fake.posts) == 1


def test_expiry_and_config_change_invalidate_approval(setup_phone):
    api, fake, now = setup_phone
    call_id = approved(api)
    with pytest.raises(PhoneError, match="Settings changed"):
        with_settings(api, max_duration_seconds=60).call(SESSION, call_id)
    now[0] += 121
    with pytest.raises(PhoneError, match="approval"):
        api.call(SESSION, call_id)
    assert fake.posts == []


def test_old_draft_cannot_be_approved(setup_phone):
    api, _, now = setup_phone
    call_id = prepared(api)
    now[0] += 601
    with pytest.raises(PhoneError, match="expired"):
        api.approve(SESSION, call_id, OWNER)


def test_kill_revokes_all_pending_approvals(setup_phone):
    api, fake, _ = setup_phone
    call_id = approved(api)
    api.store.kill()
    api.store.kill(False)
    with pytest.raises(PhoneError):
        api.call(SESSION, call_id)
    assert fake.posts == []


def test_unknown_post_never_retries_or_allows_another_call(setup_phone):
    api, fake, _ = setup_phone
    call_id = approved(api)
    fake.fail = True
    with pytest.raises(PhoneError, match="unknown"):
        api.call(SESSION, call_id)
    with pytest.raises(PhoneError):
        api.call(SESSION, call_id)
    second = approved(api)
    with pytest.raises(PhoneError, match="unresolved"):
        api.call(SESSION, second)
    assert len(fake.posts) == 1
    api.store.reconcile(call_id, SESSION)
    assert api.status(SESSION, call_id)["status"] == "reconciled"


def test_preflight_failure_consumes_approval(setup_phone):
    api, fake, _ = setup_phone
    fake.preflight_error = True
    call_id = approved(api)
    with pytest.raises(ValueError):
        api.call(SESSION, call_id)
    assert api.status(SESSION, call_id)["status"] == "failed_preflight"
    assert not fake.posts


def test_completed_call_frees_the_single_call_slot(setup_phone):
    api, fake, _ = setup_phone
    first = approved(api)
    api.call(SESSION, first)
    fake.completed = True
    assert api.status(SESSION, first)["status"] == "completed"
    assert api.call(SESSION, approved(api))["status"] == "started"


def test_disabled_dialing_and_missing_carrier_refuse(setup_phone):
    api, fake, _ = setup_phone
    call_id = approved(api)
    for changes in ({"dialing_enabled": False}, {"from_phone_number_id": 0}):
        with pytest.raises(PhoneError):
            with_settings(api, **changes).call(SESSION, call_id)
    assert not fake.posts


def test_cancel_draft_does_not_claim_to_hang_up(setup_phone):
    api, _, _ = setup_phone
    call_id = approved(api)
    api.store.cancel(call_id, SESSION)
    assert api.status(SESSION, call_id)["status"] == "cancelled"
    active = approved(api)
    api.call(SESSION, active)
    with pytest.raises(PhoneError, match="Only an undialed"):
        api.store.cancel(active, SESSION)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/api",
        "https://example.com?secret=a",
        "https://example.com/#x",
        "file:///tmp/x",
        "https://example.com\\@evil.test",
        "https://example.com\n",
    ],
)
def test_rejects_unsafe_origin(value):
    with pytest.raises(PhoneError):
        origin(value)


def test_supports_cloud_and_local_origin():
    assert origin("https://app.dograh.com/") == "https://app.dograh.com"
    assert origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
