from __future__ import annotations

from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

from turn.server.security import LocalOnlyMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LocalOnlyMiddleware)

    @app.post("/mutate")
    async def mutate():
        return {"ok": True}

    @app.websocket("/terminal")
    async def terminal(socket: WebSocket):
        await socket.accept()
        await socket.send_text("ready")
        await socket.close()

    return app


def test_local_server_rejects_lan_hosts_and_cross_origin_mutations():
    app = _app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/mutate").status_code == 200
        assert client.post("/mutate", headers={"origin": "http://127.0.0.1"}).status_code == 200
        assert client.post("/mutate", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/mutate", headers={"origin": "http://127.0.0.1:9000"}).status_code == 403
        assert client.post("/mutate", headers={"host": "192.168.1.20:8000"}).status_code == 403


def test_terminal_websocket_rejects_cross_origin_and_lan_hosts():
    app = _app()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        with client.websocket_connect("/terminal", headers={"host": "127.0.0.1", "origin": "http://127.0.0.1"}) as socket:
            assert socket.receive_text() == "ready"
        for headers in ({"origin": "https://evil.example"}, {"host": "10.0.0.2:8000"}):
            try:
                with client.websocket_connect("/terminal", headers=headers):
                    raise AssertionError("cross-origin terminal unexpectedly connected")
            except Exception as exc:
                assert getattr(exc, "code", None) == 1008
                assert "loopback" in getattr(exc, "reason", "").lower()
