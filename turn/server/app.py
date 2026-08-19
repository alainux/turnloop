"""FastAPI application: lifespan wiring + UI mount."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import uuid

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from turn.config import settings, test_modes_enabled
from turn.server import api
from turn.runtime import TurnRuntime
from turn.server.security import LocalOnlyMiddleware

UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
UI_DIST_DIR = UI_DIR / "dist"
ROOT_DIR = UI_DIR.parent
XTERM_DIR = ROOT_DIR / "node_modules" / "@xterm" / "xterm"
XTERM_FIT_DIR = ROOT_DIR / "node_modules" / "@xterm" / "addon-fit"


@asynccontextmanager
async def lifespan(app: FastAPI):
    herdr_adapter = None
    terminal_transport = None
    if test_modes_enabled() and os.getenv("TURN_TEST_HERDR_ADAPTER") == "mock":
        from turn.testing.mocks import MockHerdrAdapter

        herdr_adapter = MockHerdrAdapter()
    if test_modes_enabled() and os.getenv("TURN_TEST_TERMINAL") == "local":
        # A served Turn instance must never silently fall back to a process
        # local PTY.  Local transports are valid for isolated tests that
        # inject one into TurnRuntime, but they are not project workspaces:
        # closing the browser/server connection would otherwise leave the
        # user with a terminal that Herdr does not own.  Fail loudly so an
        # outdated launch command cannot create an orphan-prone server.
        raise RuntimeError(
            "TURN_TEST_TERMINAL=local is not supported by the served app; "
            "start Turn with Herdr or inject LocalPtyTransport in a test"
        )
    runtime = TurnRuntime(
        settings,
        herdr_adapter=herdr_adapter,
        terminal_transport=terminal_transport,
    )
    components = await runtime.start()
    app.state.runtime = runtime
    app.state.store = components.store
    app.state.events = components.events
    app.state.logs = components.logs
    app.state.runner = components.runner
    app.state.triggers = runtime.triggers
    app.state.capabilities = components.capabilities
    app.state.test_mode = components.test_mode
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(title="Turn", version="0.1.0", lifespan=lifespan)
app.add_middleware(LocalOnlyMiddleware)


@app.middleware("http")
async def log_application_requests(request, call_next):
    """Make request failures visible without changing the response path."""
    try:
        response = await call_next(request)
    except Exception as error:
        logs = getattr(request.app.state, "logs", None)
        if logs is not None:
            await logs.emit(
                _request_project_id(request),
                kind="application.error",
                action=f"{request.method} {request.url.path}",
                message=str(error),
                status="error",
                source="http",
            )
        raise
    if response.status_code >= 400:
        logs = getattr(request.app.state, "logs", None)
        if logs is not None:
            await logs.emit(
                _request_project_id(request),
                kind="application.error",
                action=f"{request.method} {request.url.path}",
                message=f"HTTP {response.status_code}",
                status="error",
                source="http",
            )
    return response


def _request_project_id(request):
    for part in request.url.path.split("/"):
        try:
            return str(uuid.UUID(part))
        except (ValueError, AttributeError):
            continue


app.include_router(api.router)

if XTERM_DIR.exists():
    app.mount("/vendor/xterm", StaticFiles(directory=str(XTERM_DIR)), name="xterm")
if XTERM_FIT_DIR.exists():
    app.mount("/vendor/xterm-fit", StaticFiles(directory=str(XTERM_FIT_DIR)), name="xterm-fit")
if (UI_DIR / "icons").exists():
    app.mount("/icons", StaticFiles(directory=str(UI_DIR / "icons")), name="icons")
if UI_DIST_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIST_DIR), html=True), name="ui")
