"""Vault recall — the durable-notes lookup a voice session can actually make.

Hermes's memory PROVIDERS publish a system-prompt block that tells the model
which tools to call (``homie_memory_search``, ``homie_memory_context``, …).
Those tools exist in a text agent's registry and **not** in a Realtime
session's, so passing that block through to a voice prompt spends identity
budget instructing the model to call things it does not have. This module is
the other half of the fix: it reaches the provider's index directly, in
process, so ``search_vault`` is a real tool and the pointer that names it is
true.

In process on purpose. The dispatch path used elsewhere
(``ctx.dispatch_tool``) needs an attached agent loop, which the gateway and
the dashboard do not have — the two lanes most likely to be asked "what do
you know about X". Loading the provider ourselves works on all three.

Two costs, both measured on a 1,000-document vault and both shaping the
design: the first resolve is ~0.9s and a search is ~0.3s. A tool call runs on
the loop carrying the microphone, so the index is built ONCE behind a
single-flight lock and kept, rather than rebuilt per lookup.

That first resolve is paid while the SESSION IS BEING MINTED, not on the
operator's first question: ``identity_sections`` builds the vault pointer and
``default_talk_tools`` asks whether to advertise the tool, and both run before
the call is live. It matters enough to be pinned by tests
(``test_the_index_is_built_during_session_setup_not_mid_call``) — if a future
change made the first resolve lazy, the symptom would be a one-second silence
on the operator's first lookup and nothing in the logs to explain it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

try:
    from . import talk_config
except ImportError:  # pragma: no cover - flat-module fallback (Hermes file-path load)
    import talk_config

_log = logging.getLogger(__name__)

#: Session id used for the read-only provider instance. Distinct from a real
#: conversation id so nothing this module does can be mistaken for a turn.
VAULT_SESSION_ID = "hermes-talk-vault"

#: Upper bound on what one lookup returns to the model. A voice answer is one
#: to three sentences; anything past this is budget spent on text that will
#: never be spoken.
MAX_VAULT_CHARS = 2_000

class VaultSearchError(Exception):
    """A vault lookup failed — distinct from a lookup that found nothing."""


_LOCK = threading.Lock()
_PROVIDER: Any | None = None
#: Distinct from ``_PROVIDER is None``: a failed load must not be retried on
#: every lookup, because the failure path includes a filesystem walk.
_RESOLVED = False
#: Whether the cached provider is OURS. A borrowed one belongs to a running
#: agent, and calling ``shutdown()`` on it would tear down that agent's memory
#: — the ownership flag is what keeps :func:`reset` from doing exactly that.
_OWNED = False


def _provider_from_agent() -> Any | None:
    """Borrow the LIVE agent's provider when a plugin context is attached.

    Free and side-effect-less compared to loading our own: that instance is
    already initialized, so borrowing skips a second full vault walk (~0.3s
    and a duplicate copy of the index in memory). The traversal mirrors
    ``PluginContext.dispatch_tool``'s own route to the parent agent
    (plugins.py:604-608) and is guarded at every hop — these are framework
    internals and may simply not be there.
    """

    ctx = talk_host_ctx()
    if ctx is None:
        return None
    try:
        cli = getattr(getattr(ctx, "_manager", None), "_cli_ref", None)
        manager = getattr(getattr(cli, "agent", None), "_memory_manager", None)
        if manager is None:
            return None
        for provider in getattr(manager, "providers", ()) or ():
            if getattr(provider, "_index", None) is not None:
                return provider
    except Exception as exc:  # noqa: BLE001 — a missing capability, never an outage
        _log.debug("agent provider unavailable: %s: %s", type(exc).__name__, exc)
    return None


def talk_host_ctx() -> Any | None:
    """The bound plugin context, read through :mod:`talk_host` at call time.

    Imported lazily and by MODULE rather than by symbol: ``talk_host`` imports
    this module, so a top-level import here would be circular, and binding
    ``get_ctx`` itself would freeze the lookup against a later rebind.
    """

    try:
        from . import talk_host
    except ImportError:  # pragma: no cover - flat-module fallback
        import talk_host
    return talk_host.get_ctx()


def _load_provider() -> Any | None:
    """Load and initialize the configured memory provider, or ``None``.

    ``initialize`` is required, not optional: a provider's index does not
    exist before it, and ``system_prompt_block``/``search`` are empty until it
    has run.
    """

    try:
        from plugins.memory import _get_active_memory_provider, load_memory_provider
    except Exception as exc:  # noqa: BLE001 — no Hermes, no vault
        _log.debug("memory provider plugins unavailable: %s: %s", type(exc).__name__, exc)
        return None

    provider = None
    try:
        active = _get_active_memory_provider()
        if not active:
            return None
        provider = load_memory_provider(active)
        if provider is None or not provider.is_available():
            _release(provider)  # constructed but unusable — still ours to close
            return None
        provider.initialize(
            VAULT_SESSION_ID,
            platform="cli",
            hermes_home=str(talk_config.get_hermes_home()),
            # Non-primary: the ABC's contract is that providers skip writes
            # outside a primary agent context. This instance only ever reads.
            agent_context="flush",
        )
    except Exception as exc:  # noqa: BLE001 — a missing capability, never an outage
        _log.debug("vault provider unavailable: %s: %s", type(exc).__name__, exc)
        # Abandoning a provider we CONSTRUCTED without closing it leaks it
        # past any reset(), because the cache only remembers what it kept.
        _release(provider)
        return None
    if getattr(provider, "_index", None) is None:
        # Loadable is not usable. A provider whose initialize left no index
        # answers every lookup with "" — which reads as "nothing written down
        # about that" for EVERY query. A false negative is worse than the
        # refusal we give when there is no provider at all, so this counts as
        # unavailable and the tool is never advertised.
        _log.debug("vault provider initialized without an index; treating as absent")
        _release(provider)
        return None
    return provider


def _release(provider: Any | None) -> None:
    """Shut down a provider WE own. Never called on a borrowed one."""

    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception as exc:  # noqa: BLE001 — teardown is best-effort
        _log.debug("vault provider shutdown failed: %s: %s", type(exc).__name__, exc)


def provider() -> Any | None:
    """The read-only provider for lookups: the agent's, else one of our own.

    Resolved at most once per process and then held, rather than rebuilt per
    lookup — a rebuild is a full vault walk on the loop carrying the
    microphone. The index is therefore a snapshot: a note written mid-call is
    not visible until restart, which is how the host's own provider behaves
    for a text session too.

    A BORROWED provider outlives the agent it came from (a ``/clear`` or
    ``/resume`` builds a new agent, and nothing here re-resolves). That is
    safe for the one thing this module does, and the host says so itself: the
    provider's ``on_session_switch`` documents that "the vault index is
    session-independent and read-only, so no other state needs to move". We
    only ever read. If this module ever gains a WRITE path, that assumption
    stops holding and the cache has to be invalidated on session switch.
    """

    global _PROVIDER, _RESOLVED, _OWNED
    with _LOCK:
        if _RESOLVED:
            return _PROVIDER

    # Resolve OUTSIDE the lock. `initialize()` is arbitrary third-party
    # provider code plus filesystem work; running it under a non-reentrant
    # lock means any provider that reached back into this module would
    # deadlock the call rather than degrade. The cost is that two threads
    # racing a cold cache can both build one — settled below, where the
    # loser's copy is closed rather than leaked.
    borrowed = _provider_from_agent()
    resolved = borrowed if borrowed is not None else _load_provider()
    owned = borrowed is None

    loser = None
    with _LOCK:
        if _RESOLVED:
            winner = _PROVIDER
            if owned and resolved is not winner:
                loser = resolved
        else:
            _PROVIDER, _OWNED, _RESOLVED = resolved, owned, True
            winner = _PROVIDER

    # Teardown happens OUTSIDE the lock for the same reason resolution does:
    # ``shutdown()`` is third-party code, and calling it under a non-reentrant
    # lock re-opens exactly the deadlock the split above was meant to close.
    _release(loser)
    return winner


def reset() -> None:
    """Drop the cached provider. For tests and for a config change.

    Only an instance WE loaded is shut down. A borrowed one belongs to a live
    agent that is still using it.
    """

    global _PROVIDER, _RESOLVED, _OWNED
    with _LOCK:
        stale = _PROVIDER if _OWNED else None
        _PROVIDER, _RESOLVED, _OWNED = None, False, False
    if stale is not None:
        try:
            stale.shutdown()
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            _log.debug("vault provider shutdown failed: %s: %s", type(exc).__name__, exc)


def available() -> bool:
    """Whether a vault lookup can actually be served in this process.

    The gate on advertising the tool at all. Advertising a lookup we cannot
    perform is the same defect this module exists to fix, one level up.
    """

    return provider() is not None


def document_count() -> int:
    """How many documents the index holds, or ``0`` when unknown."""

    active = provider()
    if active is None:
        return 0
    try:
        index = getattr(active, "_index", None)
        if index is None:
            return 0
        return int(index.status().get("documents") or 0)
    except Exception as exc:  # noqa: BLE001 — a count, never an outage
        _log.debug("vault status unavailable: %s: %s", type(exc).__name__, exc)
        return 0


def search(query: str, *, max_chars: int = MAX_VAULT_CHARS) -> str:
    """Look ``query`` up in the vault and return speakable text.

    Returns "" when there is genuinely nothing — the caller turns that into a
    spoken "nothing in my notes", which is a different sentence from an error
    and must stay distinguishable from one.
    """

    query = (query or "").strip()
    if not query:
        return ""
    active = provider()
    if active is None:
        # NOT "" — that is the sentence "nothing is written down about that",
        # and we do not know it. We know we could not look. Collapsing the two
        # would have the session tell the operator, in a confident voice, that
        # they never wrote something down.
        raise VaultSearchError("no memory provider is available in this process")
    try:
        # ``prefetch`` is the ABC's own read entrypoint, so this works for any
        # provider rather than only the one that ships with this box.
        found = (active.prefetch(query, session_id=VAULT_SESSION_ID) or "").strip()
    except Exception as exc:
        _log.warning("vault search failed: %s: %s", type(exc).__name__, exc)
        raise VaultSearchError(f"{type(exc).__name__}: {exc}") from exc
    return found[:max_chars]


__all__ = [
    "MAX_VAULT_CHARS",
    "VAULT_SESSION_ID",
    "VaultSearchError",
    "available",
    "document_count",
    "provider",
    "reset",
    "search",
]
