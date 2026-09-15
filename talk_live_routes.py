"""Authenticated browser and native Live routes over one task coordinator."""

from __future__ import annotations

import asyncio

try:
    from starlette.requests import Request
except ImportError:  # pragma: no cover - plugin import without the dashboard extra
    Request = object

try:
    from .talk_dashboard_gateway import DashboardTaskError
    from .talk_live_browser import LiveBrowserRegistry, speech_context
    from .talk_passive import HistoryError
    from .talk_task_sources import TaskEventError
except ImportError:  # pragma: no cover - flat plugin load
    from talk_dashboard_gateway import DashboardTaskError
    from talk_live_browser import LiveBrowserRegistry, speech_context
    from talk_passive import HistoryError
    from talk_task_sources import TaskEventError


def mount_live_routes(
    router,
    *,
    require_auth,
    read_body,
    task_call,
    tasks,
    targets,
    session_tools,
    http_exception,
    setup_factory=None,
    registry=None,
):
    registry = registry or LiveBrowserRegistry(
        tasks,
        targets,
        session_tools,
        require_auth,
        setup_factory=setup_factory,
    )

    async def invoke(operation, request, body):
        try:
            return await operation
        except (DashboardTaskError, HistoryError, TaskEventError) as error:

            def raise_domain_error(_request, _body, failure=error):
                raise failure

            return await task_call(raise_domain_error, request, body)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - upstream auth/session details are server-only
            raise http_exception(
                status_code=502,
                detail={
                    "code": "live_unavailable",
                    "message": (
                        "GPT-Live could not complete this operation. "
                        "Inspect the task before retrying."
                    ),
                },
            ) from None

    @router.post("/live/session")
    async def live_session(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await invoke(registry.create(request, body), request, body)

    @router.post("/live/events")
    async def live_events(request: Request):
        require_auth(request)
        body = await read_body(request)

        async def poll():
            binding = await registry.binding(request, body, refresh=True)
            return await binding.poll(body.get("after"), body.get("timing"))

        return await invoke(poll(), request, body)

    @router.post("/live/input")
    async def live_input(request: Request):
        require_auth(request)
        body = await read_body(request)

        async def typed():
            binding = await registry.binding(request, body)
            return await binding.typed(body.get("text"), body.get("input_id"))

        return await invoke(typed(), request, body)

    @router.post("/live/flush")
    async def live_flush(request: Request):
        require_auth(request)
        body = await read_body(request)

        async def flush():
            binding = await registry.binding(request, body, refresh=True)
            await binding.flush_capture()
            return {"ok": True}

        return await invoke(flush(), request, body)

    @router.post("/live/close")
    async def live_close(request: Request):
        require_auth(request)
        body = await read_body(request)

        async def close():
            binding = await registry.binding(request, body)
            await binding.close()
            registry.bindings.pop(binding.key, None)
            return {"ok": True}

        return await invoke(close(), request, body)

    @router.post("/live/transcript")
    async def live_transcript(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await task_call(registry.coordinator.transcript, request, body)

    @router.post("/live/delegation")
    async def live_delegation(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await invoke(registry.coordinator.delegation(request, body), request, body)

    @router.post("/live/typed")
    async def live_typed(request: Request):
        require_auth(request)
        body = await read_body(request)
        return await invoke(registry.coordinator.typed(request, body), request, body)

    @router.get("/live/operation")
    async def live_operation(request: Request):
        require_auth(request)
        query = request.query_params
        try:
            body = {
                "connection_id": query.get("connection_id"),
                "generation": int(query.get("generation", "")),
                "operation_id": query.get("operation_id"),
            }
        except (TypeError, ValueError):
            raise http_exception(
                status_code=400, detail=DashboardTaskError("invalid_event", 400).detail(),
            ) from None
        return await task_call(registry.coordinator.operation, request, body)

    @router.post("/live/speech")
    async def live_speech(request: Request):
        require_auth(request)
        body = await read_body(request)
        prepared = await task_call(tasks.speech, request, body)
        return await task_call(lambda _request, value: speech_context(value), request, prepared)

    handlers = (
        live_session,
        live_events,
        live_input,
        live_close,
        live_flush,
        live_transcript,
        live_delegation,
        live_typed,
        live_operation,
        live_speech,
    )
    if hasattr(router, "add_event_handler"):
        router.add_event_handler("shutdown", registry.close_all)
    return handlers, registry
