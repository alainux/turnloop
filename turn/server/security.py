"""Local-only browser boundary for the single-user MVP."""
from __future__ import annotations

import json
from urllib.parse import urlsplit


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testserver"}


def _authority(value: str, *, origin: bool = False) -> tuple[str | None, int | None, str | None]:
    try:
        parsed = urlsplit(value if origin else f"//{value}")
        return ((parsed.hostname or "").lower() or None, parsed.port, parsed.scheme or None)
    except ValueError:
        return (None, None, None)


class LocalOnlyMiddleware:
    """Reject LAN hosts and cross-origin browser/terminal requests.

    Turn has no remote authentication in current scope. Binding uvicorn to a
    broad interface must therefore not silently turn native dialogs, project
    mutations, or PTY input into a network API.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        host, host_port, _ = _authority(headers.get("host", ""))
        origin_value = headers.get("origin")
        origin, origin_port, origin_scheme = _authority(origin_value, origin=True) if origin_value else (None, None, None)
        request_scheme = "https" if scope.get("scheme") in {"https", "wss"} else "http"
        same_origin = origin_value is None or (origin == host and origin_port == host_port and origin_scheme == request_scheme)
        allowed = host in LOOPBACK_HOSTS and same_origin
        if allowed:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": "Turn accepts loopback origins only"})
            return
        body = json.dumps({"detail": "Turn accepts loopback hosts and origins only"}).encode()
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
