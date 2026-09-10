"""Authorized local profiles/Bot Chats and explicitly registered Hermes peer routes."""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass, replace

try:
    from .talk_dashboard_gateway import DashboardTaskError, TaskGateway
    from .talk_dashboard_tasks import DashboardOwnerContext, configured_transport
    from .talk_passive import HistoryError, HistoryTransport, digest, session_id
    from .talk_target_state import TargetState
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError, TaskGateway
    from talk_dashboard_tasks import DashboardOwnerContext, configured_transport
    from talk_passive import HistoryError, HistoryTransport, digest, session_id
    from talk_target_state import TargetState

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
PUBLIC_FIELDS = ("target_id", "kind", "label", "peer_id", "host_label", "profile", "session_id")


def profile_rows():
    from hermes_cli.profiles import list_profiles

    return [{"name": p.name, "display_name": p.display_name or p.name} for p in list_profiles()]


def peer_names():
    from hermes_cli.subcommands.peer import _load_peers

    return sorted(
        name
        for name, value in _load_peers().items()
        if isinstance(name, str) and _NAME.fullmatch(name) and isinstance(value, dict)
    )[:16]


def peer_route(name, profile, home):
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.subcommands.peer import _resolve_peer_target

    token = set_secret_scope(build_profile_secret_scope(home))
    try:
        resolved, scoped, config, key = _resolve_peer_target(name + "/" + profile)
    finally:
        reset_secret_scope(token)
    if (resolved, scoped) != (name, profile):
        raise DashboardTaskError("target_route_changed", 409)
    return config["url"], key


def local_transport(context):
    if context.profile_name == "default":
        return configured_transport(context)
    from agent.secret_scope import build_profile_secret_scope

    try:
        from . import talk_config
    except ImportError:  # pragma: no cover - flat plugin load
        import talk_config
    key = build_profile_secret_scope(context.profile_home).get("API_SERVER_KEY")
    if not key:
        raise DashboardTaskError("target_auth_unavailable", 503)
    return HistoryTransport(
        talk_config.api_server_url(),
        context.profile_name,
        key,
        named_profile=True,
        actor_scope=digest([context.principal_id, context.store_id]),
    )


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    context: DashboardOwnerContext
    transport: HistoryTransport
    record: dict


class TargetCatalog:
    def __init__(
        self,
        manager,
        *,
        profiles=profile_rows,
        peers=peer_names,
        resolve_peer=peer_route,
        local_factory=local_transport,
        remote_factory=HistoryTransport,
    ):
        self.manager = manager
        self.profiles, self.peers, self.resolve_peer = profiles, peers, resolve_peer
        self.local_factory, self.remote_factory = local_factory, remote_factory

    def actor(self, request):
        return self.manager.resolve_context(request, None)

    def state(self, request):
        return TargetState(self.actor(request))

    @staticmethod
    def public(record):
        return {key: record[key] for key in PUBLIC_FIELDS}

    def _route(self, request, peer_id, profile, *, expected=None):
        if not isinstance(profile, str) or not _NAME.fullmatch(profile):
            raise DashboardTaskError("invalid_event", 400)
        actor = self.actor(request)
        if peer_id == "local":
            context = self.manager.resolve_context(request, profile)
            transport = self.local_factory(context)
            gateway = TaskGateway(transport)
            gateway.require_local()
            proof = transport.request("capabilities")
            if proof.get("store_id") != context.store_id:
                raise DashboardTaskError("catalog_host_unverified", 503)
            fingerprint = digest([transport.base_url, transport.prefix, transport.credential])
        else:
            if not isinstance(peer_id, str) or not _NAME.fullmatch(peer_id):
                raise DashboardTaskError("target_missing", 404)
            try:
                if peer_id not in self.peers():
                    raise DashboardTaskError("target_missing", 404)
                url, key = self.resolve_peer(peer_id, profile, actor.profile_home)
            except (ImportError, AttributeError, LookupError):
                raise DashboardTaskError("target_missing", 404) from None
            except PermissionError:
                raise DashboardTaskError("target_auth_unavailable", 503) from None
            fingerprint = digest([peer_id, url, key, profile])
            if expected and expected["route_fingerprint"] != fingerprint:
                raise DashboardTaskError("target_route_changed", 409)
            probe = self.remote_factory(url, profile, key, named_profile=True)
            proof = probe.request("capabilities")
            store_id = proof.get("store_id")
            if not isinstance(store_id, str) or not store_id:
                raise DashboardTaskError("catalog_host_unverified", 503)
            home = (
                actor.profile_home
                / "state"
                / "talk-peers"
                / digest([peer_id, profile, fingerprint, store_id])
            )
            context = DashboardOwnerContext(
                actor.principal_id, actor.principal_kind, profile, home, store_id
            )
            transport = replace(probe, actor_scope=digest([actor.principal_id, peer_id, store_id]))
            gateway = TaskGateway(transport)
        if expected and (
            expected["route_fingerprint"] != fingerprint or expected["store_id"] != context.store_id
        ):
            raise DashboardTaskError("target_route_changed", 409)
        capabilities = gateway.capabilities()
        gateway.require_child(capabilities)
        return context, transport, fingerprint

    def catalog(self, request, body):
        if not isinstance(body, dict) or set(body) - {"peer_id", "profile", "tab_id"}:
            raise DashboardTaskError("invalid_event", 400)
        for key in ("peer_id", "profile"):
            if body.get(key) is not None and (
                not isinstance(body[key], str) or not _NAME.fullmatch(body[key])
            ):
                raise DashboardTaskError("invalid_event", 400)
        actor = self.actor(request)
        state = TargetState(actor)
        peer_id = body.get("peer_id") or "local"
        requested = body.get("profile")
        peers = [{"peer_id": "local", "label": "Local Hermes"}]
        # Local selection remains available without the optional saved-peer CLI.
        with suppress(ImportError, AttributeError):
            peers += [{"peer_id": name, "label": name} for name in self.peers()]
        if peer_id == "local":
            try:
                rows = self.profiles()
            except (ImportError, AttributeError):
                raise DashboardTaskError("target_unsupported", 503) from None
            if not isinstance(rows, list) or len(rows) > 32:
                raise DashboardTaskError("capacity", 409)
            scopes = [row for row in rows if not requested or row["name"] == requested]
        else:
            scopes = [{"name": requested or "default", "display_name": requested or "default"}]
        records, unavailable = [], []
        for row in scopes:
            profile = row["name"]
            try:
                context, transport, fingerprint = self._route(request, peer_id, profile)
                gateway = TaskGateway(transport)
                visible = gateway.sessions()
                bots = gateway.sessions(bot=True)
                seen = set()
                for kind, entries in (("bot", bots), ("task", visible)):
                    if kind == "bot" and len(entries) > 1:
                        unavailable.append(
                            {"peer_id": peer_id, "profile": profile, "reason": "target_ambiguous"}
                        )
                        continue
                    for entry in entries:
                        selected = session_id(entry.get("id"))
                        if selected in seen:
                            continue
                        if kind == "bot" and entry.get("title") != "Bot Chat":
                            continue
                        seen.add(selected)
                        label = (
                            (row.get("display_name") or profile)
                            if kind == "bot"
                            else (entry.get("title") or selected)
                        )
                        safe_label = str(label).replace(transport.credential, "[redacted]")[:200]
                        record = {
                            "peer_id": peer_id,
                            "profile": profile,
                            "session_id": selected,
                            "store_id": context.store_id,
                            "route_fingerprint": fingerprint,
                            "kind": kind,
                            "label": safe_label,
                            "host_label": "Local Hermes" if peer_id == "local" else peer_id,
                        }
                        record["target_id"] = digest(
                            [
                                actor.principal_id,
                                peer_id,
                                profile,
                                selected,
                                context.store_id,
                                fingerprint,
                            ]
                        )
                        records.append(record)
            except (DashboardTaskError, HistoryError, ValueError, RuntimeError) as exc:
                code = getattr(exc, "code", "target_unavailable")
                code = {
                    "unauthorized": "target_auth_denied",
                    "unavailable": "target_offline",
                    "unsupported": "target_unsupported",
                }.get(code, code)
                unavailable.append({"peer_id": peer_id, "profile": profile, "reason": code})
        state.cache(records)
        selection = {"current": None, "return_depth": 0}
        if body.get("tab_id"):
            saved = state.snapshot(body["tab_id"])
            if saved["current"]:
                current = saved["current"]
                selection["current"] = {
                    **self.public(current["target"]),
                    "connection_id": current["connection_id"],
                    "generation": current["generation"],
                }
            selection["return_depth"] = len(saved["stack"])
        return {
            "ok": True,
            "targets": [self.public(record) for record in records],
            "peers": peers,
            "unavailable": unavailable,
            "selection": selection,
        }

    def resolve(self, request, reference, *, peer_id=None, profile=None):
        if not isinstance(reference, str) or not reference.strip() or len(reference) > 256:
            raise DashboardTaskError("invalid_event", 400)
        # Refresh the requested authorized roster; no fuzzy matching or newest-session guesses.
        self.catalog(
            request, {"peer_id": peer_id or "local", **({"profile": profile} if profile else {})}
        )
        records = self.state(request).targets()
        query = reference.strip().casefold()
        records = [
            record
            for record in records
            if (not peer_id or record["peer_id"] == peer_id)
            and (not profile or record["profile"] == profile)
        ]
        exact = [record for record in records if record["target_id"] == reference]
        matches = exact or [
            record
            for record in records
            if (not peer_id or record["peer_id"] == peer_id)
            and (not profile or record["profile"] == profile)
            and query
            in {
                record["label"].casefold(),
                record["session_id"].casefold(),
                f"{record['peer_id']}/{record['profile']}".casefold(),
            }
        ]
        if len(matches) != 1:
            return {
                "state": "ambiguous" if matches else "missing",
                "choices": [self.public(row) for row in matches],
            }
        return {"state": "resolved", "target": matches[0]}

    def materialize(self, request, reference):
        record = self.state(request).target(reference) if isinstance(reference, str) else reference
        context, transport, _ = self._route(
            request, record["peer_id"], record["profile"], expected=record
        )
        return ResolvedTarget(context, transport, record)
