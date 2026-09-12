"""Trusted Discord room proof, independent of the selected task's configured peer."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

try:
    from .talk_dashboard_gateway import DashboardTaskError, TaskGateway
    from .talk_passive import HistoryTransport, identifier, session_id
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError, TaskGateway
    from talk_passive import HistoryTransport, identifier, session_id

FIELDS = frozenset({"surface", "surface_token", "surface_profile", "anchor_session_id",
                    "guild_id", "channel_id", "operator_user_id"})


def public_context(value):
    try:
        result = {"surface": "discord"}
        for key in ("guild_id", "channel_id", "operator_user_id"):
            raw = value[key]
            if isinstance(raw, bool) or str(int(raw)) != str(raw) or int(raw) <= 0:
                raise ValueError
            result[key] = int(raw)
        audience = value["audience_user_ids"]
        if not isinstance(audience, list) or not audience or len(audience) > 100:
            raise ValueError
        result["audience_user_ids"] = sorted({int(item) for item in audience})
        if (any(item <= 0 for item in result["audience_user_ids"])
                or result["operator_user_id"] not in result["audience_user_ids"]):
            raise ValueError
        result["audience_revision"] = identifier(value["audience_revision"])
        if value["surface"] != "discord":
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError):
        raise DashboardTaskError("context_denied", 403) from None


@dataclass
class NativeSurface:
    issuer: TaskGateway = field(repr=False)
    binding: dict = field(repr=False)
    context: dict

    def verify(self):
        current = self.issuer.discord_context("verify", self.binding)
        if public_context(current) != self.context:
            raise DashboardTaskError("context_denied", 403)
        return self.context

    def revoke(self):
        return self.issuer.discord_context("revoke", self.binding)


def prepare_surface(bound, body, *, previous=None, issuer_factory=None):
    prior = previous.native_surface if previous is not None else None
    surface = body.get("surface", "discord" if prior else "cli")
    if surface not in {"cli", "discord"} or (prior is not None and surface != "discord"):
        raise DashboardTaskError("context_denied", 403)
    if surface == "cli":
        if any(key in body for key in FIELDS - {"surface"}):
            raise DashboardTaskError("context_denied", 403)
        return {"surface": "cli"}
    if prior is not None:
        prior.verify()
        current = prior.issuer.discord_context("rebind", {
            **prior.binding, "next_session_id": prior.binding["session_id"],
            "next_binding_id": bound.connection_id,
        })
        issuer = prior.issuer
        binding = {"proof": current["proof"], "session_id": prior.binding["session_id"],
                   "binding_id": bound.connection_id}
    else:
        proof = body.get("surface_token")
        if not isinstance(proof, str) or not 1 <= len(proof) <= 256:
            raise DashboardTaskError("context_denied", 403)
        profile = identifier(body.get("surface_profile", "default"))
        anchor = session_id(body.get("anchor_session_id"))
        if issuer_factory is None:
            transport = HistoryTransport.configured_gateway(
                profile=profile, named_profile=profile != "default",
            )
            issuer = TaskGateway(transport)
        else:
            issuer = issuer_factory(profile)
        binding = {"proof": proof, "session_id": anchor, "binding_id": bound.connection_id}
        current = issuer.discord_context("redeem", binding)
    context = public_context(current)
    for key in ("guild_id", "channel_id", "operator_user_id"):
        if key in body and str(body[key]) != str(context[key]):
            issuer.discord_context("revoke", binding)
            raise DashboardTaskError("context_denied", 403)
    guard = NativeSurface(issuer, binding, context)
    bound.native_surface = guard
    bound.gateway = replace(bound.gateway, before_request=guard.verify)
    return context
