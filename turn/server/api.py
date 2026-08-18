"""REST + SSE API for Turn.

The server is a thin boundary: it loads/saves the workgraph through the Store
and drives the Runner for actions. All streaming goes through the EventBus.
"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
import re
import shutil
import uuid
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from turn.db.store import Store
from turn.capabilities.catalog import CapabilityCatalog
from turn.config import REAL_HARNESSES
from turn.domain.schemas import (
    AgentConfig,
    GraphView,
    InputSpec,
    Node,
    NodeStatus,
    RunPolicy,
)
from turn.workers.capabilities import capability_is_installed
from turn.domain.state_machine import present_node
from turn.graph.logic import GraphWalker, derive_flow_edges
from turn.contracts.schema import public_schema
from turn.runner.runner import Runner
from turn.workers.conversations import (
    ConversationProgress,
    cleanup_conversations,
    conversation_refs,
)


router = APIRouter()


def _capability_catalog(request: Request | None = None) -> CapabilityCatalog:
    from turn.config import settings

    store = getattr(getattr(request, "app", None), "state", None)
    store = getattr(store, "store", None)
    data_dir = getattr(store, "data_dir", Path(settings.data_dir))
    return CapabilityCatalog(Path(data_dir) / "capabilities")


@router.get("/api/schema")
async def schema():
    """Serve the domain contract consumed by generated web clients."""
    return public_schema()


@router.get("/api/capability-catalog")
async def capability_catalog(request: Request, query: str = ""):
    """Fuzzy-search the local portable capability catalog."""
    catalog = _capability_catalog(request)
    return {"capabilities": [entry.as_dict() for entry in catalog.search(query)]}


@router.get("/api/capability-catalog/{capability_id}/files/{file_path:path}")
async def capability_file(capability_id: str, file_path: str, request: Request):
    catalog = _capability_catalog(request)
    try:
        package = catalog.get(capability_id)
    except ValueError as error:
        raise HTTPException(404, "capability not found") from error
    candidate = (package.path / file_path).resolve()
    try:
        candidate.relative_to(package.path.resolve())
    except ValueError as error:
        raise HTTPException(404, "capability file not found") from error
    if not candidate.is_file():
        raise HTTPException(404, "capability file not found")
    return FileResponse(candidate)


@router.get("/api/capability-catalog/{capability_id}")
async def capability_detail(capability_id: str, request: Request):
    catalog = _capability_catalog(request)
    try:
        package = catalog.get(capability_id)
    except ValueError as error:
        raise HTTPException(404, "capability not found") from error
    return {
        "id": package.id,
        "version": package.version,
        "description": package.description,
        "path": str(package.path),
        "skills": [
            {"name": item.name, "description": item.description, "path": str(item.path),
             "source": f"/api/capability-catalog/{package.id}/files/skills/{item.name}/SKILL.md"}
            for item in package.skills
        ],
        "mcps": [{"name": item.name, "config": item.config} for item in package.mcp_servers],
    }


# -- request bodies --------------------------------------------------------


class CreateProject(BaseModel):
    prompt: str
    name: Optional[str] = None
    # "create" and "open" both use the supplied directory directly. Version
    # control is intentionally outside Turn's MVP scope.
    mode: Optional[str] = "create"
    # Working directory assigned to every worker in the project.
    # When omitted, a directory is made under TURN_PROJECTS_DIR.
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


class SetMode(BaseModel):
    auto_run: bool


class SettingsUpdate(BaseModel):
    default_auto_run: Optional[bool] = None
    theme: Optional[str] = None
    density: Optional[str] = None
    timeout_seconds: Optional[float] = Field(default=None, gt=0, le=86400)
    stall_timeout_seconds: Optional[float] = Field(default=None, gt=0, le=3600)
    max_retries: Optional[int] = Field(default=None, ge=0, le=20)
    retry_backoff_ms: Optional[int] = Field(default=None, ge=0, le=600000)
    delay_between_jobs_ms: Optional[int] = Field(default=None, ge=0, le=600000)
    retry_choked_models: Optional[bool] = None
    agent_defaults: Optional[dict[str, dict[str, str]]] = None


class BranchAction(BaseModel):
    action: str


class ProjectPolicyUpdate(BaseModel):
    run_policy: RunPolicy


class RenameProject(BaseModel):
    name: str = Field(min_length=1, max_length=72)


class DeleteProjectOptions(BaseModel):
    """Explicit opt-ins for destructive project deletion side effects."""

    delete_files: bool = False
    delete_conversations: bool = False


def _validate_served_agent(agent: AgentConfig, request: Request) -> None:
    if (
        agent.harness.value not in REAL_HARNESSES
        and not getattr(request.app.state, "test_mode", False)
    ):
        raise HTTPException(
            422,
            f"harness '{agent.harness.value}' is test-only and is not available in the served app",
        )
    from turn.workers.harnesses import validate_agent_capabilities

    try:
        validate_agent_capabilities(agent)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


# -- helpers ---------------------------------------------------------------


def _dump(n: Node):
    return n.model_dump(mode="json")


async def _serialize_graph(store: Store, project_id: uuid.UUID, runner: Runner | None = None) -> dict:
    nodes, edges, artifacts = await store.get_workgraph(project_id)
    walker = GraphWalker(nodes, edges)
    ev = walker.evaluate()
    for n in nodes:
        n.progress = ev.progress.get(n.id)
    root = next((n for n in nodes if n.id == project_id), None)
    serialized = []
    project_root = store.project_path(project_id)
    catalog = CapabilityCatalog(store.data_dir / "capabilities")
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
        generation_active = bool(
            runner is not None and runner.generation_active(n.id)
        )
        p = present_node(
            effective,
            blocked_reason=ev.blocked_reason.get(n.id),
            preparing=generation_active,
        )
        item = _dump(n)
        item["ui_state"] = p.state.value
        item["allowed_actions"] = [a.value for a in p.actions]
        item["state_reason"] = p.reason
        # A node shell and an agent harness share a persistent Herdr pane.
        # Only a runner-owned provider task is generation; an open user shell
        # must not animate a completed node as if the agent were still working.
        item["generation_active"] = generation_active
        statuses = []
        if n.agent is not None and project_root is not None:
            for capability_id in n.agent.capabilities:
                try:
                    package = catalog.resolve_project(capability_id, project_root)
                    loaded = True
                    skill_count = package.skill_count
                    mcp_count = package.mcp_count
                except ValueError:
                    loaded = False
                    skill_count = 0
                    mcp_count = 0
                statuses.append({
                    "capability_id": capability_id,
                    "skills": skill_count,
                    "mcps": mcp_count,
                    "loaded": loaded,
                    "installed": loaded and capability_is_installed(capability_id, n.agent.harness, project_root),
                })
        item["capability_status"] = statuses
        serialized.append(item)
    return GraphView.model_validate({
        "project_id": str(project_id),
        "nodes": serialized,
        "edges": [e.model_dump(mode="json") for e in edges],
        "flow_edges": [
            edge.model_dump(mode="json")
            for edge in derive_flow_edges(nodes, edges, ev.status)
        ],
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
    }).model_dump(mode="json")


# -- projects --------------------------------------------------------------


@router.post("/api/projects")
async def create_project(body: CreateProject, request: Request):
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    from turn.workers.filesystem import init_project_directory

    mode = (body.mode or "create").lower()
    if body.agent is not None:
        _validate_served_agent(body.agent, request)

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

    # Each project gets its own assigned directory. Turn never initializes or
    # manages version control in that directory.
    root_id = uuid.uuid4()
    try:
        repo_path = init_project_directory(
            root_id,
            working_dir=body.working_dir,
            projects_dir=runner.s.projects_dir,
        )
    except Exception as e:
        raise HTTPException(400, f"could not initialize project directory: {e}")

    if body.agent is None:
        from turn.config import settings as app_settings
        from turn.domain.schemas import HarnessKind, ReasoningLevel
        defaults = app_settings.agent_defaults["planner"]

        try:
            harness = HarnessKind(defaults["harness"])
        except ValueError as error:
            raise HTTPException(500, "stored default harness is not supported by the served app") from error
        if harness.value not in REAL_HARNESSES:
            raise HTTPException(500, "stored default harness is test-only and cannot be used by the served app")
        try:
            reasoning = ReasoningLevel(defaults["reasoning"])
        except ValueError as error:
            raise HTTPException(500, "stored reasoning level is not supported") from error
        body.agent = AgentConfig(
            harness=harness,
            model=defaults["model"] or None,
            reasoning=reasoning,
        )
        _validate_served_agent(body.agent, request)
    if body.run_policy is None:
        from turn.config import settings as app_settings

        body.run_policy = RunPolicy(
            auto_run=str(await store.get_setting("default_auto_run", "0")).lower() not in ("0", "false", ""),
            delay_between_jobs_ms=app_settings.delay_between_jobs_ms,
            timeout_seconds=app_settings.default_run_timeout_seconds,
            max_retries=app_settings.max_retries,
            retry_backoff_ms=app_settings.retry_backoff_ms,
            retry_choked_models=app_settings.retry_choked_models,
        )
    root = await store.create_project(
        body.prompt, name=body.name, repo_path=repo_path, id=root_id,
        agent=body.agent, run_policy=body.run_policy,
    )
    # A project starts with its durable Herdr shell already allocated. Opening
    # the inspector later only attaches to this shell; it never creates a new
    # terminal or starts a harness by itself.
    await runner.ensure_node_terminal(root.id)
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
        root = await store.set_resource_refs(root.id, refs) or root
    runner.wake()
    return {
        "project_id": str(root.id),
        "root": _dump(root),
        "repo_path": repo_path,
        "mode": mode,
    }


@router.get("/api/settings")
async def get_settings(request: Request):
    """Return cross-project preferences (e.g. the default auto-run mode)."""
    store: Store = request.app.state.store
    raw = await store.get_setting("default_auto_run", "0")
    default_auto_run = str(raw) not in ("0", "false", "False", "")
    from turn.config import settings as app_settings
    keys = {"theme": "dark", "density": "comfortable"}
    persisted = {k: await store.get_setting(k, v) for k, v in keys.items()}
    return {
        "default_auto_run": default_auto_run,
        **persisted,
        "agent_defaults": app_settings.agent_defaults,
        "timeout_seconds": app_settings.default_run_timeout_seconds,
        "stall_timeout_seconds": app_settings.stall_timeout_seconds,
        "max_retries": app_settings.max_retries,
        "retry_backoff_ms": app_settings.retry_backoff_ms,
        "delay_between_jobs_ms": app_settings.delay_between_jobs_ms,
        "retry_choked_models": app_settings.retry_choked_models,
        "projects_dir": str(Path(app_settings.projects_dir).resolve()),
    }


@router.post("/api/settings")
async def update_settings(body: SettingsUpdate, request: Request):
    """Persist a cross-project preference (e.g. the default auto-run mode)."""
    store: Store = request.app.state.store
    from turn.config import settings as app_settings, test_modes_enabled
    from turn.workers.harnesses import reasoning_levels_for
    if body.agent_defaults is not None:
        expected_roles = {"planner", "executor", "integrator", "verifier"}
        if set(body.agent_defaults) != expected_roles:
            raise HTTPException(422, "agent_defaults must define planner, executor, integrator, and verifier")
        normalized_defaults: dict[str, dict[str, str]] = {}
        for role, value in body.agent_defaults.items():
            required = {"harness", "model", "reasoning"}
            if not required.issubset(value):
                raise HTTPException(422, f"agent_defaults.{role} must define harness, model, and reasoning")
            if value["harness"] not in REAL_HARNESSES and not test_modes_enabled():
                raise HTTPException(422, f"agent_defaults.{role}.harness is not available in the served app")
            supported = reasoning_levels_for(value["harness"], value["model"] or None)
            if value["reasoning"] not in supported:
                raise HTTPException(422, f"agent_defaults.{role}.reasoning is not supported by {value['harness']} model '{value['model'] or 'default'}'")
            normalized_defaults[role] = {
                "harness": value["harness"],
                "model": value["model"],
                "reasoning": value["reasoning"],
            }
        await store.set_setting("agent_defaults", json.dumps(normalized_defaults, sort_keys=True))
        app_settings.agent_defaults = normalized_defaults
    if body.default_auto_run is not None:
        await store.set_setting("default_auto_run", "1" if body.default_auto_run else "0")
    for key in ("theme", "density"):
        value = getattr(body, key)
        if value is not None:
            await store.set_setting(key, str(value))
    live_fields = {
        "timeout_seconds": "default_run_timeout_seconds",
        "stall_timeout_seconds": "stall_timeout_seconds",
        "max_retries": "max_retries",
        "retry_backoff_ms": "retry_backoff_ms",
        "delay_between_jobs_ms": "delay_between_jobs_ms",
        "retry_choked_models": "retry_choked_models",
    }
    for incoming, target in live_fields.items():
        value = getattr(body, incoming)
        if value is not None:
            setattr(app_settings, target, value)
            await store.set_setting(incoming, str(value))
    return {"ok": True}


@router.get("/api/capabilities")
async def capabilities(request: Request):
    harnesses = getattr(request.app.state, "capabilities", None)
    if harnesses is None:
        raise HTTPException(503, "capabilities are not initialized; start the Turn server runtime")
    return {
        "harnesses": harnesses,
        "agent_types": [
            {"id": "planner", "label": "Planner"},
            {"id": "executor", "label": "Executor"},
            {"id": "integrator", "label": "Integrator"},
            {"id": "verifier", "label": "Verifier"},
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
        await runner.close_project_workspace(project.id)
    await store.clear_projects()
    runner.wake()
    return {"ok": True}


def _project_directory_for_deletion(
    store: Store, project_id: uuid.UUID, repo_path: str | None,
) -> Path:
    """Resolve exactly one validated project root, never a provider data store."""
    raw_path = repo_path or (str(store.project_path(project_id)) if store.project_path(project_id) else None)
    if not raw_path:
        raise RuntimeError("project directory is not known")
    candidate = Path(raw_path).expanduser()
    if candidate.is_symlink():
        raise RuntimeError("refusing to delete a symlink used as a project directory")
    resolved = candidate.resolve()
    protected = {
        Path(resolved.anchor),
        Path.home().resolve(),
        store.data_dir.resolve(),
        store.projects_dir.resolve(),
    }
    if resolved in protected:
        raise RuntimeError(f"refusing to delete protected directory {resolved}")
    if resolved.exists() and not resolved.is_dir():
        raise RuntimeError(f"project path is not a directory: {resolved}")
    return resolved


async def _delete_project_directory(path: Path) -> None:
    """Remove the already validated project root."""
    if not path.exists():
        return
    try:
        await asyncio.to_thread(shutil.rmtree, path)
    except FileNotFoundError:
        # An external cleanup may have won the race after validation. The
        # requested filesystem state is already satisfied in that case.
        return


@router.delete("/api/projects/{project_id}")
async def delete_project(
    project_id: str,
    request: Request,
    body: DeleteProjectOptions | None = None,
):
    runner: Runner = request.app.state.runner
    pid = uuid.UUID(project_id)
    if not runner.begin_project_deletion(pid):
        raise HTTPException(409, "project deletion is already in progress")
    try:
        return await _delete_project(pid, project_id, request, body)
    finally:
        runner.end_project_deletion(pid)


async def _delete_project(
    pid: uuid.UUID,
    project_id: str,
    request: Request,
    body: DeleteProjectOptions | None,
):
    store: Store = request.app.state.store
    runner: Runner = request.app.state.runner
    options = body or DeleteProjectOptions()
    root = await store.get_node(pid)
    if root is None or root.parent_id is not None:
        raise HTTPException(404, "project not found")

    # Capture provider session ids before cancellation. A manual stop keeps
    # the node's session until the user explicitly chooses Run, while project
    # deletion still needs the original provider id to invoke the harness's
    # public delete/archive command.
    nodes, _, _ = await store.get_workgraph(pid)
    runs = await store.get_project_runs(pid)
    refs = conversation_refs(nodes, runs)
    await runner.cancel_project_runs(pid)
    # Provider conversations may still be attached to Turn-owned Herdr panes
    # after their run tasks are cancelled. Close the project workspace before
    # invoking the harness lifecycle command so providers can release those
    # sessions through their public delete/archive surface.
    await runner.close_project_workspace(pid)

    cleanup = None
    if options.delete_conversations:
        async def report(progress: ConversationProgress) -> None:
            await request.app.state.events.publish({
                "type": "project.deletion_progress",
                "project_id": project_id,
                "phase": "conversations",
                "completed": progress.completed,
                "total": progress.total,
                "harness": progress.harness,
                "status": progress.status,
                "message": progress.message,
            })

        cleanup = await cleanup_conversations(
            refs,
            cwd=Path(root.repo_path).expanduser() if root.repo_path else store.project_path(pid),
            commands=runner.harness_commands,
            on_progress=report,
        )
        await request.app.state.events.publish({
            "type": "project.deletion_progress",
            "project_id": project_id,
            "phase": "conversations",
            "completed": cleanup.total,
            "total": cleanup.total,
            "status": "complete" if cleanup.ok else "failed",
            "message": (
                f"Conversation cleanup complete: {cleanup.deleted} deleted, "
                f"{cleanup.archived} archived"
                if cleanup.ok
                else "Conversation cleanup did not complete"
            ),
        })
        if not cleanup.ok:
            await request.app.state.events.publish({
                "type": "project.deletion_failed",
                "project_id": project_id,
                "phase": "conversations",
                "message": "Conversation cleanup failed; the project was not removed.",
                "cleanup": cleanup.as_dict(),
            })
            raise HTTPException(
                status_code=409,
                detail="Conversation cleanup failed; the project was not removed.",
            )

    if options.delete_files:
        try:
            project_directory = _project_directory_for_deletion(store, pid, root.repo_path)
        except (OSError, RuntimeError) as error:
            raise HTTPException(409, f"Project files could not be deleted: {error}") from error
        await request.app.state.events.publish({
            "type": "project.deletion_progress",
            "project_id": project_id,
            "phase": "files",
            "completed": 0,
            "total": 1,
            "status": "deleting",
            "message": "Deleting project files",
        })
        try:
            await _delete_project_directory(project_directory)
        except (OSError, RuntimeError) as error:
            try:
                await runner.ensure_node_terminal(pid)
            except Exception:
                pass
            raise HTTPException(409, f"Project files could not be deleted: {error}") from error
        await request.app.state.events.publish({
            "type": "project.deletion_progress",
            "project_id": project_id,
            "phase": "files",
            "completed": 1,
            "total": 1,
            "status": "deleted",
            "message": "Project files deleted",
        })
    await store.delete_project(pid)
    await request.app.state.events.publish({
        "type": "project.deleted",
        "project_id": project_id,
        "data": {
            "delete_files": options.delete_files,
            "delete_conversations": options.delete_conversations,
            "conversation_cleanup": cleanup.as_dict() if cleanup is not None else None,
        },
    })
    runner.wake()
    return {
        "ok": True,
        "delete_files": options.delete_files,
        "delete_conversations": options.delete_conversations,
        "conversation_cleanup": cleanup.as_dict() if cleanup is not None else None,
    }


@router.patch("/api/projects/{project_id}")
async def rename_project(project_id: str, body: RenameProject, request: Request):
    """Rename the navigation identity without rewriting the project intent."""
    store: Store = request.app.state.store
    node = await store.get_node(uuid.UUID(project_id))
    if node is None or node.parent_id is not None:
        raise HTTPException(404, "project not found")
    # Keep the authored objective and generated prompt intact. The explicit
    # project_name is the only user override; when it is absent the UI may use
    # the planner-scoped document title.
    node = await store.rename_project(uuid.UUID(project_id), body.name) or node
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
    await store.set_project_policy(uuid.UUID(project_id), body.run_policy)
    request.app.state.runner.wake()
    return {"ok": True}


@router.get("/api/projects/{project_id}/usage")
async def project_usage(project_id: str, request: Request):
    store: Store = request.app.state.store
    pid = uuid.UUID(project_id)
    nodes, edges, _ = await store.get_workgraph(pid)
    walker = GraphWalker(nodes, edges)
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
    by_branch = {}
    for node in nodes:
        branch = {**totals, "runs": 0}
        branch.update({key: 0 for key in totals})
        member = str(node.id)
        for candidate_id, usage in by_node.items():
            if candidate_id == member or any(
                str(ancestor.id) == member
                for ancestor in walker.ancestors(uuid.UUID(candidate_id))
            ):
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
    """Manual mode: execute the next runnable DAG stage in parallel."""
    runner = await _runner(request)
    node_ids = await runner.step(uuid.UUID(project_id))
    return {"ok": bool(node_ids), "stepped": [str(node_id) for node_id in node_ids]}


@router.get("/api/projects/{project_id}/graph")
async def get_graph(project_id: str, request: Request):
    store: Store = request.app.state.store
    pid = uuid.UUID(project_id)
    return await _serialize_graph(store, pid, request.app.state.runner)


@router.get("/api/projects/{project_id}/concept-images/{image_path:path}")
async def get_concept_image(project_id: str, image_path: str, request: Request):
    """Serve only planner-declared project-local concept image files."""
    store: Store = request.app.state.store
    root = store.project_path(uuid.UUID(project_id))
    if root is None:
        raise HTTPException(status_code=404, detail="project not found")
    candidate = (root / image_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="concept image not found") from error
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="concept image not found")
    return FileResponse(candidate)


@router.get("/api/projects/{project_id}/documents/{document_path:path}")
async def get_project_document(project_id: str, document_path: str, request: Request):
    """Serve one project-relative linked document without allowing traversal."""
    store: Store = request.app.state.store
    root = store.project_path(uuid.UUID(project_id))
    if root is None:
        raise HTTPException(status_code=404, detail="project not found")
    candidate = (root / document_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="document not found") from error
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="document not found")
    media_type, _ = mimetypes.guess_type(candidate.name)
    return FileResponse(candidate, media_type=media_type or "text/plain")


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


async def _pty_socket(
    websocket: WebSocket,
    node_id: str,
    transport,
    cleanup: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Serve a raw PTY without interpreting or reformatting its bytes."""

    def input_bytes(message: dict) -> str | bytes:
        data = message.get("data") or ""
        if message.get("encoding") != "base64":
            return str(data)
        try:
            return base64.b64decode(str(data), validate=True)
        except (ValueError, TypeError):
            return b""

    async def sync_visible_pane() -> None:
        """Refresh the browser from Herdr after a pane-level scroll."""
        refresh_snapshot = getattr(
            transport, "refresh_snapshot_from_persistent_pane", None
        )
        if refresh_snapshot is None:
            return
        await refresh_snapshot(nid, source="visible")
        await websocket.send_json({"type": "snapshot", **transport.snapshot(nid)})

    await websocket.accept()
    nid = uuid.UUID(node_id)
    queue = transport.subscribe(nid)
    sender = None
    status_sender = None
    try:
        # Establish the terminal dimensions before replaying a full-screen
        # snapshot. The timeout keeps the endpoint compatible with simple
        # clients that only want a read-only transcript.
        first_message = None
        try:
            first_message = await asyncio.wait_for(websocket.receive_json(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        requested_resize = None
        if isinstance(first_message, dict):
            message_type = first_message.get("type")
            if message_type == "resize":
                requested_resize = (
                    max(40, min(500, int(first_message.get("cols") or 80))),
                    max(8, min(200, int(first_message.get("rows") or 24))),
                )
            elif message_type == "input":
                await transport.write(nid, input_bytes(first_message))
            elif message_type == "scroll":
                await transport.scroll(
                    nid,
                    str(first_message.get("direction") or "down"),
                    int(first_message.get("amount") or 1),
                )
                await sync_visible_pane()

        if requested_resize is not None:
            await transport.resize(nid, *requested_resize)
            # Give a full-screen CLI one event-loop turn to consume SIGWINCH
            # and repaint. Snapshot only after this so xterm never replays a
            # screen captured at the old 80x24 geometry before it is resized.
            await asyncio.sleep(0.1)

        # Persistent terminal backends may provide a canonical replay before
        # the browser receives its first snapshot. Other transports simply do
        # not implement it.
        refresh_snapshot = getattr(transport, "refresh_snapshot_from_persistent_pane", None)
        if refresh_snapshot is not None:
            await refresh_snapshot(nid)
        snapshot = transport.snapshot(nid)
        # All bytes currently in the subscription queue are included in the
        # snapshot. Drain only before the first await; bytes emitted during the
        # network send remain queued and are delivered exactly once afterward.
        while not queue.empty():
            queue.get_nowait()
        await websocket.send_json({"type": "snapshot", **snapshot})

        async def send_output():
            while True:
                chunk = await queue.get()
                if not chunk:
                    await websocket.send_json({"type": "status", "active": False})
                    return
                await websocket.send_json({"type": "output", "data": chunk})

        sender = asyncio.create_task(send_output())

        async def send_status():
            try:
                while True:
                    await asyncio.sleep(5)
                    await websocket.send_json({"type": "status", **transport.snapshot(nid)})
            except (WebSocketDisconnect, RuntimeError):
                return

        status_sender = asyncio.create_task(send_status())
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "input":
                await transport.write(nid, input_bytes(message))
            elif message.get("type") == "scroll":
                await transport.scroll(
                    nid,
                    str(message.get("direction") or "down"),
                    int(message.get("amount") or 1),
                )
                await sync_visible_pane()
            elif message.get("type") == "resize":
                await transport.resize(
                    nid,
                    max(40, min(500, int(message.get("cols") or 80))),
                    max(8, min(200, int(message.get("rows") or 24))),
                )
    except WebSocketDisconnect:
        pass
    finally:
        if sender is not None:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if status_sender is not None:
            status_sender.cancel()
            await asyncio.gather(status_sender, return_exceptions=True)
        transport.unsubscribe(nid, queue)
        if cleanup is not None:
            await cleanup()


@router.websocket("/api/nodes/{node_id}/terminal")
async def terminal_socket(websocket: WebSocket, node_id: str):
    """Expose the provider's raw PTY stream."""
    runner: Runner = websocket.scope["app"].state.runner
    await _pty_socket(websocket, node_id, runner.terminal)


@router.websocket("/api/nodes/{node_id}/shell")
async def shell_socket(websocket: WebSocket, node_id: str):
    """Open a standalone interactive shell in the assigned project directory."""
    runner: Runner = websocket.scope["app"].state.runner
    nid = uuid.UUID(node_id)
    if not await runner.open_shell(nid):
        await websocket.close(code=1011, reason="project directory unavailable")
        return
    async def cleanup_shell() -> bool:
        # A websocket disappearing is a detach, not an explicit close. This
        # keeps the Herdr-backed shell available for reconnects and server
        # restarts without allowing a stale browser tab to kill user work.
        # Only the last viewer is allowed to release the server-side PTY;
        # another tab or a short-lived diagnostic client must not interrupt
        # the terminal that remains visible to the user.
        if runner.shell.snapshot(nid).get("subscribers", 0) > 0:
            return False
        return await runner.detach_shell(nid)

    await _pty_socket(websocket, node_id, runner.shell, cleanup_shell)


@router.post("/api/nodes/{node_id}/shell/close")
async def close_shell(node_id: str, request: Request):
    """Explicitly close a user terminal and its persistent shell session."""
    runner: Runner = request.app.state.runner
    return {"ok": await runner.close_shell(uuid.UUID(node_id))}


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
    graph = await _serialize_graph(store, node.project_id, request.app.state.runner)
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
        _validate_served_agent(body.agent, request)
    runner = await _runner(request)
    await runner.edit_node(
        uuid.UUID(node_id),
        objective=body.objective,
        generated_prompt=body.generated_prompt,
        required_inputs=body.required_inputs,
        resource_refs=body.resource_refs,
        agent=body.agent,
    )
    return {"ok": True}


@router.post("/api/nodes/{node_id}/regenerate")
async def regenerate(node_id: str, request: Request):
    runner = await _runner(request)
    try:
        result = await runner.regenerate_descendants(uuid.UUID(node_id), fresh_session=True)
    except asyncio.CancelledError:
        # Stop intentionally cancels the request task that owns an in-flight
        # regeneration. The node transition is already persisted by Runner;
        # report that normal user action as a successful cancellation instead
        # of leaking an ASGI 500 to the browser.
        return {"ok": True, "cancelled": True}
    return {"ok": True, **result}


@router.post("/api/nodes/{node_id}/retry")
async def retry(node_id: str, request: Request):
    runner = await _runner(request)
    await runner.retry(uuid.UUID(node_id))
    nid = await runner.run_node(uuid.UUID(node_id))
    return {"ok": nid is not None, "ran": str(nid) if nid else None}


@router.post("/api/nodes/{node_id}/reconnect")
async def reconnect(node_id: str, request: Request):
    runner = await _runner(request)
    return {"ok": await runner.reconnect(uuid.UUID(node_id))}


@router.post("/api/nodes/{node_id}/terminal/close")
async def close_terminal(node_id: str, request: Request):
    runner = await _runner(request)
    return {"ok": await runner.close_provider_terminal(uuid.UUID(node_id))}


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
