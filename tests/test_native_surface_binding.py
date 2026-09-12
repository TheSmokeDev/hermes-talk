"""The room issuer and the selected task gateway retain independent ownership."""

from dataclasses import replace

import pytest
from test_dashboard_tasks import environment as base_environment
from test_dashboard_tasks import join

from talk_dashboard_gateway import DashboardTaskError
from talk_native_surface import prepare_surface


@pytest.fixture

def environment(tmp_path):
    return base_environment.__wrapped__(tmp_path)


class Issuer:
    def __init__(self):
        self.calls = []
        self.denied = False
        self.context = {"surface": "discord", "guild_id": "10", "channel_id": "20",
                        "operator_user_id": "30", "audience_user_ids": ["30", "40"],
                        "audience_revision": "revision-one"}

    def discord_context(self, operation, body):
        self.calls.append((operation, dict(body)))
        if self.denied:
            raise DashboardTaskError("context_denied", 403)
        return {**self.context, "proof": "replacement-private-proof"}


def test_native_room_proof_guards_each_selected_gateway_request(environment):
    manager, request, host, _ = environment
    bound, context = join(environment)
    issuer = Issuer()
    public = prepare_surface(bound, {"surface": "discord", "surface_token": "private-proof",
        "surface_profile": "discord-profile", "anchor_session_id": "issuer-anchor",
        "operator_user_id": 30}, issuer_factory=lambda profile: issuer)
    assert public["audience_user_ids"] == [30, 40]
    assert "proof" not in public and "surface_token" not in public
    bound.gateway.capabilities()
    assert issuer.calls[-1] == ("verify", {"proof": "private-proof",
        "session_id": "issuer-anchor", "binding_id": bound.connection_id})
    count = len(host.requests)
    issuer.denied = True
    with pytest.raises(DashboardTaskError):
        bound.gateway.capabilities()
    with pytest.raises(DashboardTaskError):
        manager.state(request, context)
    assert len(host.requests) == count


def test_requested_operator_cannot_replace_verified_identity(environment):
    bound, _ = join(environment)
    issuer = Issuer()
    with pytest.raises(DashboardTaskError):
        prepare_surface(bound, {"surface": "discord", "surface_token": "private-proof",
            "anchor_session_id": "issuer-anchor", "operator_user_id": 31},
            issuer_factory=lambda profile: issuer)
    assert issuer.calls[-1][0] == "revoke"
    assert bound.native_surface is None


def test_rebind_preserves_issuer_anchor_and_disallows_cli_downgrade(environment):
    bound, _ = join(environment)
    issuer = Issuer()
    prepare_surface(bound, {"surface": "discord", "surface_token": "private-proof",
        "anchor_session_id": "issuer-anchor"}, issuer_factory=lambda profile: issuer)
    candidate = replace(bound, connection_id="new-connection", native_surface=None)
    with pytest.raises(DashboardTaskError):
        prepare_surface(candidate, {"surface": "cli"}, previous=bound)
    prepare_surface(candidate, {"surface": "discord"}, previous=bound)
    assert issuer.calls[-1] == ("rebind", {"proof": "private-proof",
        "session_id": "issuer-anchor", "binding_id": bound.connection_id,
        "next_session_id": "issuer-anchor", "next_binding_id": "new-connection"})
    assert candidate.native_surface.binding["proof"] == "replacement-private-proof"
