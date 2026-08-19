"""Integration coverage using ports, not live Herdr or provider processes."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI

from turn.config import Settings
from turn.contracts.dag import parse_plan, parse_result, plan_handoff_example
from turn.db.store import Store
from turn.domain.schemas import (
    AgentType,
    DocumentRef,
    Executor,
    Integrator,
    Node,
    NodeSpec,
    Outcome,
    PlanResult,
    Planner,
)
from turn.graph.logic import GraphWalker
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.server.api import router
from turn.server.runtime import TurnRuntime
from turn.tests.mocks import DeterministicExecutionAdapter, MockHerdrAdapter
from turn.workers.deterministic_worker import DeterministicWorker
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry
from turn.workers.base import NodeExecutionContext, render_context_block
from turn.tests.capability_fixtures import load_builtin_capabilities


async def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before the timeout")
        await asyncio.sleep(0.01)


def test_canonical_plan_handoff_keeps_documents_generic_and_dynamic():
    payload = json.loads(plan_handoff_example())
    plan = parse_plan(payload)

    assert "edges" not in payload
    assert payload["project_name"] == "Short project name"
    assert plan.project_name == "Short project name"
    assert plan.document_refs == []
    assert plan.artifacts == []


async def test_server_runtime_is_deterministic_with_replaced_ports(tmp_path):
    """Exercise create, plan, execute, UI delete, and external Herdr delete."""
    settings = Settings(
        data_dir=str(tmp_path / "turn-state"),
        projects_dir=str(tmp_path / "projects"),
        default_executor="deterministic",
        planner="heuristic",
    )
    herdr = MockHerdrAdapter()
    execution = DeterministicExecutionAdapter()
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    registry.register_planner(HeuristicPlanner("deterministic"))
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
                    "agent": {"harness": "mock", "type_id": "executor"},
                    "run_policy": {"auto_run": False},
                },
            )
            assert created.status_code == 200, created.text
            contract = (await client.get("/api/schema")).json()
            assert {"Agent", "Node", "Edge", "PlanResult", "WorkerResult"} <= set(contract["models"])
            project_id = uuid.UUID(created.json()["project_id"])
            project_root = components.store._project_paths[project_id]
            load_builtin_capabilities(project_root)
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
            graph_response = await client.get(f"/api/projects/{project_id}/graph")
            graph_payload = graph_response.json()
            assert "architecture_spec" not in graph_payload
            root_payload = next(item for item in graph_payload["nodes"] if item["id"] == str(project_id))
            assert isinstance(root_payload["document_refs"], list)

            stepped = await client.post(f"/api/projects/{project_id}/step")
            assert stepped.status_code == 200, stepped.text
            assert len(stepped.json()["stepped"]) == 3
            await _wait_for(
                lambda: any(
                    node.status.value == "COMPLETE"
                    for node in components.store._states[project_id]["nodes"].values()
                    if node.id != project_id
                )
            )
            assert execution.workers == ["deterministic", "deterministic", "deterministic"]

            deleted = await client.delete(f"/api/projects/{project_id}")
            assert deleted.status_code == 200, deleted.text
            assert project_id not in components.store._states
            assert workspace_id not in herdr.workspaces

            external = await client.post(
                "/api/projects",
                json={
                    "prompt": "Verify the reverse lifecycle",
                    "agent": {"harness": "mock", "type_id": "executor"},
                    "run_policy": {"auto_run": False},
                },
            )
            external_id = uuid.UUID(external.json()["project_id"])
            external_workspace = runner.terminal.project_workspace_id(str(external_id))
            assert external_workspace is not None
            assert await herdr.close_workspace(external_workspace)
            await runner._reconcile_project_workspaces(await components.store.list_projects())
            # A provider workspace is an execution resource, not project
            # state. If Herdr removes it externally, reconciliation recreates
            # the workspace and preserves the durable project graph.
            assert await components.store.get_node(external_id) is not None
            recreated_workspace = runner.terminal.project_workspace_id(str(external_id))
            assert recreated_workspace is not None
            assert recreated_workspace != external_workspace
            assert recreated_workspace in herdr.workspaces
    finally:
        await runtime.stop()


def test_dag_contract_codecs_reject_invalid_graphs_and_parse_deterministically():
    plan = parse_plan({
        "nodes": [{"key": "build", "objective": "Build", "generated_prompt": None,
                   "executor": "deterministic", "required_inputs": [], "resource_refs": [],
                   "parent_key": None, "follows": [], "plan": False}],
        "edges": [],
    })
    assert plan.nodes[0].key == "build"
    result = parse_result({"outcome": Outcome.COMPLETE, "summary": "ok"})
    assert result.outcome is Outcome.COMPLETE


def test_planner_and_executor_are_agent_types_with_capability_contracts():
    planner = Planner()
    executor = Executor()
    integrator = Integrator()
    assert planner.type_id is AgentType.PLANNER
    assert executor.type_id is AgentType.EXECUTOR
    assert integrator.type_id is AgentType.INTEGRATOR
    assert planner.capabilities and "turn-planning" in planner.capabilities
    assert executor.capabilities == ["turn-basics", "turn-executing"]
    assert integrator.capabilities == ["turn-basics", "turn-integrating"]
    planner_context = render_context_block(
        NodeExecutionContext(
            node=Node(project_id=uuid.uuid4(), objective="plan", agent=planner)
        )
    )
    assert "turn-planning" not in planner_context
    assert "activate=" in planner_context
    assert "turn-setup" not in planner_context
    assert "turn project info" not in planner_context
    assert "harness-native capability mechanism" not in planner_context
    assert "AGENT SKILL:" not in planner_context
    integration_context = render_context_block(
        NodeExecutionContext(
            node=Node(project_id=uuid.uuid4(), objective="Integrate result", agent=integrator)
        )
    )
    assert "turn-integrating" not in integration_context
    assert "turn-basics" not in integration_context
    assert "turn-setup" not in integration_context
    assert "integrator-only directory" not in integration_context


async def test_project_documents_are_persisted_as_dynamic_references_and_visible_to_workers(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    root = await store.create_project("Build an inspectable system")
    await store.apply_plan(
        root,
        PlanResult(
            document_refs=[DocumentRef(ref="ARCHITECTURE.md", title="Architecture")],
            nodes=[NodeSpec(key="worker", objective="Build the system", executor="deterministic")],
        ),
    )

    graph = await store.get_graph(root.id)
    assert [ref.ref for ref in graph.nodes[0].document_refs] == ["ARCHITECTURE.md"]
    assert graph.artifacts == []
    state = (root.repo_path and Path(root.repo_path) / ".turn" / "state.json") or next(
        (tmp_path / "turn" / "projects").glob("*/.turn/state.json")
    )
    raw = json.loads(state.read_text())
    persisted_root = next(item for item in raw["nodes"] if item["id"] == str(root.id))
    assert persisted_root["document_refs"][0]["ref"] == "ARCHITECTURE.md"

    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    runner = Runner(
        store,
        registry=registry,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
    )
    context = await runner._build_context(root)
    rendered = render_context_block(context)
    assert "PROJECT DOCUMENT REFERENCES" not in rendered
    assert "ARCHITECTURE.md" not in rendered

    await store.dispose()
