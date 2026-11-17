"""Authenticated call transport into the owner's existing Hermes conversation."""

import asyncio
import hmac
import json
import time

from .bridge_store import BridgeStore
from .policy import PhoneError, Settings, origin


class BearerGuard:
    """Authenticate provider requests before reading their contents."""

    def __init__(self, app, token):
        if (
            not isinstance(token, str)
            or not 32 <= len(token) <= 512
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise PhoneError("Set DOGRAH_BRIDGE_TOKEN in Hermes secret settings.")
        self.app, self.expected = app, ("Bearer " + token).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        credentials = [v for k, v in headers if k.lower() == b"authorization"]
        browser = any(k.lower() == b"origin" for k, _ in headers)
        if (
            len(credentials) != 1
            or not hmac.compare_digest(credentials[0], self.expected)
            or browser
        ):
            from starlette.responses import JSONResponse

            return await JSONResponse(
                {"error": "Bridge access denied"}, status_code=403 if browser else 401
            )(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(store: BridgeStore, token: str, *, allowed_hosts=None):
    from starlette.applications import Starlette
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def turn(request):
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 32768:
                    return JSONResponse({"state": "unavailable"}, status_code=413)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise PhoneError("Expected a call request.")
            key = store.route(body.get("call_route"))
            if request.url.path == "/phone/turn":
                rid = store.enqueue(key, body.get("request_key"), "question", body.get("message"))
            else:
                rid = body.get("request_id")
                if not isinstance(rid, str):
                    raise PhoneError("Expected a request ID.")
            deadline = time.monotonic() + 20
            while True:
                result = store.remote_result(key, rid)
                if result["state"] != "pending" or time.monotonic() >= deadline:
                    return JSONResponse(result, headers={"Cache-Control": "no-store"})
                await asyncio.sleep(0.1)
        except (PhoneError, ValueError, TypeError):
            return JSONResponse(
                {"state": "unavailable", "message": "The call connection is unavailable."},
                status_code=400,
            )
        except Exception:  # noqa: BLE001 - provider boundary; never expose credentials or host data
            return JSONResponse(
                {"state": "unavailable", "message": "Hermes is temporarily unavailable."},
                status_code=503,
            )

    hosts = [h.split(":")[0] for h in (allowed_hosts or ["127.0.0.1", "localhost"])]
    app = Starlette(
        routes=[
            Route("/phone/turn", turn, methods=["POST"]),
            Route("/phone/result", turn, methods=["POST"]),
        ]
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    return BearerGuard(app, token)


def serve(ctx, args):
    from urllib.parse import urlsplit

    import uvicorn
    from agent.secret_scope import get_secret

    from .context import data_path, phone_context, require_enabled

    require_enabled(ctx)
    settings = Settings.read(phone_context(ctx).get_config)
    if not settings.bridge_enabled:
        raise PhoneError("Enable Phone conversation transport in plugin settings.")
    if not 1024 <= args.port <= 65535:
        raise PhoneError("Bridge port must be between 1024 and 65535.")
    hosts = ["127.0.0.1", "localhost"]
    if settings.bridge_public_url:
        public = origin(settings.bridge_public_url, service_http=True)
        hosts.append(urlsplit(public).hostname)
    app = create_app(
        BridgeStore(data_path()), get_secret("DOGRAH_BRIDGE_TOKEN"), allowed_hosts=hosts
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
