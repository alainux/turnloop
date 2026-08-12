"""REST + SSE API for Turn.

The server is a thin boundary: it loads/saves the workgraph through the Store
and drives the Runner for actions. All streaming goes through the EventBus.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from turn.db.store import Store
from turn.domain.schemas import InputSpec, Node, NodeStatus
from turn.graph.logic import evaluate
from turn.runner.runner import Runner


router = APIRouter()


# -- request bodies --------------------------------------------------------


class CreateProject(BaseModel):
    prompt: str
    name: Optional[str] = None


class ProvideInput(BaseModel):
    input_id: str
    value: str


class EditNode(BaseModel):
    objective: Optional[str] = None
    generated_prompt: Optional[str] = None
    required_inputs: Optional[list[InputSpec]] = None
    resource_refs: Optional[list[str]] = None


class SetMode(BaseModel):
    auto_run: bool


class SettingsUpdate(BaseModel):
    default_auto_run: Optional[bool] = None


# -- helpers ---------------------------------------------------------------


def _dump(n: Node):
    return n.model_dump(mode="json")


async def _serialize_graph(store: Store, project_id: uuid.UUID) -> dict:
    nodes, edges, artifacts = await store.get_workgraph(project_id)
    ev = evaluate(nodes, edges)
    for n in nodes:
        n.progress = ev.progress.get(n.id)
    return {
        "project_id": str(project_id),
        "nodes": [_dump(n) for n in nodes],
        "edges": [e.model_dump(mode="json") for e in edges],
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
    }


# -- projects --------------------------------------------------------------


@router.post("/api/projects")
async def create_project(body: CreateProject, request: Request):
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    root = await store.create_project(body.prompt, name=body.name)
    runner.wake()
    return {"project_id": str(root.id), "root": _dump(root)}


@router.get("/api/settings")
async def get_settings(request: Request):
    """Return cross-project preferences (e.g. the default auto-run mode)."""
    store: Store = request.app.state.store
    raw = await store.get_setting("default_auto_run", "1")
    default_auto_run = str(raw) not in ("0", "false", "False", "")
    return {"default_auto_run": default_auto_run}


@router.post("/api/settings")
async def update_settings(body: SettingsUpdate, request: Request):
    """Persist a cross-project preference (e.g. the default auto-run mode)."""
    store: Store = request.app.state.store
    if body.default_auto_run is not None:
        await store.set_setting("default_auto_run", "1" if body.default_auto_run else "0")
    return {"ok": True}


@router.get("/api/projects")
async def list_projects(request: Request):
    store: Store = request.app.state.store
    roots = await store.list_projects()
    return {"projects": [_dump(r) for r in roots]}


@router.delete("/api/projects")
async def clear_projects(request: Request):
    """Remove all projects and their data (keeps cross-project settings)."""
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    await store.clear_projects()
    runner.wake()
    return {"ok": True}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    await store.delete_project(uuid.UUID(project_id))
    runner.wake()
    return {"ok": True}


@router.post("/api/projects/{project_id}/mode")
async def set_mode(project_id: str, body: SetMode, request: Request):
    """Toggle a project between auto-run and manual step mode."""
    runner = await _runner(request)
    await runner.set_mode(uuid.UUID(project_id), body.auto_run)
    return {"ok": True}


@router.post("/api/projects/{project_id}/step")
async def step_project(project_id: str, request: Request):
    """Manual mode: execute the next runnable node (one step)."""
    runner = await _runner(request)
    nid = await runner.step(uuid.UUID(project_id))
    return {"ok": nid is not None, "stepped": str(nid) if nid else None}


@router.get("/api/projects/{project_id}/graph")
async def get_graph(project_id: str, request: Request):
    store: Store = request.app.state.store
    pid = uuid.UUID(project_id)
    return await _serialize_graph(store, pid)


@router.get("/api/projects/{project_id}/stream")
async def stream(project_id: str, request: Request):
    events: object = request.app.state.events
    pid = project_id

    async def event_generator():
        q = events.subscribe()
        try:
            yield _sse({"type": "connected", "project_id": pid})
            while True:
                event = await q.get()
                if event.get("project_id") == pid or event.get("project_id") is None:
                    yield _sse(event)
        finally:
            events.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


# -- node detail -----------------------------------------------------------


@router.get("/api/nodes/{node_id}")
async def get_node(node_id: str, request: Request):
    store: Store = request.app.state.store
    nid = uuid.UUID(node_id)
    node = await store.get_node(nid)
    if node is None:
        raise HTTPException(404, "node not found")
    runs = await store.get_runs(nid)
    arts = await store.get_artifacts(nid)
    return {
        "node": _dump(node),
        "runs": [r.model_dump(mode="json") for r in runs],
        "artifacts": [a.model_dump(mode="json") for a in arts],
    }


# -- actions ---------------------------------------------------------------


async def _runner(request) -> Runner:
    return request.app.state.runner


@router.post("/api/nodes/{node_id}/provide-input")
async def provide_input(node_id: str, body: ProvideInput, request: Request):
    runner = await _runner(request)
    await runner.provide_input(uuid.UUID(node_id), body.input_id, body.value)
    return {"ok": True}


@router.post("/api/nodes/{node_id}/edit")
async def edit_node(node_id: str, body: EditNode, request: Request):
    runner = await _runner(request)
    await runner.edit_node(
        uuid.UUID(node_id),
        objective=body.objective,
        generated_prompt=body.generated_prompt,
        required_inputs=body.required_inputs,
        resource_refs=body.resource_refs,
    )
    return {"ok": True}


@router.post("/api/nodes/{node_id}/regenerate")
async def regenerate(node_id: str, request: Request):
    runner = await _runner(request)
    await runner.regenerate_descendants(uuid.UUID(node_id))
    return {"ok": True}


@router.post("/api/nodes/{node_id}/fork")
async def fork(node_id: str, request: Request):
    runner = await _runner(request)
    fork = await runner.fork(uuid.UUID(node_id))
    return {"ok": True, "fork_id": str(fork.id) if fork else None}


@router.post("/api/nodes/{node_id}/retry")
async def retry(node_id: str, request: Request):
    runner = await _runner(request)
    await runner.retry(uuid.UUID(node_id))
    return {"ok": True}


@router.post("/api/nodes/{node_id}/pause")
async def pause(node_id: str, request: Request):
    runner = await _runner(request)
    await runner.pause(uuid.UUID(node_id))
    return {"ok": True}


@router.post("/api/nodes/{node_id}/resume")
async def resume(node_id: str, request: Request):
    runner = await _runner(request)
    await runner.resume(uuid.UUID(node_id))
    return {"ok": True}


@router.post("/api/nodes/{node_id}/cancel")
async def cancel(node_id: str, request: Request):
    runner = await _runner(request)
    await runner.cancel(uuid.UUID(node_id))
    return {"ok": True}


@router.post("/api/nodes/{node_id}/run")
async def run_node(node_id: str, request: Request):
    """Manually execute a specific node (works in any mode)."""
    runner = await _runner(request)
    nid = await runner.run_node(uuid.UUID(node_id))
    return {"ok": nid is not None, "ran": str(nid) if nid else None}
