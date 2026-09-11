"""Native client for authenticated shared Talk task routes; no local permission inference."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


class NativeTaskError(RuntimeError):
    pass


class NativeTaskAPI:
    def __init__(self, origin, *, session_token="", talk_token="", client=None):
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise NativeTaskError("Task API must be an explicit HTTP(S) origin without credentials")
        self.origin = origin.rstrip("/") + "/api/plugins/hermes-talk"
        headers = {}
        if session_token:
            headers["X-Hermes-Session-Token"] = session_token
        if talk_token:
            headers["X-Talk-Token"] = talk_token
        self._owned_client = client is None
        self.client = client or httpx.AsyncClient(
            headers=headers, timeout=30, follow_redirects=False
        )
        self.headers = headers
        self.context = None
        self.closed = False

    async def request(self, path, body=None, *, bound=True, method="POST", params=None):
        if self.closed:
            raise NativeTaskError("Task connection is closed")
        context = dict(self.context or {}) if bound else {}
        if bound and not context:
            raise NativeTaskError("Task is not attached")
        try:
            response = await self.client.request(
                method,
                self.origin + path,
                headers=self.headers,
                json={**(body or {}), **context} if method == "POST" else None,
                params={**(params or {}), **context} if method == "GET" else None,
            )
        except httpx.HTTPError:
            raise NativeTaskError(
                "Task service is unavailable; original work remains owned"
            ) from None
        if self.closed or (bound and self.context != context):
            raise NativeTaskError("Task response belongs to an older connection")
        if not response.is_success:
            raise NativeTaskError(f"Task service refused the request (HTTP {response.status_code})")
        if len(response.content) > 4 * 1024 * 1024:
            raise NativeTaskError("Task response exceeds the supported size")
        try:
            result = response.json()
        except ValueError:
            raise NativeTaskError("Task service returned invalid data") from None
        if not isinstance(result, dict):
            raise NativeTaskError("Task service returned invalid data")
        return result

    async def catalog(self, *, peer_id="local", profile="default"):
        return await self.request("/targets", {"peer_id": peer_id, "profile": profile}, bound=False)

    async def attach(
        self, *, target_id=None, tab_id=None, back=False, reference=None, peer_id=None, profile=None
    ):
        initial = self.context is None
        if initial:
            body = {"target_id": target_id, "tab_id": tab_id}
        else:
            body = (
                {"back": True}
                if back
                else {"target_id": target_id}
                if target_id
                else {"reference": reference, "peer_id": peer_id, "profile": profile}
            )
            body = {key: value for key, value in body.items() if value is not None}
        result = await self.request("/native/attach", body, bound=not initial)
        if result.get("ok") is not True:
            return result
        task = result.get("task") or {}
        if (
            not isinstance(task.get("connection_id"), str)
            or type(task.get("generation")) is not int
            or not isinstance(result.get("instructions"), str)
            or not isinstance(result.get("tools"), list)
        ):
            raise NativeTaskError("Invalid task attachment receipt")
        self.context = {"connection_id": task["connection_id"], "generation": task["generation"]}
        return result

    async def result(self, run_id):
        return await self.request("/result", method="GET", params={"run_id": run_id})

    async def close(self):
        if self.closed:
            return
        try:
            if self.context:
                await self.request("/close")
        except NativeTaskError:
            pass
        finally:
            self.closed = True
            if self._owned_client:
                await self.client.aclose()
