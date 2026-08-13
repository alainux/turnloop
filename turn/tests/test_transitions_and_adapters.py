from __future__ import annotations

import asyncio
import shlex
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    ArtifactKind,
    EdgeSpec,
    EdgeType,
    HarnessKind,
    NodeSpec,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    ReviewMode,
    ReasoningLevel,
    RunPolicy,
    RunStatus,
    Usage,
    VerificationStatus,
    WorkerResult,
)
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.tools import graph_explorer
from turn.workers.base import NodeExecutionContext, Planner, Worker, render_context_block
from turn.workers.echo_worker import EchoWorker
from turn.workers.harnesses import CLIHarnessWorker, _json_text_and_session, recover_session_id
from turn.workers import parsing
from turn.workers.artifacts import has_material_change, missing_declared_files, requires_material_change
from turn.workers.registry import WorkerRegistry
from turn.workers.planner import AgentPlanner, CodexPlanner, HeuristicPlanner
from turn.workers import worktree
import turn.workers.planner as planner_module
from turn.workers.terminal import TerminalResult


class StubTerminal:
    def __init__(self, output: bytes = b"", capture: dict | None = None):
        self.output = output
        self.capture = capture if capture is not None else {}

    async def run(self, node_id, command, **kwargs):
        self.capture["args"] = tuple(command)
        self.capture["cwd"] = kwargs.get("cwd")
        return TerminalResult(returncode=0, output=self.output)


class SlowWorker(Worker):
    name = "slow"

    def __init__(self):
        self.started = asyncio.Event()

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class SessionWorker(Worker):
    name = "echo"

    def __init__(self):
        self.seen: list[tuple[str, str | None, str | None]] = []

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        self.seen.append(
            (str(ctx.node.id), ctx.node.agent.session_id, ctx.node.generated_prompt)
        )


class FixedPlanner(Planner):
    name = "fixed"

    async def plan(self, ctx):
        return PlanResult(
            nodes=[NodeSpec(key="leaf", objective="Alternative ending", executor="codex")]
        )


class ParentVerifierWorker(Worker):
    name = "codex"

    def __init__(self):
        self.contexts: list[NodeExecutionContext] = []

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        self.contexts.append(ctx)
        if len(self.contexts) == 1:
            return WorkerResult(
                outcome=Outcome.BLOCK,
                summary="Add a deterministic collision regression before acceptance.",
                session_id="parent-verifier-session",
            )
        return WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="Collision regression passes and the parent accepts the branch.",
            session_id="parent-verifier-session",
        )
        return WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="revision complete",
            session_id=ctx.node.agent.session_id,
            usage=Usage(input_tokens=12, output_tokens=4, cost_usd=0.01),
        )


async def test_heuristic_planner_keeps_card_titles_short_and_full_intent_in_prompts():
    intent = "Build a tiny dependency-free one-page constellation name generator with deterministic seeds and accessible controls."
    root = Node(
        id=uuid.uuid4(), project_id=uuid.uuid4(), objective="Constellation generator",
        generated_prompt=intent, executor="planner", status=NodeStatus.RUNNABLE,
    )
    plan = await HeuristicPlanner("echo").plan(NodeExecutionContext(node=root))
    assert [node.objective for node in plan.nodes] == [
        "Investigate context", "Clarify scope", "Produce deliverable", "Verify result",
    ]
    assert all(len(node.objective) <= 24 for node in plan.nodes)
    assert all(intent in (node.generated_prompt or "") for node in plan.nodes)


async def test_parent_auto_verification_can_reject_then_accept_without_losing_sessions(tmp_path):
    worker = ParentVerifierWorker()
    cfg, store, runner = await _runtime(tmp_path, worker)
    policy = RunPolicy(auto_run=False, review_mode=ReviewMode.PARENT, max_retries=2)
    root = await store.create_project(
        "Build game", name="Game", run_policy=policy,
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )
    parent = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Simulation parent",
        status=NodeStatus.EXPANDED,
        agent=AgentConfig(harness=HarnessKind.CODEX), executor="planner",
    )
    child = await store.create_node(
        project_id=root.id, parent_id=parent.id, objective="Collision runtime",
        generated_prompt="Implement collisions", status=NodeStatus.COMPLETE,
        agent=AgentConfig(harness=HarnessKind.CODEX, session_id="child-session"),
        executor="codex",
    )
    child.needs_review = True
    await store._save_node(child)

    async def reserve_without_launch(node_id):
        return node_id

    runner.run_node = reserve_without_launch
    await runner._verify_with_parent(child.id)
    rejected = await store.get_node(child.id)
    remembered_parent = await store.get_node(parent.id)
    assert rejected.needs_review is False
    assert rejected.agent.session_id == "child-session"
    assert "deterministic collision regression" in rejected.generated_prompt
    assert remembered_parent.agent.session_id is None
    assert rejected.verification_session_id == "parent-verifier-session"
    assert worker.contexts[0].purpose == "verify"
    assert worker.contexts[0].node.agent.session_id is None

    # Simulate the same child session completing its requested correction.
    rejected.status = NodeStatus.COMPLETE
    rejected.needs_review = True
    await store._save_node(rejected)
    await runner._verify_with_parent(child.id)
    accepted = await store.get_node(child.id)
    assert accepted.merge_accepted is True
    assert accepted.verification_summary.startswith("Collision regression passes")
    assert worker.contexts[1].node.agent.session_id == "parent-verifier-session"
    evidence = [a for a in await store.get_artifacts(child.id) if a.name.startswith("parent-verification-")]
    assert [item.content["decision"] for item in evidence] == ["rejected", "accepted"]
    await store.dispose()


async def _runtime(tmp_path, worker: Worker):
    cfg = Settings()
    cfg.default_executor = worker.name
    cfg.runner_tick_seconds = 0.001
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'turn.db'}")
    await store.init()
    registry = WorkerRegistry()
    registry.register(worker)
    if worker.name != "echo":
        registry.register(EchoWorker())
    runner = Runner(store, registry, EventBus(), cfg)
    return cfg, store, runner


def test_plan_contract_rejects_missing_references_and_cycles():
    with pytest.raises(ValidationError, match="unknown dependency key"):
        PlanResult(nodes=[NodeSpec(key="a", objective="a", depends_on=["missing"])])
    with pytest.raises(ValidationError, match="acyclic"):
        PlanResult(
            nodes=[NodeSpec(key="a", objective="a"), NodeSpec(key="b", objective="b")],
            edges=[
                EdgeSpec(type=EdgeType.DEPENDS_ON, src="a", dst="b"),
                EdgeSpec(type=EdgeType.DEPENDS_ON, src="b", dst="a"),
            ],
        )


def test_cli_harness_commands_distinguish_new_and_resumed_sessions():
    worker = CLIHarnessWorker(HarnessKind.CLAUDE)
    agent = AgentConfig(harness=HarnessKind.CLAUDE, session_id="00000000-0000-0000-0000-000000000123")
    fresh = worker._command(agent, "do it", "/tmp/project", resume=False)
    resumed = worker._command(agent, "fix it", "/tmp/project", resume=True)
    assert fresh[fresh.index("--session-id") + 1] == agent.session_id
    assert "--resume" not in fresh
    assert resumed[resumed.index("--resume") + 1] == agent.session_id
    assert "--session-id" not in resumed

    text, session, usage = _json_text_and_session(
        '{"session_id":"s-1","text":"done","usage":{"input_tokens":9,"output_tokens":3,"cost":0.02}}'
    )
    assert (text, session) == ("done", "s-1")
    assert usage.input_tokens == 9 and usage.cost_usd == 0.02
    nested_pi = (
        '{"type":"message_end","message":{"content":[{"type":"text","text":'
        '"```turn-result\\n{\\\"outcome\\\":\\\"COMPLETE\\\",\\\"summary\\\":\\\"ok\\\"}\\n```"}],"usage":'
        '{"input":11,"output":5,"cacheRead":7,"cost":{"total":0.004}}}}'
    )
    pi_text, _, pi_usage = _json_text_and_session(nested_pi)
    assert '"outcome":"COMPLETE"' in pi_text
    assert pi_usage.input_tokens == 11
    assert pi_usage.cached_input_tokens == 7
    assert pi_usage.output_tokens == 5
    assert pi_usage.cost_usd == 0.004
    pi_event = '{"type":"session","id":"019ffb1f-2223-7c64-bf37-26e5d98c0b31"}'
    assert _json_text_and_session(pi_event)[1] == "019ffb1f-2223-7c64-bf37-26e5d98c0b31"
    assert recover_session_id("noise\n" + pi_event) == "019ffb1f-2223-7c64-bf37-26e5d98c0b31"


async def test_agent_config_inherits_cascades_and_forks(tmp_path):
    cfg, store, runner = await _runtime(tmp_path, EchoWorker())
    runner.registry.register_planner(FixedPlanner())
    chosen = AgentConfig(
        harness=HarnessKind.CODEX,
        model="gpt-5.6-luna",
        reasoning=ReasoningLevel.HIGH,
    )
    root = await store.create_project("game", agent=chosen, run_policy=RunPolicy(auto_run=False))
    children = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="story", objective="Plan story", executor="planner", plan=True),
            NodeSpec(key="code", objective="Build game", executor="codex"),
        ]),
    )
    assert all(c.agent.model == "gpt-5.6-luna" for c in children)
    assert all(c.agent.reasoning == ReasoningLevel.HIGH for c in children)
    assert all(c.agent.harness == HarnessKind.CODEX for c in children)

    pi = AgentConfig(
        harness=HarnessKind.PI,
        model="freeinference/deepseek-v4-flash",
        reasoning=ReasoningLevel.HIGH,
    )
    await runner.edit_node(root.id, agent=pi, cascade_agent=True)
    live = [n for n in await store.descendants(root.id) if not n.superseded_by]
    assert all(n.agent.harness == HarnessKind.PI for n in live)
    assert all(n.agent.model == pi.model and n.agent.reasoning == pi.reasoning for n in live)

    planner_child = next(n for n in live if n.executor == "planner")
    fork = await runner.fork(planner_child.id, generated_prompt="try a stranger fork")
    assert fork.forked_from == planner_child.id
    assert fork.agent.harness == HarnessKind.PI and fork.agent.model == pi.model
    assert fork.agent.session_id is None
    assert fork.generated_prompt == "try a stranger fork"
    await store.dispose()


async def test_root_fork_stays_visible_in_its_project(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    runner.registry.register_planner(FixedPlanner())
    root = await store.create_project("visible root fork", run_policy=RunPolicy(auto_run=False))

    fork = await runner.fork(root.id, objective="alternative root plan")

    nodes, edges, _ = await store.get_workgraph(root.id)
    assert fork.project_id == root.id
    assert fork.parent_id == root.id
    assert fork.agent.type_id == "planner"
    assert any(node.id == fork.id for node in nodes)
    assert any(edge.src == root.id and edge.dst == fork.id for edge in edges)
    await store.dispose()


async def test_store_migrates_legacy_planner_agent_type(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'legacy-planner.db'}"
    store = Store(database_url)
    await store.init()
    root = await store.create_project("legacy planner")
    root.agent.type_id = "general"
    await store._save_node(root)
    await store.dispose()

    reopened = Store(database_url)
    await reopened.init()
    migrated = await reopened.get_node(root.id)
    assert migrated.agent.type_id == "planner"
    await reopened.dispose()


async def test_new_plan_children_inherit_config_but_not_parent_session(tmp_path):
    _, store, _ = await _runtime(tmp_path, EchoWorker())
    parent = await store.create_project(
        "session boundary",
        agent=AgentConfig(
            harness=HarnessKind.CODEX,
            model="gpt-5.6-luna",
            reasoning=ReasoningLevel.HIGH,
            session_id="planner-thread",
        ),
        run_policy=RunPolicy(auto_run=False),
    )
    created = await store.apply_plan(
        parent,
        PlanResult(nodes=[NodeSpec(key="child", objective="Implement leaf")]),
    )

    child = created[0]
    assert child.agent.harness == HarnessKind.CODEX
    assert child.agent.model == "gpt-5.6-luna"
    assert child.agent.reasoning == ReasoningLevel.HIGH
    assert child.agent.session_id is None
    await store.dispose()


async def test_regeneration_cancels_descendant_verifiers_and_releases_stale_review(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    runner.registry.register_planner(FixedPlanner())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    parent = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Planner branch",
        executor="planner", status=NodeStatus.EXPANDED,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    child = await store.create_node(
        project_id=root.id, parent_id=parent.id, objective="Stale child",
        executor="echo", status=NodeStatus.COMPLETE,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    child.needs_review = True
    await store._save_node(child)
    verifier = asyncio.create_task(asyncio.Event().wait())
    runner._verifying[child.id] = verifier

    await runner.regenerate_descendants(parent.id)

    assert verifier.cancelled()
    stale = await store.get_node(child.id)
    assert stale.status == NodeStatus.CANCELLED
    assert stale.superseded_by == parent.id
    assert stale.needs_review is False
    await store.dispose()


async def test_scheduler_reconciles_cancelled_stale_review_from_persisted_history(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    stale = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Cancelled historical work",
        executor="echo",
        status=NodeStatus.CANCELLED,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    stale.needs_review = True
    stale.verification_status = VerificationStatus.ERROR
    await store._save_node(stale)
    verifier = asyncio.create_task(asyncio.Event().wait())
    runner._verifying[stale.id] = verifier

    await runner._schedule_project(root.id)

    repaired = await store.get_node(stale.id)
    assert repaired.needs_review is False
    assert repaired.verification_status == VerificationStatus.ERROR  # history is retained
    assert verifier.cancelled()
    await store.dispose()


async def test_scheduler_cancels_child_created_after_parent_cancellation(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    cancelled_parent = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Cancelled parent",
        executor="planner",
        status=NodeStatus.CANCELLED,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    late_child = await store.create_node(
        project_id=root.id,
        parent_id=cancelled_parent.id,
        objective="Late verifier replacement",
        executor="echo",
        status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    late_child.needs_review = True
    await store._save_node(late_child)
    worker = asyncio.create_task(asyncio.Event().wait())
    verifier = asyncio.create_task(asyncio.Event().wait())
    runner._running[late_child.id] = worker
    runner._verifying[late_child.id] = verifier

    await runner._schedule_project(root.id)

    repaired = await store.get_node(late_child.id)
    assert repaired.status == NodeStatus.CANCELLED
    assert repaired.superseded_by == cancelled_parent.id
    assert repaired.needs_review is False
    assert worker.cancelled() and verifier.cancelled()
    await store.dispose()


async def test_parent_verification_never_resets_the_child_worktree(monkeypatch, tmp_path):
    from turn.workers.codex_worker import CodexWorker

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    observed: list[bool] = []
    merges: list[uuid.UUID] = []

    def fake_worktree(node_id, parent_id, force=False, repo_path=None):
        observed.append(force)
        return str(repo)

    async def fake_exec(*args, **kwargs):
        class Reader:
            async def read(self, _size):
                return b""

        class Process:
            stdout = Reader()
            stderr = Reader()

            async def wait(self):
                return 0

        return Process()

    monkeypatch.setattr("turn.workers.codex_worker.worktree.get_or_create_worktree", fake_worktree)
    monkeypatch.setattr(
        "turn.workers.codex_worker.worktree.merge_into_parent",
        lambda node_id, *args, **kwargs: merges.append(node_id),
    )
    monkeypatch.setattr("turn.workers.codex_worker.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "turn.workers.codex_worker.CodexWorker._parse_result",
        lambda self, text: WorkerResult(outcome=Outcome.COMPLETE, summary="verified"),
    )
    node = Node(
        project_id=uuid.uuid4(),
        parent_id=uuid.uuid4(),
        objective="Verify branch",
        executor="codex",
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )
    context = NodeExecutionContext(node=node, repo_path=str(repo), purpose="verify", terminal=StubTerminal())

    await CodexWorker(Settings()).execute(context)

    validator_node = node.model_copy(deep=True)
    validator_node.agent.type_id = "validator"
    await CodexWorker(Settings()).execute(
        NodeExecutionContext(node=validator_node, repo_path=str(repo), terminal=StubTerminal())
    )

    assert observed == [False, False]
    assert merges == []


@pytest.mark.parametrize("harness", [HarnessKind.CLAUDE, HarnessKind.OPENCODE, HarnessKind.PI])
async def test_generic_parent_verification_is_read_only_for_every_provider(
    harness, monkeypatch, tmp_path
):
    repo = tmp_path / harness.value
    repo.mkdir()
    (repo / ".git").mkdir()
    forces: list[bool] = []
    merges: list[uuid.UUID] = []
    commands: list[tuple[str, ...]] = []

    def fake_worktree(node_id, parent_id, force=False, repo_path=None):
        forces.append(force)
        return str(repo)

    class Process:
        returncode = 0

        async def communicate(self):
            payload = '```turn-result\n{"outcome":"COMPLETE","summary":"verified"}\n```'
            return (f'{{"text":{payload!r}}}'.replace("'", '"').encode(), b"")

    async def fake_exec(*command, **kwargs):
        commands.append(tuple(command))
        # Use valid provider JSON-lines while preserving the fenced result.
        class ValidProcess(Process):
            async def communicate(self):
                import json
                text = '```turn-result\n{"outcome":"COMPLETE","summary":"verified"}\n```'
                return ((json.dumps({"text": text}) + "\n").encode(), b"")
        return ValidProcess()

    monkeypatch.setattr("turn.workers.harnesses.worktree.get_or_create_worktree", fake_worktree)
    monkeypatch.setattr(
        "turn.workers.harnesses.worktree.merge_into_parent",
        lambda node_id, *args, **kwargs: merges.append(node_id),
    )
    monkeypatch.setattr("turn.workers.harnesses.capture_worktree", lambda cwd: [])
    monkeypatch.setattr("turn.workers.harnesses.asyncio.create_subprocess_exec", fake_exec)
    node = Node(
        project_id=uuid.uuid4(),
        parent_id=uuid.uuid4(),
        objective="Verify provider branch",
        executor=harness.value,
        agent=AgentConfig(harness=harness, session_id="review-session"),
    )

    import json
    structured = '```turn-result\n{"outcome":"COMPLETE","summary":"verified"}\n```'
    terminal = StubTerminal((json.dumps({"text": structured}) + "\n").encode(), {"commands": commands})
    result = await CLIHarnessWorker(harness).execute(
        NodeExecutionContext(node=node, repo_path=str(repo), purpose="verify", terminal=terminal)
    )

    assert result.outcome == Outcome.COMPLETE
    assert forces == [False]
    assert merges == []
    assert any("Do not edit files." in part for part in terminal.capture["args"])


async def test_old_verifier_callback_cannot_drop_new_single_flight_reservation(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    node_id = uuid.uuid4()
    async def finishes_immediately(_node_id):
        return None

    runner._verify_with_parent = finishes_immediately
    runner._queue_parent_verification(node_id)
    old = runner._verifying[node_id]
    replacement = asyncio.create_task(asyncio.Event().wait())
    runner._verifying[node_id] = replacement
    await asyncio.sleep(0)
    await old
    await asyncio.sleep(0)

    assert runner._verifying[node_id] is replacement
    replacement.cancel()
    await store.dispose()


async def test_accepting_container_stops_descendant_tasks_before_cleanup(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    parent = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Container",
        executor="planner", status=NodeStatus.COMPLETE,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    child = await store.create_node(
        project_id=root.id, parent_id=parent.id, objective="Late worker",
        executor="echo", status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    accepted_child = await store.create_node(
        project_id=root.id, parent_id=parent.id, objective="Previously accepted worker",
        executor="echo", status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    accepted_child.merge_accepted = True
    accepted_child.verification_status = VerificationStatus.ACCEPTED
    await store._save_node(accepted_child)
    parent.needs_review = True
    await store._save_node(parent)
    worker = asyncio.create_task(asyncio.Event().wait())
    verifier = asyncio.create_task(asyncio.Event().wait())
    accepted_worker = asyncio.create_task(asyncio.Event().wait())
    runner._running[child.id] = worker
    runner._running[accepted_child.id] = accepted_worker
    runner._verifying[child.id] = verifier

    await runner.accept_merge(parent.id)

    assert worker.cancelled() and verifier.cancelled() and accepted_worker.cancelled()
    repaired = await store.get_node(child.id)
    assert repaired.status == NodeStatus.CANCELLED
    assert repaired.merge_accepted is False
    assert repaired.needs_review is False
    accepted_projection = await store.get_node(accepted_child.id)
    assert accepted_projection.status == NodeStatus.COMPLETE
    assert accepted_projection.merge_accepted is True
    await store.dispose()


async def test_scheduler_terminalizes_persisted_running_rows_without_live_tasks(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    orphan = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Interrupted work",
        status=NodeStatus.CANCELLED, agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    live = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Owned work",
        status=NodeStatus.RUNNING, agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    orphan_run = await store.create_run(orphan, "echo")
    live_run = await store.create_run(live, "echo")
    live_task = asyncio.create_task(asyncio.Event().wait())
    runner._running[live.id] = live_task

    await runner._schedule_project(root.id)

    assert (await store.get_runs(orphan.id))[-1].status == RunStatus.CANCELLED
    assert (await store.get_runs(live.id))[-1].status == RunStatus.RUNNING
    assert orphan_run.id != live_run.id
    live_task.cancel()
    await asyncio.gather(live_task, return_exceptions=True)
    await store.dispose()


async def test_late_failure_cannot_revive_an_accepted_node(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    node = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Accepted while running",
        executor="echo", status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    node.merge_accepted = True
    await store._save_node(node)
    run = await store.create_run(node, "echo", 1)

    await runner._handle_outcome(
        node, run, root.id,
        WorkerResult(outcome=Outcome.FAIL, summary="late failure", retry_recommended=True),
    )

    repaired = await store.get_node(node.id)
    saved_run = (await store.get_runs(node.id))[-1]
    assert repaired.status == NodeStatus.COMPLETE
    assert repaired.merge_accepted is True
    assert saved_run.status == RunStatus.CANCELLED
    assert runner._retries.get(node.id, 0) == 0
    await store.dispose()


def test_nested_child_merges_into_parent_worktree_not_repository_root(tmp_path):
    repo = worktree.init_project_repo(uuid.uuid4(), working_dir=str(tmp_path / "repo"))
    root_id = uuid.uuid4()
    parent_id = uuid.uuid4()
    child_id = uuid.uuid4()
    worktree.get_or_create_worktree(root_id, None, repo_path=repo)
    parent_wt = worktree.get_or_create_worktree(parent_id, root_id, repo_path=repo)
    child_wt = worktree.get_or_create_worktree(child_id, parent_id, repo_path=repo)
    assert parent_wt and child_wt
    (Path(child_wt) / "nested-output.txt").write_text("from child\n")

    worktree.merge_into_parent(child_id, parent_id, repo_path=repo)

    assert (Path(parent_wt) / "nested-output.txt").read_text() == "from child\n"
    assert not (Path(repo) / "nested-output.txt").exists()
    assert worktree._git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).stdout.strip() == worktree.branch_name(root_id)


async def test_codex_planner_resumes_its_own_session(monkeypatch, tmp_path):
    captured = {}

    class Reader:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        async def read(self, _size):
            return self.chunks.pop(0) if self.chunks else b""

    class Process:
        def __init__(self):
            self.stdout = Reader([b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n'])
            self.stderr = Reader([])

        async def wait(self):
            return 0

    async def fake_subprocess(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return Process()

    monkeypatch.setattr(planner_module.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(planner_module.asyncio, "create_subprocess_exec", fake_subprocess)
    cfg = Settings()
    planner = CodexPlanner(settings=cfg)
    agent = AgentConfig(harness=HarnessKind.CODEX, session_id="planner-session")
    terminal = StubTerminal(b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n', captured)
    _, _, session = await planner._call_codex("revise plan", str(tmp_path), agent=agent, terminal=terminal)

    args = captured["args"]
    assert args[:3] == (cfg.codex_binary, "exec", "resume")
    assert "--ephemeral" not in args
    assert "planner-session" in args
    assert captured["cwd"] == str(tmp_path)
    assert session == "planner-session"


async def test_graph_inspection_is_audited_and_integrators_get_glue_contract(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'turn.db'}"
    store = Store(db_url)
    await store.init()
    root = await store.create_project("Assemble the adventure")
    ctx = NodeExecutionContext(node=root)
    block = render_context_block(ctx)
    assert shlex.quote(sys.executable) in block
    assert f"--requester {root.id}" in block
    assert "INTEGRATOR CONTRACT" in block
    assert "Limit changes to assembly" in block

    await graph_explorer._query(db_url, str(root.id), str(root.id), "tree")
    evidence = await store.get_graph_inspections(root.id)
    assert len(evidence) == 1
    assert evidence[0]["requester_node_id"] == str(root.id)
    assert evidence[0]["query"] == "tree"
    await store.dispose()


def test_planner_honors_editable_planning_instructions():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Create a tiny adventure",
        generated_prompt="Use two nested planning branches and a small integration leaf.",
    )
    prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=node))
    assert "PLANNING INSTRUCTIONS FOR THIS NODE" in prompt
    assert "Use two nested planning branches" in prompt


def test_codex_choked_output_is_retryable_not_a_false_success():
    from turn.workers.codex_worker import CodexWorker

    parsed = CodexWorker()._parse_result("I will inspect the project now.")
    assert parsed.outcome == Outcome.FAIL
    assert parsed.retry_recommended is True
    assert "structured result" in parsed.summary


def test_agent_artifact_shorthand_is_normalized():
    specs = parsing.artifact_specs([
        "app.js",
        {"path": "docs/README.md", "description": "Run instructions"},
        {"name": "evidence", "kind": "evidence", "content": "checked"},
    ])
    assert [(a.kind, a.name) for a in specs] == [
        (ArtifactKind.FILE, "app.js"),
        (ArtifactKind.FILE, "README.md"),
        (ArtifactKind.EVIDENCE, "evidence"),
    ]
    assert specs[1].ref == "docs/README.md"


def test_claimed_file_outputs_must_exist_in_the_worktree(tmp_path):
    (tmp_path / "present.js").write_text("ok")
    specs = parsing.artifact_specs(["present.js", "missing.js"])
    assert missing_declared_files(specs, str(tmp_path)) == ["missing.js"]


def test_graph_tool_json_cannot_be_mistaken_for_a_worker_plan():
    graph_output = '{"nodes":[{"id":"existing","objective":"already built"}],"edges":[]}'
    assert parsing.first_plan_json(graph_output) is None
    fenced = '```turn-plan\n{"nodes":[{"key":"new","objective":"new work"}],"edges":[]}\n```'
    assert parsing.first_plan_json(fenced)["nodes"][0]["key"] == "new"
    mixed = graph_output + '\n```turn-result\n{"outcome":"COMPLETE","summary":"verified"}\n```'
    assert parsing.first_result_json(mixed)["summary"] == "verified"


def test_bare_schema_plan_is_accepted_and_explicit_edges_follow_domain_direction():
    bare = '{"nodes":[{"key":"a","objective":"write story"},{"key":"b","objective":"build UI"}],"edges":[{"src":"a","dst":"b"}]}'
    assert parsing.first_plan_json(bare)["nodes"][0]["key"] == "a"
    plan = AgentPlanner._parse_plan(bare)
    assert plan is not None
    assert plan.nodes[0].depends_on == []
    assert plan.nodes[1].depends_on == ["a"]


def test_final_structured_worker_result_wins_and_material_work_is_explicit():
    messages = (
        '{"outcome":"COMPLETE","summary":"I will inspect"}\n'
        '{"outcome":"COMPLETE","summary":"finished integration"}'
    )
    assert parsing.first_result_json(messages)["summary"] == "finished integration"
    assert requires_material_change("Assemble the application", None)
    assert not has_material_change([])


def test_parent_verification_is_read_only_and_does_not_require_a_file_diff():
    source = (Path(__file__).parents[1] / "workers" / "codex_worker.py").read_text()
    assert 'ctx.node.agent.type_id == "validator"' in source
    assert "force=not is_verification" in source
    assert "and not is_verification" in source


def test_local_harnesses_share_the_bidirectional_terminal_transport():
    workers = Path(__file__).parents[1] / "workers"
    for name in ("codex_worker.py", "planner.py", "harnesses.py"):
        source = (workers / name).read_text()
        assert ".run(" in source and "LocalPtyTransport" in source, name


async def test_harness_switch_clears_provider_session_in_store(tmp_path):
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'session.db'}")
    await store.init()
    root = await store.create_project("session invariant")
    root = await store.edit_node(
        root.id,
        agent=AgentConfig(harness=HarnessKind.CODEX, session_id="codex-thread"),
    )
    changed = await store.edit_node(
        root.id,
        agent=AgentConfig(harness=HarnessKind.PI, session_id="codex-thread"),
    )
    assert changed.agent.harness == HarnessKind.PI
    assert changed.agent.session_id is None
    await store.dispose()


async def test_completed_project_reships_correction_and_repairs_accepted_residue(tmp_path):
    import subprocess
    from turn.workers import worktree

    root_id = uuid.uuid4()
    repo = worktree.init_project_repo(root_id, working_dir=str(tmp_path / "repo"))
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'reship.db'}")
    await store.init()
    policy = RunPolicy(auto_run=False, review_mode=ReviewMode.AUTO_ACCEPT)
    root = await store.create_project("root", id=root_id, repo_path=repo, run_policy=policy)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="integrate correction",
        status=NodeStatus.CANCELLED,
    )
    child.merge_accepted = True
    await store._save_node(child)
    await store.set_status(root.id, NodeStatus.COMPLETE)

    # Simulate a correction merged to the root working branch after the first
    # project shipment, plus stale lifecycle resources from a prior accept.
    (tmp_path / "repo" / "correction.txt").write_text("fixed")
    subprocess.run(["git", "add", "correction.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "correction"], cwd=repo, check=True)
    subprocess.run(["git", "branch", worktree.branch_name(child.id)], cwd=repo, check=True)
    orphan = worktree.worktree_path(child.id, repo)
    orphan.mkdir(parents=True)
    (orphan / "residue.txt").write_text("stale")

    runner = Runner(store, WorkerRegistry(), EventBus(), Settings())
    await runner._schedule_project(root.id)

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "master" or branch == "main"
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", worktree.branch_name(root.id), branch],
        cwd=repo,
    ).returncode == 0
    assert not orphan.exists()
    assert not worktree._branch_exists(worktree.branch_name(child.id), repo)
    repaired = await store.get_node(child.id)
    assert repaired.status == NodeStatus.COMPLETE
    assert repaired.merge_accepted is True
    await store.dispose()


async def test_accepted_cleanup_refetches_stale_snapshot_before_removal(tmp_path):
    from turn.workers import worktree

    root_id = uuid.uuid4()
    repo = worktree.init_project_repo(root_id, working_dir=str(tmp_path / "repo"))
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    await store.init()
    root = await store.create_project("root", id=root_id, repo_path=repo)
    child = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="child", status=NodeStatus.COMPLETE,
    )
    child.merge_accepted = True
    await store._save_node(child)
    stale = await store.get_node(child.id)

    # A reviewer revives the node after the scheduler captured `stale`.
    fresh = await store.get_node(child.id)
    fresh.merge_accepted = False
    fresh.status = NodeStatus.RUNNABLE
    await store._save_node(fresh)
    active = worktree.worktree_path(child.id, repo)
    active.mkdir(parents=True)
    (active / "active.txt").write_text("do not delete")

    runner = Runner(store, WorkerRegistry(), EventBus(), Settings())
    await runner._cleanup_accepted(stale)
    assert active.exists()
    await store.dispose()


async def test_rejection_waits_for_inflight_accepted_cleanup(tmp_path):
    from turn.workers import worktree

    root_id = uuid.uuid4()
    repo = worktree.init_project_repo(root_id, working_dir=str(tmp_path / "repo"))
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'barrier.db'}")
    await store.init()
    root = await store.create_project("root", id=root_id, repo_path=repo)
    child = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="child", status=NodeStatus.COMPLETE,
    )
    child.merge_accepted = True
    await store._save_node(child)
    active = worktree.worktree_path(child.id, repo)
    active.mkdir(parents=True)
    (active / "stale.txt").write_text("stale")

    runner = Runner(store, WorkerRegistry(), EventBus(), Settings())
    async def reserve_without_worker(node_id):
        return node_id
    runner.run_node = reserve_without_worker
    entered = asyncio.Event()
    release = asyncio.Event()
    original_remove = runner._remove_merged_resources

    async def paused_remove(ids, project_repo):
        entered.set()
        await release.wait()
        return await original_remove(ids, project_repo)

    runner._remove_merged_resources = paused_remove
    cleanup = asyncio.create_task(runner._cleanup_accepted(child))
    await entered.wait()
    rejection = asyncio.create_task(runner.reject_merge(child.id, "revise safely"))
    await asyncio.sleep(0)
    # Rejection cannot flip lifecycle state while cleanup holds the lock.
    during = await store.get_node(child.id)
    assert during.merge_accepted is True
    assert not rejection.done()

    release.set()
    await cleanup
    await rejection
    after = await store.get_node(child.id)
    assert after.merge_accepted is False
    assert after.status in (NodeStatus.RUNNABLE, NodeStatus.RUNNING)
    await store.dispose()


async def test_scheduler_snapshot_cannot_regress_a_fresh_complete_node(tmp_path):
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'scheduler-race.db'}")
    await store.init()
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=True))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    child = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="leaf", status=NodeStatus.PENDING,
    )
    stale_graph = await store.get_workgraph(root.id)
    await store.set_status(child.id, NodeStatus.COMPLETE)

    async def old_snapshot(_project_id):
        return stale_graph

    store.get_workgraph = old_snapshot
    runner = Runner(store, WorkerRegistry(), EventBus(), Settings())
    await runner._schedule_project(root.id)
    assert (await store.get_node(child.id)).status == NodeStatus.COMPLETE
    assert child.id not in runner._running
    await store.dispose()


async def test_cancelling_a_running_node_cancels_task_and_run(tmp_path):
    slow = SlowWorker()
    _, store, runner = await _runtime(tmp_path, slow)
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="slow work",
        executor="slow",
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    # Worker lookup follows agent harness. Keep the custom adapter assignment
    # explicit without adding it to the persistent HarnessKind catalog.
    child.agent = None
    await store._save_node(child)
    await runner.run_node(child.id)
    await asyncio.wait_for(slow.started.wait(), timeout=1)
    task = runner._running[child.id]
    await runner.cancel(child.id)
    await asyncio.gather(task, return_exceptions=True)

    cancelled = await store.get_node(child.id)
    runs = await store.get_runs(child.id)
    assert cancelled.status == NodeStatus.CANCELLED
    assert runs[-1].status.value == "CANCELLED"
    await store.dispose()


async def test_resume_respects_step_and_auto_modes(tmp_path):
    _, store, runner = await _runtime(tmp_path, EchoWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="resumable",
        executor="echo",
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    await runner.pause(child.id)
    await runner.resume(child.id)
    await runner._schedule_project(root.id)
    assert (await store.get_node(child.id)).status == NodeStatus.RUNNABLE
    assert child.id not in runner._running

    await runner.set_mode(root.id, True)
    await runner._schedule_project(root.id)
    await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    assert (await store.get_node(child.id)).status == NodeStatus.COMPLETE
    await store.dispose()


async def test_accept_is_terminal_and_reject_reuses_agent_session(tmp_path):
    session_worker = SessionWorker()
    _, store, runner = await _runtime(tmp_path, session_worker)
    root = await store.create_project(
        "root", run_policy=RunPolicy(auto_run=True, review_mode=ReviewMode.MANUAL)
    )
    await store.set_status(root.id, NodeStatus.EXPANDED)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="reviewed work",
        generated_prompt="initial context",
        executor="echo",
        status=NodeStatus.COMPLETE,
    )
    child.agent = AgentConfig(harness=HarnessKind.ECHO, session_id="session-42")
    child.needs_review = True
    await store._save_node(child)

    await runner.reject_merge(child.id, "preserve the ending")
    await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    revised = await store.get_node(child.id)
    assert session_worker.seen == [
        (str(child.id), "session-42", revised.generated_prompt)
    ]
    assert revised.id == child.id and revised.agent.session_id == "session-42"
    assert "preserve the ending" in revised.generated_prompt

    revised.needs_review = True
    revised.status = NodeStatus.COMPLETE
    await store._save_node(revised)
    await runner.accept_merge(child.id)
    await runner.resume(child.id)
    accepted = await store.get_node(child.id)
    assert accepted.status == NodeStatus.COMPLETE
    assert accepted.merge_accepted and not accepted.needs_review
    assert not runner._auto_accept(await store.get_node(root.id))

    auto_root = await store.get_node(root.id)
    auto_root.run_policy.review_mode = ReviewMode.AUTO_ACCEPT
    await store._save_node(auto_root)
    assert runner._auto_accept(await store.get_node(root.id))
    await store.dispose()
