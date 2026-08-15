"""Integration coverage using ports, not live Herdr or provider processes."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI

from turn.config import Settings
from turn.contracts.dag import parse_plan, parse_result
from turn.db.store import Store
from turn.domain.schemas import AgentType, Executor, Node, Outcome, Planner
from turn.graph.logic import GraphWalker
from turn.runner.events import EventBus
from turn.server.api import router
from turn.server.runtime import TurnRuntime
from turn.tests.fakes import DeterministicExecutionAdapter, FakeHerdrAdapter
from turn.workers.echo_worker import EchoWorker
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry
from turn.workers.base import NodeExecutionContext, render_context_block


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before the timeout")
        await asyncio.sleep(0.01)


async def test_server_runtime_is_deterministic_with_replaced_ports(tmp_path):
    """Exercise create, plan, execute, UI delete, and external Herdr delete."""
    settings = Settings(
        data_dir=str(tmp_path / "turn-state"),
        projects_dir=str(tmp_path / "projects"),
        default_executor="echo",
        planner="heuristic",
    )
    herdr = FakeHerdrAdapter()
    execution = DeterministicExecutionAdapter()
    registry = WorkerRegistry()
    registry.register(EchoWorker())
    registry.register_planner(HeuristicPlanner("echo"))
    runtime = TurnRuntime(
        settings,
        registry=registry,
        execution_adapter=execution,
        herdr_adapter=herdr,
        test_mode=True,
    )
    components = await runtime.start()
    app = FastAPI()
    app.include_router(router)
    app.state.store = components.store
    app.state.runner = components.runner
    app.state.events = components.events
    app.state.test_mode = True

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={
                    "name": "Deterministic boundary project",
                    "prompt": "Build a deterministic integration fixture",
                    "agent": {"harness": "echo", "type_id": "executor"},
                    "run_policy": {"auto_run": False},
                },
            )
            assert created.status_code == 200, created.text
            contract = (await client.get("/api/schema")).json()
            assert {"Agent", "Node", "Edge", "PlanResult", "WorkerResult"} <= set(contract["models"])
            project_id = uuid.UUID(created.json()["project_id"])
            runner = components.runner
            workspace_id = runner.terminal.project_workspace_id(str(project_id))
            assert workspace_id in herdr.workspaces

            started = await client.post(f"/api/nodes/{project_id}/run")
            assert started.status_code == 200, started.text
            await _wait_for(
                lambda: len(components.store._states[project_id]["nodes"]) > 1
            )
            nodes, edges, _ = await components.store.get_workgraph(project_id)
            walker = GraphWalker(nodes, edges)
            assert walker.descendants(project_id)

            stepped = await client.post(f"/api/projects/{project_id}/step")
            assert stepped.status_code == 200, stepped.text
            await _wait_for(
                lambda: any(
                    node.status.value == "COMPLETE"
                    for node in components.store._states[project_id]["nodes"].values()
                    if node.id != project_id
                )
            )
            assert execution.workers == ["echo"]

            deleted = await client.delete(f"/api/projects/{project_id}")
            assert deleted.status_code == 200, deleted.text
            assert project_id not in components.store._states
            assert workspace_id not in herdr.workspaces

            external = await client.post(
                "/api/projects",
                json={
                    "prompt": "Verify the reverse lifecycle",
                    "agent": {"harness": "echo", "type_id": "executor"},
                    "run_policy": {"auto_run": False},
                },
            )
            external_id = uuid.UUID(external.json()["project_id"])
            external_workspace = runner.terminal.project_workspace_id(str(external_id))
            assert external_workspace is not None
            assert await herdr.close_workspace(external_workspace)
            await runner._reconcile_project_workspaces(await components.store.list_projects())
            assert await components.store.get_node(external_id) is None
    finally:
        await runtime.stop()


def test_dag_contract_codecs_reject_invalid_graphs_and_parse_deterministically():
    plan = parse_plan({
        "nodes": [{"key": "build", "objective": "Build", "generated_prompt": None,
                   "executor": "echo", "required_inputs": [], "resource_refs": [],
                   "parent_key": None, "depends_on": [], "plan": False}],
        "edges": [],
    })
    assert plan.nodes[0].key == "build"
    result = parse_result({"outcome": Outcome.COMPLETE, "summary": "ok"})
    assert result.outcome is Outcome.COMPLETE


def test_planner_and_executor_are_agent_types_with_filesystem_skills():
    planner = Planner()
    executor = Executor()
    assert planner.type_id is AgentType.PLANNER
    assert executor.type_id is AgentType.EXECUTOR
    assert planner.skills and all(Path(path).is_file() for path in planner.skills)
    assert executor.skills and all(Path(path).is_file() for path in executor.skills)
    planner_context = render_context_block(
        NodeExecutionContext(
            node=Node(project_id=uuid.uuid4(), objective="plan", agent=planner)
        )
    )
    assert "Turn planning skill" in planner_context
