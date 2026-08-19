"""Offline kernel and direct-filesystem worker tests."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import uuid
from pathlib import Path

from turn.config import Settings, settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    ArtifactSpec,
    EdgeType,
    HarnessKind,
    Node,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
    RunPolicy,
    WorkerResult,
)
from turn.graph.logic import evaluate
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.base import NodeExecutionContext, Planner
from turn.workers.codex_worker import CodexWorker
from turn.workers.deterministic_worker import DeterministicWorker
from turn.workers.filesystem import init_project_directory
from turn.workers.registry import WorkerRegistry
from turn.workers.planner import HeuristicPlanner
from turn.tests.mocks import MockHerdrAdapter
from turn.tests.capability_fixtures import load_builtin_capabilities


def seed_project_capabilities(store: Store, project_id: uuid.UUID) -> None:
    project_root = store._project_paths[project_id]
    load_builtin_capabilities(project_root)


def test_project_directory_initializes_an_independent_git_root(tmp_path) -> None:
    project = init_project_directory(uuid.uuid4(), working_dir=str(tmp_path / "demo"))
    assert project == str((tmp_path / "demo").resolve())
    assert (tmp_path / "demo" / ".git").exists()
    git_root = subprocess.run(
        ["git", "-C", project, "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(git_root).resolve() == Path(project).resolve()
    assert (tmp_path / "demo" / "AGENTS.md").read_text() == (
        "# Turn project\n\nThis is a Turn project.\n"
    )


def test_project_directory_preserves_existing_agents_file(tmp_path) -> None:
    project = tmp_path / "existing"
    project.mkdir()
    agents = project / "AGENTS.md"
    agents.write_text("# Existing project instructions\n")

    init_project_directory(uuid.uuid4(), working_dir=str(project))

    assert agents.read_text() == "# Existing project instructions\n"


async def test_codex_worker_refuses_missing_directory() -> None:
    worker = CodexWorker(Settings())
    node = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="do something dangerous",
        executor="codex",
        status=NodeStatus.RUNNABLE,
    )
    result = await worker.execute(NodeExecutionContext(node=node, repo_path=None))
    assert result.outcome == Outcome.FAIL
    assert "assigned project directory" in result.summary


async def test_pause_respected() -> None:
    class P(Planner):
        name = "p"

        async def plan(self, ctx):
            return PlanResult(nodes=[NodeSpec(key="a", objective="do a", executor="deterministic")])

    store = Store(tempfile.mkdtemp())
    await store.init()
    reg = WorkerRegistry()
    reg.register(DeterministicWorker())
    reg.register_planner(P())
    runner = Runner(
        store,
        registry=reg,
        events=EventBus(),
        settings=settings,
        herdr_adapter=MockHerdrAdapter(),
    )
    root = await store.create_project("x")
    seed_project_capabilities(store, root.id)
    await store.set_auto_run(root.id, True)
    await runner.step(root.id)
    if runner._running:
        await asyncio.gather(*runner._running.values(), return_exceptions=True)
    node = next(n for n in (await store.get_workgraph(root.id))[0] if n.objective == "do a")
    await runner.pause(node.id)
    await runner.tick()
    assert (await store.get_node(node.id)).status != NodeStatus.COMPLETE
    await store.dispose()


async def test_direct_files_are_available_to_downstream_nodes(tmp_path) -> None:
    class P(Planner):
        name = "p"

        async def plan(self, ctx):
            return PlanResult(
                nodes=[
                    NodeSpec(key="write", objective="write a source file", executor="deterministic"),
                    NodeSpec(key="read", objective="assemble the source file", executor="deterministic", follows=["write"]),
                ]
            )

    store = Store(tmp_path / "state")
    await store.init()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    reg = WorkerRegistry()
    reg.register(DeterministicWorker())
    reg.register_planner(P())
    runner = Runner(
        store,
        registry=reg,
        events=EventBus(),
        settings=settings,
        herdr_adapter=MockHerdrAdapter(),
    )
    root = await store.create_project("demo", repo_path=str(project_dir))
    seed_project_capabilities(store, root.id)
    await runner.set_mode(root.id, False)
    await runner.step(root.id)
    if runner._running:
        await asyncio.gather(*runner._running.values(), return_exceptions=True)
    nodes, edges, _ = await store.get_workgraph(root.id)
    assert len(nodes) == 3
    assert any(edge.type == EdgeType.FOLLOWS for edge in edges)
    assert all(node.repo_path is None or node.repo_path == str(project_dir) for node in nodes)
    await store.dispose()


async def test_architectural_decomposition_has_parallel_lanes_and_integrator(tmp_path) -> None:
    store = Store(tmp_path / "turn")
    await store.init()
    root = await store.create_project("Build a modular reading log")
    seed_project_capabilities(store, root.id)
    plan = await HeuristicPlanner("deterministic").plan(
        NodeExecutionContext(node=root.model_copy(update={"generated_prompt": root.objective}))
    )
    assert [node.objective for node in plan.nodes] == [
        "Define core structure",
        "Handle inputs and storage",
        "Create output surface",
        "Integrate the deliverable",
    ]
    assert plan.nodes[-1].follows == ["core", "inputs", "outputs"]
    assert all(not node.required_inputs for node in plan.nodes)
    created = await store.apply_plan(root, plan)
    nodes, edges, _ = await store.get_workgraph(root.id)
    evaluation = evaluate(nodes, edges)
    parallel = [node for node in created if node.objective != "Integrate the deliverable"]
    integrator = next(node for node in created if node.objective == "Integrate the deliverable")
    assert integrator.agent is not None
    assert integrator.agent.type_id.value == "integrator"
    assert "turn-integrating" in integrator.agent.capabilities
    assert all(node.id in evaluation.runnable for node in parallel)
    assert integrator.id not in evaluation.runnable
    assert {edge.src for edge in edges if edge.dst == integrator.id and edge.type == EdgeType.FOLLOWS} == {
        node.id for node in parallel
    }
    await store.dispose()


async def test_auto_runner_dispatches_all_independent_lanes_together(tmp_path) -> None:
    class ProbeWorker:
        name = "codex"

        def __init__(self):
            self.active = 0
            self.max_active = 0

        async def execute(self, ctx):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.03)
            self.active -= 1
            return WorkerResult(
                outcome=Outcome.COMPLETE,
                summary=ctx.node.objective,
                artifacts=[ArtifactSpec(name="result", content=ctx.node.objective)],
            )

    cfg = Settings()
    store = Store(tmp_path / "turn")
    await store.init()
    worker = ProbeWorker()
    registry = WorkerRegistry()
    registry.register(worker)
    runner = Runner(
        store,
        registry=registry,
        events=EventBus(),
        settings=cfg,
        herdr_adapter=MockHerdrAdapter(),
    )
    root = await store.create_project("parallel demo", run_policy=RunPolicy(auto_run=True))
    seed_project_capabilities(store, root.id)
    await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(key="a", objective="lane a", executor="codex"),
                NodeSpec(key="b", objective="lane b", executor="codex"),
                NodeSpec(key="c", objective="lane c", executor="codex"),
            ]
        ),
    )
    await runner.tick()
    tasks = list(runner._running.values())
    await asyncio.gather(*tasks)
    await runner.tick()
    assert worker.max_active == 3
    await store.dispose()


async def test_manual_step_uses_sequence_order_not_uuid_order(tmp_path) -> None:
    store = Store(tmp_path / "turn")
    await store.init()
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    runner = Runner(
        store,
        registry=registry,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
    )
    root = await store.create_project(
        "ordered manual demo",
        agent=AgentConfig(harness=HarnessKind.MOCK),
        run_policy=RunPolicy(auto_run=False),
    )
    seed_project_capabilities(store, root.id)
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="finish", objective="finish", executor="deterministic", follows=["middle"]),
            NodeSpec(key="middle", objective="middle", executor="deterministic", follows=["first"]),
            NodeSpec(key="first", objective="first", executor="deterministic"),
        ]),
    )

    selected = await runner.step(root.id)
    first = next(node for node in created if node.objective == "first")
    assert selected == [first.id]
    await asyncio.gather(*runner._running.values())
    await store.dispose()


async def test_manual_step_dispatches_the_entire_parallel_stage(tmp_path) -> None:
    store = Store(tmp_path / "turn")
    await store.init()
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    runner = Runner(
        store,
        registry=registry,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
    )
    root = await store.create_project(
        "parallel manual demo",
        agent=AgentConfig(harness=HarnessKind.MOCK),
        run_policy=RunPolicy(auto_run=False),
    )
    seed_project_capabilities(store, root.id)
    created = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(key="world", objective="world", executor="deterministic"),
                NodeSpec(key="systems", objective="systems", executor="deterministic"),
                NodeSpec(
                    key="integrate",
                    objective="integrate",
                    executor="deterministic",
                    follows=["world", "systems"],
                ),
            ]
        ),
    )

    first_stage = await runner.step(root.id)
    world = next(node for node in created if node.objective == "world")
    systems = next(node for node in created if node.objective == "systems")
    integrate = next(node for node in created if node.objective == "integrate")
    assert set(first_stage) == {world.id, systems.id}
    assert integrate.id not in first_stage

    # A second click while the stage is still active cannot advance early.
    assert await runner.step(root.id) == []
    await asyncio.gather(*runner._running.values())

    second_stage = await runner.step(root.id)
    assert second_stage == [integrate.id]
    await asyncio.gather(*runner._running.values())
    await store.dispose()


def test_sequential_policy_is_removed_from_runtime_contract():
    assert not hasattr(Settings(), "force_sequential")
    assert not hasattr(Settings(), "max_concurrency")
    assert "force_sequential" not in RunPolicy().model_dump()


def test_worker_prompt_uses_assigned_directory() -> None:
    worker = CodexWorker(Settings())
    node = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="do it",
        executor="codex",
        generated_prompt="Edit the file at /tmp/project/README.md",
        status=NodeStatus.RUNNABLE,
    )
    context = NodeExecutionContext(node=node, repo_path="/tmp/project")
    prompt = worker._build_prompt(context, cwd="/tmp/project")
    assert "/tmp/project/README.md" in prompt


if __name__ == "__main__":
    asyncio.run(test_codex_worker_refuses_missing_directory())
