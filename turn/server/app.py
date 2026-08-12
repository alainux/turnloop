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
from turn.workers.registry import build_registry

UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(settings.database_url)
    await store.init()
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
app.include_router(api.router)

if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
