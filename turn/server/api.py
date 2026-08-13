"""REST + SSE API for Turn.

The server is a thin boundary: it loads/saves the workgraph through the Store
and drives the Runner for actions. All streaming goes through the EventBus.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from turn.db.store import Store
from turn.domain.schemas import AgentConfig, InputSpec, Node, NodeStatus, RunPolicy
from turn.domain.state_machine import present_node, review_blocked_ids
from turn.graph.logic import evaluate
from turn.runner.runner import Runner


router = APIRouter()


# -- request bodies --------------------------------------------------------


class CreateProject(BaseModel):
    prompt: str
    name: Optional[str] = None
    # "create" -> new empty project repo (or reuse the dir if it is already a
    # git repo); "open" -> use an EXISTING git repo (e.g. to refactor it).
    mode: Optional[str] = "create"
    # Working directory that becomes (or already is) the project's git root.
    # When omitted in create mode, a repo is made under TURN_PROJECTS_DIR.
    working_dir: Optional[str] = None
    agent: Optional[AgentConfig] = None
    run_policy: Optional[RunPolicy] = None
    attachments: list["ProjectAttachment"] = Field(default_factory=list)


class ProjectAttachment(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_base64: str
    mime: Optional[str] = None


class ProvideInput(BaseModel):
    input_id: str
    value: str


class EditNode(BaseModel):
    objective: Optional[str] = None
    generated_prompt: Optional[str] = None
    required_inputs: Optional[list[InputSpec]] = None
    resource_refs: Optional[list[str]] = None
    agent: Optional[AgentConfig] = None
    cascade_agent: bool = False


class ForkNode(BaseModel):
    objective: Optional[str] = None
    generated_prompt: Optional[str] = None


class SetMode(BaseModel):
    auto_run: bool


class SettingsUpdate(BaseModel):
    default_auto_run: Optional[bool] = None
    auto_accept_merges: Optional[bool] = None
    theme: Optional[str] = None
    density: Optional[str] = None
    default_harness: Optional[str] = None
    default_model: Optional[str] = None
    reasoning: Optional[str] = None
    permission: Optional[str] = None
    timeout_seconds: Optional[float] = Field(default=None, gt=0, le=86400)
    stall_timeout_seconds: Optional[float] = Field(default=None, gt=0, le=3600)
    max_retries: Optional[int] = Field(default=None, ge=0, le=20)
    retry_backoff_ms: Optional[int] = Field(default=None, ge=0, le=600000)
    delay_between_jobs_ms: Optional[int] = Field(default=None, ge=0, le=600000)
    force_sequential: Optional[bool] = None
    retry_choked_models: Optional[bool] = None


class BranchAction(BaseModel):
    action: str


class ProjectPolicyUpdate(BaseModel):
    run_policy: RunPolicy


class RenameProject(BaseModel):
    name: str = Field(min_length=1, max_length=72)


# -- helpers ---------------------------------------------------------------


def _dump(n: Node):
    return n.model_dump(mode="json")


async def _serialize_graph(store: Store, project_id: uuid.UUID) -> dict:
    nodes, edges, artifacts = await store.get_workgraph(project_id)
    ev = evaluate(nodes, edges)
    for n in nodes:
        n.progress = ev.progress.get(n.id)
    review_blocked = review_blocked_ids(nodes)
    root = next((n for n in nodes if n.id == project_id), None)
    review_owner = (
        "parent"
        if root and root.run_policy and root.run_policy.review_mode in ("parent", "auto_accept")
        else "manual"
    )
    serialized = []
    for n in nodes:
        effective_status = ev.status.get(n.id, n.status)
        # The root is not user-visible COMPLETE until final shipping has
        # persisted it. Graph evaluation can reach COMPLETE a tick earlier,
        # while the root working branch still awaits merge into the base.
        if (
            n.parent_id is None
            and effective_status == NodeStatus.COMPLETE
            and n.status != NodeStatus.COMPLETE
        ):
            effective_status = n.status
        effective = n.model_copy(update={"status": effective_status})
        node_review_owner = (
            "manual"
            if n.verification_status and n.verification_status.value == "error"
            else review_owner
        )
        p = present_node(
            effective,
            blocked_reason=ev.blocked_reason.get(n.id),
            subtree_needs_review=n.id in review_blocked and not n.needs_review,
            review_owner=node_review_owner,
        )
        item = _dump(n)
        item["ui_state"] = p.state.value
        item["allowed_actions"] = [a.value for a in p.actions]
        item["state_reason"] = p.reason
        item["review_owner"] = node_review_owner
        serialized.append(item)
    return {
        "project_id": str(project_id),
        "nodes": serialized,
        "edges": [e.model_dump(mode="json") for e in edges],
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
    }


# -- projects --------------------------------------------------------------


@router.post("/api/projects")
async def create_project(body: CreateProject, request: Request):
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    from turn.workers import worktree as wtmod

    mode = (body.mode or "create").lower()
    open_existing = mode == "open"
    if open_existing and not body.working_dir:
        raise HTTPException(400, "open mode requires a working_dir (an existing git repo)")
    if body.agent is not None:
        from turn.workers.harnesses import validate_agent_capabilities
        try:
            validate_agent_capabilities(body.agent)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    decoded_attachments: list[tuple[str, bytes]] = []
    used_names: set[str] = set()
    for attachment in body.attachments:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(attachment.name).name).strip(".-") or "attachment"
        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
        candidate = safe_name
        index = 2
        while candidate.lower() in used_names:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        used_names.add(candidate.lower())
        try:
            payload = base64.b64decode(attachment.content_base64, validate=True)
        except ValueError as error:
            raise HTTPException(422, f"invalid attachment encoding for {attachment.name}") from error
        if len(payload) > 10 * 1024 * 1024:
            raise HTTPException(413, f"attachment {attachment.name} exceeds 10 MB")
        decoded_attachments.append((candidate, payload))

    # Each project gets its OWN git repo so the user is left with a real,
    # initialized repository of their finished work (not a sub-worktree of the
    # Turn app). The repo path is recorded on the root node.
    root_id = uuid.uuid4()
    repo_path = None
    try:
        repo_path = wtmod.init_project_repo(
            root_id,
            working_dir=body.working_dir,
            open_existing=open_existing,
            projects_dir=runner.s.projects_dir,
        )
    except Exception as e:
        raise HTTPException(400, f"could not initialize project repo: {e}")

    if body.agent is None:
        from turn.config import settings as app_settings
        from turn.domain.schemas import HarnessKind, PermissionMode, ReasoningLevel

        try:
            harness = HarnessKind(await store.get_setting("default_harness", app_settings.default_executor))
        except ValueError:
            harness = HarnessKind.CODEX
        try:
            reasoning = ReasoningLevel(await store.get_setting("reasoning", app_settings.default_reasoning))
        except ValueError:
            reasoning = ReasoningLevel.DEFAULT
        try:
            permission = PermissionMode(await store.get_setting("permission", app_settings.default_permission))
        except ValueError:
            permission = PermissionMode.WORKSPACE
        body.agent = AgentConfig(
            harness=harness,
            model=(await store.get_setting("default_model", app_settings.codex_model or "")) or None,
            reasoning=reasoning,
            permission=permission,
        )
        from turn.workers.harnesses import reasoning_levels_for
        if body.agent.reasoning.value not in reasoning_levels_for(body.agent.harness, body.agent.model):
            body.agent.reasoning = ReasoningLevel.DEFAULT
    if body.run_policy is None:
        from turn.config import settings as app_settings

        body.run_policy = RunPolicy(
            auto_run=str(await store.get_setting("default_auto_run", "1")).lower() not in ("0", "false", ""),
            force_sequential=app_settings.force_sequential,
            delay_between_jobs_ms=app_settings.delay_between_jobs_ms,
            timeout_seconds=app_settings.default_run_timeout_seconds,
            max_retries=app_settings.max_retries,
            retry_backoff_ms=app_settings.retry_backoff_ms,
            retry_choked_models=app_settings.retry_choked_models,
            review_mode="parent" if app_settings.auto_accept_merges else "manual",
        )
    root = await store.create_project(
        body.prompt, name=body.name, repo_path=repo_path, id=root_id,
        agent=body.agent, run_policy=body.run_policy,
    )
    if decoded_attachments:
        attachment_dir = Path(repo_path) / ".turn" / "attachments"
        attachment_dir.mkdir(parents=True, exist_ok=True)
        refs: list[str] = []
        for candidate, payload in decoded_attachments:
            target = attachment_dir / candidate
            index = 2
            while target.exists():
                target = attachment_dir / f"{Path(candidate).stem}-{index}{Path(candidate).suffix}"
                index += 1
            target.write_bytes(payload)
            refs.append(str(target.resolve()))
        root.resource_refs = refs
        root = await store._save_node(root)
    runner.wake()
    return {
        "project_id": str(root.id),
        "root": _dump(root),
        "repo_path": repo_path,
        "mode": mode,
    }


@router.get("/api/projects/{project_id}/graph-inspections")
async def graph_inspections(project_id: str, request: Request):
    store: Store = request.app.state.store
    items = await store.get_graph_inspections(uuid.UUID(project_id))
    return {"project_id": project_id, "inspections": items}


@router.get("/api/settings")
async def get_settings(request: Request):
    """Return cross-project preferences (e.g. the default auto-run mode)."""
    store: Store = request.app.state.store
    raw = await store.get_setting("default_auto_run", "1")
    default_auto_run = str(raw) not in ("0", "false", "False", "")
    from turn.config import settings as app_settings
    keys = {
        "theme": "dark", "density": "comfortable", "default_harness": app_settings.default_executor,
        "default_model": app_settings.codex_model or "", "reasoning": app_settings.default_reasoning,
        "permission": app_settings.default_permission,
    }
    persisted = {k: await store.get_setting(k, v) for k, v in keys.items()}
    return {
        "default_auto_run": default_auto_run,
        "auto_accept_merges": bool(app_settings.auto_accept_merges),
        **persisted,
        "timeout_seconds": app_settings.default_run_timeout_seconds,
        "stall_timeout_seconds": app_settings.stall_timeout_seconds,
        "max_retries": app_settings.max_retries,
        "retry_backoff_ms": app_settings.retry_backoff_ms,
        "delay_between_jobs_ms": app_settings.delay_between_jobs_ms,
        "force_sequential": app_settings.force_sequential,
        "retry_choked_models": app_settings.retry_choked_models,
        "projects_dir": str(Path(app_settings.projects_dir).resolve()),
    }


@router.post("/api/settings")
async def update_settings(body: SettingsUpdate, request: Request):
    """Persist a cross-project preference (e.g. the default auto-run mode)."""
    store: Store = request.app.state.store
    from turn.config import settings as app_settings
    from turn.workers.harnesses import reasoning_levels_for
    effective_harness = body.default_harness or await store.get_setting("default_harness", app_settings.default_executor)
    effective_model = body.default_model if body.default_model is not None else await store.get_setting("default_model", app_settings.codex_model or "")
    effective_reasoning = body.reasoning or await store.get_setting("reasoning", app_settings.default_reasoning)
    supported = reasoning_levels_for(effective_harness, effective_model)
    if effective_reasoning not in supported:
        raise HTTPException(
            422,
            f"reasoning '{effective_reasoning}' is not supported by {effective_harness} model "
            f"'{effective_model or 'default'}'; choose one of: {', '.join(supported)}",
        )
    if body.default_auto_run is not None:
        await store.set_setting("default_auto_run", "1" if body.default_auto_run else "0")
    if body.auto_accept_merges is not None:
        await store.set_setting("auto_accept_merges", "1" if body.auto_accept_merges else "0")
        # Keep the runner's live view of the option in sync with the persisted value.
        app_settings.auto_accept_merges = bool(body.auto_accept_merges)
    for key in ("theme", "density", "default_harness", "default_model", "reasoning", "permission"):
        value = getattr(body, key)
        if value is not None:
            await store.set_setting(key, str(value))
    if body.default_harness is not None:
        app_settings.default_executor = body.default_harness
    if body.default_model is not None:
        app_settings.codex_model = body.default_model or None
    if body.reasoning is not None:
        app_settings.default_reasoning = body.reasoning
    if body.permission is not None:
        app_settings.default_permission = body.permission
    live_fields = {
        "timeout_seconds": "default_run_timeout_seconds",
        "stall_timeout_seconds": "stall_timeout_seconds",
        "max_retries": "max_retries",
        "retry_backoff_ms": "retry_backoff_ms",
        "delay_between_jobs_ms": "delay_between_jobs_ms",
        "force_sequential": "force_sequential",
        "retry_choked_models": "retry_choked_models",
    }
    for incoming, target in live_fields.items():
        value = getattr(body, incoming)
        if value is not None:
            setattr(app_settings, target, value)
            await store.set_setting(incoming, str(value))
    return {"ok": True}


@router.get("/api/capabilities")
async def capabilities():
    from turn.config import settings as app_settings
    from turn.workers.harnesses import harness_capabilities

    harnesses = harness_capabilities()
    if app_settings.codex_model:
        codex = next((item for item in harnesses if item["id"] == "codex"), None)
        if codex is not None:
            codex["models"] = [{"id": app_settings.codex_model, "label": app_settings.codex_model}]
    return {
        "harnesses": harnesses,
        "agent_types": [
            {"id": "planner", "label": "Planner"},
            {"id": "general", "label": "General agent"},
            {"id": "validator", "label": "Validator", "future": True},
        ],
        # This describes a registry seam, not selectable/rendered output types
        # in the current MVP. Keep that boundary machine-readable so clients
        # cannot accidentally present these as shipped controls.
        "output_types": [
            {"id": "artifact", "label": "Artifact", "future": True},
            {"id": "code", "label": "Code", "future": True},
            {"id": "document", "label": "Document", "future": True},
            {"id": "structured_data", "label": "Structured data", "future": True},
        ],
    }


@router.post("/api/system/pick-directory")
async def pick_directory():
    """Open the platform folder chooser through the replaceable native seam."""
    from turn.server.native import choose_directory

    path = await asyncio.to_thread(choose_directory)
    return {"path": path, "cancelled": path is None}


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
    for project in await store.list_projects():
        await runner.cancel_project_runs(project.id)
    await store.clear_projects()
    runner.wake()
    return {"ok": True}


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request):
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    pid = uuid.UUID(project_id)
    await runner.cancel_project_runs(pid)
    await store.delete_project(pid)
    runner.wake()
    return {"ok": True}


@router.patch("/api/projects/{project_id}")
async def rename_project(project_id: str, body: RenameProject, request: Request):
    """Rename the navigation identity without rewriting the project intent."""
    store: Store = request.app.state.store
    node = await store.get_node(uuid.UUID(project_id))
    if node is None or node.parent_id is not None:
        raise HTTPException(404, "project not found")
    node.project_name = body.name.strip()
    # The root card is the project identity; the authored intent remains
    # untouched in generated_prompt.
    node.objective = node.project_name
    node = await store._save_node(node)
    await request.app.state.events.publish(
        {"type": "node.updated", "project_id": str(node.project_id), "data": _dump(node)}
    )
    return {"ok": True, "project": _dump(node)}


@router.post("/api/projects/{project_id}/mode")
async def set_mode(project_id: str, body: SetMode, request: Request):
    """Toggle a project between auto-run and manual step mode."""
    runner = await _runner(request)
    await runner.set_mode(uuid.UUID(project_id), body.auto_run)
    return {"ok": True}


@router.post("/api/projects/{project_id}/policy")
async def set_project_policy(project_id: str, body: ProjectPolicyUpdate, request: Request):
    store: Store = request.app.state.store
    node = await store.get_node(uuid.UUID(project_id))
    if node is None:
        raise HTTPException(404, "project not found")
    node.run_policy = body.run_policy
    node.auto_run = body.run_policy.auto_run
    await store._save_node(node)
    request.app.state.runner.wake()
    return {"ok": True}


@router.get("/api/projects/{project_id}/usage")
async def project_usage(project_id: str, request: Request):
    store: Store = request.app.state.store
    pid = uuid.UUID(project_id)
    nodes, _, _ = await store.get_workgraph(pid)
    runs = await store.get_project_runs(pid)
    totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    by_node = {}
    for run in runs:
        row = by_node.setdefault(
            str(run.node_id),
            {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "runs": 0},
        )
        row["runs"] += 1
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = getattr(run.usage, key)
            row[key] += value
            totals[key] += value
        if run.usage.cost_usd is not None:
            row["cost_usd"] += run.usage.cost_usd
            totals["cost_usd"] += run.usage.cost_usd
    parent_of = {str(node.id): str(node.parent_id) if node.parent_id else None for node in nodes}
    by_branch = {}
    for node in nodes:
        branch = {**totals, "runs": 0}
        branch.update({key: 0 for key in totals})
        member = str(node.id)
        for candidate_id, usage in by_node.items():
            cursor = candidate_id
            while cursor is not None and cursor != member:
                cursor = parent_of.get(cursor)
            if cursor == member:
                branch["runs"] += usage["runs"]
                for key in totals:
                    branch[key] += usage[key]
        by_branch[member] = branch
    return {
        "totals": totals,
        "by_node": by_node,
        "by_branch": by_branch,
        "node_count": len(nodes),
        "run_count": len(runs),
    }


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
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    # A heartbeat gives the ASGI server a regular cancellation
                    # point and prevents an idle EventSource from blocking a
                    # clean application shutdown indefinitely.
                    yield ": keep-alive\n\n"
                    continue
                if event.get("project_id") == pid or event.get("project_id") is None:
                    yield _sse(event)
        finally:
            events.unsubscribe(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


@router.websocket("/api/nodes/{node_id}/terminal")
async def terminal_socket(websocket: WebSocket, node_id: str):
    """Bridge xterm to the active provider-neutral terminal transport."""
    await websocket.accept()
    runner: Runner = websocket.scope["app"].state.runner
    nid = uuid.UUID(node_id)
    queue = runner.terminal.subscribe(nid)
    snapshot = runner.terminal.snapshot(nid)
    await websocket.send_json({"type": "snapshot", **snapshot})

    async def send_output():
        while True:
            chunk = await queue.get()
            if not chunk:
                await websocket.send_json({"type": "status", "active": False})
                return
            await websocket.send_json({"type": "output", "data": chunk})

    sender = asyncio.create_task(send_output())
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "input":
                await runner.terminal.write(nid, str(message.get("data") or ""))
            elif message.get("type") == "resize":
                await runner.terminal.resize(
                    nid, int(message.get("cols") or 80), int(message.get("rows") or 24)
                )
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        runner.terminal.unsubscribe(nid, queue)


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
    graph = await _serialize_graph(store, node.project_id)
    enriched = next((n for n in graph["nodes"] if n["id"] == str(node.id)), _dump(node))
    return {
        "node": enriched,
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
    if body.agent is not None:
        from turn.workers.harnesses import validate_agent_capabilities
        try:
            validate_agent_capabilities(body.agent)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
    runner = await _runner(request)
    await runner.edit_node(
        uuid.UUID(node_id),
        objective=body.objective,
        generated_prompt=body.generated_prompt,
        required_inputs=body.required_inputs,
        resource_refs=body.resource_refs,
        agent=body.agent,
        cascade_agent=body.cascade_agent,
    )
    return {"ok": True}


@router.post("/api/nodes/{node_id}/regenerate")
async def regenerate(node_id: str, request: Request):
    runner = await _runner(request)
    result = await runner.regenerate_descendants(uuid.UUID(node_id))
    return {"ok": True, **result}


@router.post("/api/nodes/{node_id}/fork")
async def fork(node_id: str, request: Request, body: ForkNode | None = None):
    runner = await _runner(request)
    fork = await runner.fork(
        uuid.UUID(node_id),
        objective=body.objective if body else None,
        generated_prompt=body.generated_prompt if body else None,
    )
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


@router.post("/api/nodes/{node_id}/branch")
async def branch_action(node_id: str, body: BranchAction, request: Request):
    if body.action not in {"pause", "resume", "cancel"}:
        raise HTTPException(400, "branch action must be pause, resume, or cancel")
    runner = await _runner(request)
    await runner.branch_action(uuid.UUID(node_id), body.action)
    return {"ok": True}


@router.post("/api/nodes/{node_id}/run")
async def run_node(node_id: str, request: Request):
    """Manually execute a specific node (works in any mode)."""
    runner = await _runner(request)
    nid = await runner.run_node(uuid.UUID(node_id))
    return {"ok": nid is not None, "ran": str(nid) if nid else None}


class RejectBody(BaseModel):
    feedback: Optional[str] = None


@router.post("/api/nodes/{node_id}/accept")
async def accept_merge(node_id: str, request: Request):
    """Accept a merged node: keep the merged result (already in the parent)
    and delete this node's redundant subtree worktree to reclaim space."""
    runner = await _runner(request)
    await runner.accept_merge(uuid.UUID(node_id))
    return {"ok": True}


@router.post("/api/nodes/{node_id}/reject")
async def reject_merge(node_id: str, body: RejectBody, request: Request):
    """Reject a merged node: send feedback into the SAME node (no new node)
    and re-run it in place so it can correct its output."""
    runner = await _runner(request)
    await runner.reject_merge(uuid.UUID(node_id), body.feedback or "")
    return {"ok": True}
