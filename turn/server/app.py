"""FastAPI application: lifespan wiring + UI mount."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from turn.config import settings
from turn.db.store import Store
from turn.runner.events import EventBus
from turn.runner.prefect_adapter import get_execution_adapter
from turn.runner.runner import Runner
from turn.server import api
from turn.server.security import LocalOnlyMiddleware
from turn.workers.registry import build_registry

UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"
ROOT_DIR = UI_DIR.parent
XTERM_DIR = ROOT_DIR / "node_modules" / "@xterm" / "xterm"
XTERM_FIT_DIR = ROOT_DIR / "node_modules" / "@xterm" / "addon-fit"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(settings.database_url)
    await store.init()
    # Restore durable preferences before constructing adapters and the runner.
    casts = {
        "max_retries": int,
        "retry_backoff_ms": int,
        "delay_between_jobs_ms": int,
        "timeout_seconds": float,
        "stall_timeout_seconds": float,
        "force_sequential": lambda v: str(v).lower() in ("1", "true", "yes"),
        "retry_choked_models": lambda v: str(v).lower() in ("1", "true", "yes"),
    }
    targets = {
        "max_retries": "max_retries",
        "retry_backoff_ms": "retry_backoff_ms",
        "delay_between_jobs_ms": "delay_between_jobs_ms",
        "timeout_seconds": "default_run_timeout_seconds",
        "stall_timeout_seconds": "stall_timeout_seconds",
        "force_sequential": "force_sequential",
        "retry_choked_models": "retry_choked_models",
    }
    for key, cast in casts.items():
        raw = await store.get_setting(key)
        if raw is not None:
            setattr(settings, targets[key], cast(raw))
    stored_harness = await store.get_setting("default_harness")
    if stored_harness:
        settings.default_executor = stored_harness
    stored_model = await store.get_setting("default_model")
    if stored_model:
        settings.codex_model = stored_model
    stored_reasoning = await store.get_setting("reasoning")
    if stored_reasoning:
        settings.default_reasoning = stored_reasoning
    stored_permission = await store.get_setting("permission")
    if stored_permission:
        settings.default_permission = stored_permission
    stored_auto_accept = await store.get_setting("auto_accept_merges")
    if stored_auto_accept is not None:
        settings.auto_accept_merges = str(stored_auto_accept).lower() in ("1", "true", "yes")
    events = EventBus()
    adapter = get_execution_adapter(settings)
    registry = build_registry(settings)
    runner = Runner(store, registry, events, settings, adapter)

    app.state.store = store
    app.state.events = events
    app.state.runner = runner

    await runner.start()
    try:
        yield
    finally:
        await runner.stop()
        await store.dispose()


app = FastAPI(title="Turn", version="0.1.0", lifespan=lifespan)
app.add_middleware(LocalOnlyMiddleware)
app.include_router(api.router)

if XTERM_DIR.exists():
    app.mount("/vendor/xterm", StaticFiles(directory=str(XTERM_DIR)), name="xterm")
if XTERM_FIT_DIR.exists():
    app.mount("/vendor/xterm-fit", StaticFiles(directory=str(XTERM_FIT_DIR)), name="xterm-fit")
if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
