from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from turn.config import Settings
from turn.db.store import Store
from turn.__main__ import agent_command, parser
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    ArtifactSpec,
    ArtifactKind,
    EdgeSpec,
    Edge,
    EdgeType,
    HarnessKind,
    NodeSpec,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    ReasoningLevel,
    RunPolicy,
    RunStatus,
    Usage,
    WorkerResult,
)
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.tools import graph_explorer
from turn.workers.base import NodeExecutionContext, Planner, Worker, render_context_block
from turn.workers.deterministic_worker import DeterministicWorker
from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport
from turn.workers.harnesses import CLIHarnessWorker, _json_text_and_session, recover_session_id
from turn.workers.harness_catalog import HarnessCommandFactory
import turn.workers.harnesses as harness_module
from turn.workers import parsing
from turn.workers.registry import WorkerRegistry
from turn.workers.herdr import HerdrResourceNotFound
from turn.workers.planner import AgentPlanner, CodexPlanner, HeuristicPlanner
import turn.workers.planner as planner_module
from turn.workers.terminal import LocalPtyTransport, TerminalResult
from turn.tests.capability_fixtures import load_builtin_capabilities


@pytest.fixture(autouse=True)
def load_builtin_capability_plugins(tmp_path):
    # Direct worker tests bypass the planner boundary.
    for project_root in (tmp_path, tmp_path / "project"):
        load_builtin_capabilities(project_root)


def test_codex_model_discovery_handles_long_lived_server_bytes(monkeypatch):
    payload = '{"id":2,"result":{"data":[{"id":"gpt-test","supportedReasoningEfforts":[{"reasoningEffort":"high"}]}]}}\n'

    class Process:
        def __init__(self, *args, **kwargs):
            self.stdin = self
            self.stdout = iter([payload])

        def write(self, value):
            return len(value)

        def flush(self):
            pass

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(harness_module.subprocess, "Popen", Process)
    assert harness_module._codex_models() == ["gpt-test"]
    assert harness_module.reasoning_levels_for("codex", "gpt-test") == ["default", "high"]


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


class FixedPlanner(Planner):
    name = "fixed"

    async def plan(self, ctx):
        return PlanResult(
            nodes=[NodeSpec(key="leaf", objective="Alternative ending", executor="codex")]
        )


class SessionPlanner(Planner):
    name = "session-aware"

    def __init__(self, session_id: str, started: asyncio.Event | None = None):
        self.session_id = session_id
        self.started = started
        self.forbidden_session_id: str | None = None
        self.seen_session_id: str | None = None
        self.command: list[str] | None = None

    async def plan(self, ctx):
        self.forbidden_session_id = ctx.forbidden_session_id
        self.seen_session_id = ctx.node.agent.session_id if ctx.node.agent else None
        if ctx.node.agent and ctx.node.agent.harness in {
            HarnessKind.CODEX,
            HarnessKind.CLAUDE,
            HarnessKind.OPENCODE,
            HarnessKind.PI,
        }:
            self.command = HarnessCommandFactory().planner_command(
                ctx.node.agent,
                "fresh planner prompt",
                cwd=ctx.repo_path or ".",
                native=True,
                resume=self.seen_session_id is not None,
            )
        if self.started is not None:
            self.started.set()
            await asyncio.Event().wait()
        return PlanResult(
            nodes=[NodeSpec(key="leaf", objective="Fresh branch", executor="deterministic")],
            session_id=self.session_id,
        )


async def test_heuristic_planner_keeps_card_titles_short_and_full_intent_in_prompts():
    intent = "Build a tiny dependency-free one-page constellation name generator with deterministic seeds and accessible controls."
    root = Node(
        id=uuid.uuid4(), project_id=uuid.uuid4(), objective="Constellation generator",
        generated_prompt=intent, executor="planner", status=NodeStatus.RUNNABLE,
    )
    plan = await HeuristicPlanner("deterministic").plan(NodeExecutionContext(node=root))
    assert [node.objective for node in plan.nodes] == [
        "Define core structure", "Handle inputs and storage",
        "Create output surface", "Integrate the deliverable",
    ]
    assert all(len(node.objective) <= 50 for node in plan.nodes)
    assert all(intent in (node.generated_prompt or "") for node in plan.nodes)


async def test_heuristic_planner_inherits_the_project_agent_harness():
    root = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="Luna game",
        executor="planner",
        agent=AgentConfig(harness=HarnessKind.CODEX, model="gpt-5.6-luna"),
    )
    plan = await HeuristicPlanner("deterministic").plan(NodeExecutionContext(node=root))
    assert {node.executor for node in plan.nodes} == {"codex"}
    assert plan.nodes[-1].agent_type.value == "integrator"


async def _runtime(tmp_path, worker: Worker):
    cfg = Settings()
    cfg.default_executor = worker.name
    cfg.runner_tick_seconds = 0.001
    store = Store(tmp_path / "turn")
    await store.init()
    registry = WorkerRegistry()
    registry.register(worker)
    if worker.name != "deterministic":
        registry.register(DeterministicWorker())
    runner = Runner(store, registry, EventBus(), cfg, herdr_adapter=MockHerdrAdapter())
    return cfg, store, runner


@pytest.mark.asyncio
async def test_scheduler_reservation_is_idempotent_when_manual_and_auto_paths_race(tmp_path):
    """One RUNNABLE observation must produce one provider attempt."""
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    started: list[uuid.UUID] = []
    release = asyncio.Event()
    node = Node(id=uuid.uuid4(), project_id=uuid.uuid4(), objective="race", executor="deterministic")

    async def execute(candidate, _project_id):
        started.append(candidate.id)
        await release.wait()

    runner.scheduler.set_executor(execute)
    first = runner.scheduler.reserve(node, node.project_id)
    second = runner.scheduler.reserve(node, node.project_id)
    await asyncio.sleep(0)

    assert first is second
    assert started == [node.id]
    release.set()
    await first
    await store.dispose()


def test_plan_contract_rejects_missing_references_and_cycles():
    with pytest.raises(ValidationError, match="unknown sequence key"):
        PlanResult(nodes=[NodeSpec(key="a", objective="a", follows=["missing"])])
    with pytest.raises(ValidationError, match="cycle"):
        PlanResult(
            nodes=[NodeSpec(key="a", objective="a"), NodeSpec(key="b", objective="b")],
            edges=[
                EdgeSpec(type=EdgeType.FOLLOWS, src="a", dst="b"),
                EdgeSpec(type=EdgeType.FOLLOWS, src="b", dst="a"),
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
    opencode_event = (
        '{"type":"text","sessionID":"ses-1","part":{"text":"```turn-result\\n'
        '{\\"outcome\\":\\"COMPLETE\\",\\"summary\\":\\"ok\\"}\\n```",'
        '"tokens":{"input":13,"output":4,"cache":{"read":8}}}}'
    )
    opencode_text, opencode_session, opencode_usage = _json_text_and_session(opencode_event)
    assert '"outcome":"COMPLETE"' in opencode_text
    assert opencode_session == "ses-1"
    assert opencode_usage.input_tokens == 13
    assert opencode_usage.cached_input_tokens == 8
    assert opencode_usage.output_tokens == 4


def test_local_cli_harnesses_use_native_commands_for_the_browser_terminal():
    pi = CLIHarnessWorker(HarnessKind.PI)._command(
        AgentConfig(harness=HarnessKind.PI, model="nous/tencent/hy3:free"),
        "do the work",
        "/tmp/project",
        native=True,
    )
    opencode = CLIHarnessWorker(HarnessKind.OPENCODE)._command(
        AgentConfig(harness=HarnessKind.OPENCODE, model="opencode/deepseek-v4-flash-free"),
        "do the work",
        "/tmp/project",
        native=True,
    )
    assert "--mode" not in pi and "--print" not in pi and pi[-1] == "do the work"
    assert "run" not in opencode and "--format" not in opencode
    # The transport does not classify commands. Native-vs-machine behavior is
    # solely a launch decision owned by the harness catalog.
    assert pi[0] == "pi" and opencode[0] == "opencode"
    assert opencode[-2:] == ["--prompt", "do the work"]


def test_opencode_machine_command_keeps_prompt_positional_when_transport_can_inject():
    command = HarnessCommandFactory().worker_command(
        HarnessKind.OPENCODE,
        AgentConfig(harness=HarnessKind.OPENCODE, model="opencode/test-model"),
        "structured initial prompt",
        "/workspace/project",
        prompt_via_stdin=True,
    )
    assert command[-1] == "structured initial prompt"
    assert "-" not in command
    assert command[command.index("--format") + 1] == "json"


@pytest.mark.parametrize("harness", [HarnessKind.CLAUDE, HarnessKind.OPENCODE, HarnessKind.PI])
def test_native_harness_commands_deploy_prompt_and_resume_saved_session(harness):
    worker = CLIHarnessWorker(harness)
    fresh = worker._command(
        AgentConfig(harness=harness, model="test-model"),
        "initial prompt",
        "/workspace/project",
        native=True,
    )
    resumed = worker._command(
        AgentConfig(harness=harness, model="test-model", session_id="saved-session"),
        "follow-up prompt",
        "/workspace/project",
        native=True,
        resume=True,
    )
    assert "initial prompt" in fresh
    assert "follow-up prompt" in resumed
    assert "saved-session" in resumed
    assert "--print" not in fresh and "--mode" not in fresh
    if harness is HarnessKind.OPENCODE:
        assert fresh[-2:] == ["--prompt", "initial prompt"]
        assert resumed[-2:] == ["--prompt", "follow-up prompt"]
    else:
        assert fresh[-1] == "initial prompt"
        assert resumed[-1] == "follow-up prompt"


@pytest.mark.parametrize(
    ("harness", "session_id"),
    [
        (HarnessKind.PI, "pi-native-session"),
        (HarnessKind.OPENCODE, "ses_native_session"),
    ],
)
async def test_native_executor_persists_provider_session_before_result(
    tmp_path, monkeypatch, harness, session_id
):
    class MockNativeTransport:
        pass

    async def mock_run_until_result(_transport, _node_id, _command, **kwargs):
        (tmp_path / "native-output.txt").write_text("native output")
        kwargs["result_path"].write_text(
            '{"outcome":"COMPLETE","summary":"native executor verified"}'
        )
        await kwargs["session_callback"](session_id)
        return TerminalResult(returncode=0, output=b"\x1b[32mnative PTY\x1b[0m")

    monkeypatch.setattr(harness_module, "LocalPtyTransport", MockNativeTransport)
    monkeypatch.setattr(harness_module, "run_until_result", mock_run_until_result)
    monkeypatch.setattr(harness_module, "opencode_session_ids", lambda: [])
    seen: list[str] = []

    async def remember(value: str) -> None:
        seen.append(value)

    node = Node(
        project_id=uuid.uuid4(),
        objective="Create native-output.txt",
        generated_prompt="Create native-output.txt.",
        repo_path=str(tmp_path),
        executor=harness.value,
        agent=AgentConfig(harness=harness, model="free-test"),
    )
    result = await CLIHarnessWorker(harness).execute(
        NodeExecutionContext(
            node=node,
            repo_path=str(tmp_path),
            session_callback=remember,
        )
    )

    assert result.outcome == Outcome.COMPLETE
    assert result.session_id == session_id
    assert seen and seen[-1] == session_id


def test_reconnect_commands_use_native_provider_sessions(tmp_path):
    load_builtin_capabilities(tmp_path, ["turn-executing"])
    runner = Runner(
        Store(Path("/tmp/turn-reconnect-test")),
        WorkerRegistry(),
        EventBus(),
        Settings(),
        herdr_adapter=MockHerdrAdapter(),
    )
    project_id = uuid.uuid4()
    node = Node(
        id=uuid.uuid4(),
        project_id=project_id,
        objective="resume",
        executor="codex",
        agent=AgentConfig(model="gpt-5.6-luna", session_id="session-123"),
    )
    codex = runner._reconnect_command(node, str(tmp_path), "session-123")
    assert codex is not None
    assert codex[1] == "resume"
    assert "exec" not in codex and "--json" not in codex
    assert "project_root_markers=[]" in codex
    assert "--thinking" not in codex
    assert "model_reasoning_effort=\"default\"" not in codex

    node.agent = AgentConfig(harness=HarnessKind.PI, model="nous/tencent/hy3:free")
    pi = runner._reconnect_command(node, str(tmp_path), "pi-session")
    assert pi[:3] == ["pi", "--session", "pi-session"]
    assert "--skill" in pi
    assert pi[-2:] == ["--model", "nous/tencent/hy3:free"]

    node.agent = AgentConfig(
        harness=HarnessKind.OPENCODE,
        model="opencode/deepseek-v4-flash-free",
    )
    opencode = runner._reconnect_command(node, str(tmp_path), "opencode-session")
    assert opencode is not None
    assert opencode[:3] == ["opencode", "--session", "opencode-session"]
    assert "run" not in opencode and "--format" not in opencode and "--auto" not in opencode


async def test_user_shell_is_independent_from_node_activity(tmp_path):
    store = Store(tmp_path / "shell-state")
    await store.init()
    root = await store.create_project(
        "standalone shell",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    runner = Runner(
        store,
        WorkerRegistry(),
        EventBus(),
        Settings(data_dir=str(tmp_path / "turn-shell-state")),
        terminal_transport=MockTerminalTransport(),
    )
    initial_status = (await store.get_node(root.id)).status
    assert runner.shell is runner.terminal

    assert await runner.open_shell(root.id)
    for _ in range(100):
        if runner.shell.snapshot(root.id).get("active"):
            break
        await asyncio.sleep(0.01)
    assert runner.shell.snapshot(root.id).get("active")
    assert await runner.shell.resize(root.id, 91, 29)
    assert runner.shell.pane_id(root.id)
    assert await runner.detach_shell(root.id)
    assert await runner.shell.has_persistent_session(root.id)
    assert await runner.open_shell(root.id)
    for _ in range(100):
        if runner.shell.snapshot(root.id).get("active"):
            break
        await asyncio.sleep(0.01)
    assert runner.shell.snapshot(root.id).get("active")
    for _ in range(100):
        if runner.shell.snapshot(root.id).get("output"):
            break
        await asyncio.sleep(0.01)
    # Shell and harness access share one per-node Herdr pane. Shell
    # activity is still independent from node lifecycle state.
    assert runner.terminal.snapshot(root.id).get("active")
    assert await runner.shell.write(root.id, "printf '\\033[32mSHELL_OK\\033[0m\\n'\n")
    for _ in range(100):
        if "SHELL_OK" in runner.shell.snapshot(root.id).get("output", ""):
            break
        await asyncio.sleep(0.01)
    assert "SHELL_OK" in runner.shell.snapshot(root.id)["output"]
    assert (await store.get_node(root.id)).status == initial_status

    assert await runner.close_shell(root.id)
    assert not runner.shell.snapshot(root.id).get("active")
    assert (await store.get_node(root.id)).status == initial_status
    await store.dispose()


async def test_runtime_session_survives_run_creation_and_agent_config_save(tmp_path):
    store = Store(tmp_path / "session-state")
    await store.init()
    root = await store.create_project(
        "session continuity",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    agent = root.agent.model_copy(deep=True)
    agent.session_id = "session-keep-me"
    await store.edit_node(root.id, agent=agent)
    saved = await store.get_node(root.id)
    assert saved is not None and saved.agent is not None

    edited = saved.agent.model_copy(deep=True)
    edited.reasoning = edited.reasoning
    edited.session_id = None
    await store.edit_node(root.id, agent=edited)
    preserved = await store.get_node(root.id)
    assert preserved is not None and preserved.agent is not None
    assert preserved.agent.session_id == "session-keep-me"

    run = await store.create_run(preserved, "codex")
    assert run.session_id == "session-keep-me"
    await store.dispose()


async def test_reconnect_requires_the_node_session_not_run_history(tmp_path):
    store = Store(tmp_path / "session-state")
    await store.init()
    root = await store.create_project(
        "node-owned reconnect",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    run = await store.create_run(root, "codex")
    await store.update_run(run.id, session_id="history-only-session")
    transport = MockTerminalTransport()
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=transport,
    )

    # A run record is history, not an addressable live conversation.  Without
    # the session stored on this node, reconnect must refuse to guess—even if
    # another session id happens to be present in the node's run history.
    assert await runner.reconnect(root.id) is False
    assert runner._reconnect_tasks == {}
    await store.dispose()


async def test_manual_stop_of_retained_follow_up_requires_a_fresh_run(tmp_path):
    store = Store(tmp_path / "session-state")
    await store.init()
    root = await store.create_project(
        "stop retained follow up",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    root.agent.session_id = "retained-session"
    await store._save_node(root)
    transport = MockTerminalTransport()
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=transport,
    )
    assert await runner.reconnect(root.id)
    assert await runner.cancel(root.id) is None
    stopped = await store.get_node(root.id)
    assert stopped.status == NodeStatus.CANCELLED
    assert stopped.agent.session_id == "retained-session"
    assert not transport.snapshot(root.id)["active"]
    await store.dispose()


def test_non_codex_planner_commands_keep_resumable_sessions():
    planner = AgentPlanner(settings=Settings())
    pi = planner._command(
        AgentConfig(harness=HarnessKind.PI, session_id="pi-session"),
        "plan this",
    )
    assert "--no-session" not in pi
    assert pi[pi.index("--session-id") + 1] == "pi-session"

    opencode = planner._command(
        AgentConfig(harness=HarnessKind.OPENCODE, session_id="opencode-session"),
        "plan this",
    )
    assert opencode[1:4] == ["run", "--format", "json"]
    assert opencode[opencode.index("--session") + 1] == "opencode-session"
    assert opencode[-1] == "plan this"


async def test_agent_planner_uses_the_selected_opencode_harness(monkeypatch):
    planner = AgentPlanner(settings=Settings())
    node = Node(
        project_id=uuid.uuid4(),
        objective="Plan a puzzle room",
        executor="planner",
        agent=AgentConfig(harness=HarnessKind.OPENCODE, type_id="planner"),
    )
    observed: list[HarnessKind] = []

    async def mock_call(agent, prompt, ctx):
        observed.append(agent.harness)
        return '''```turn-plan
{"nodes":[{"key":"room","objective":"Build room","executor":"codex"}],"edges":[]}
```'''

    monkeypatch.setattr(planner, "_call_harness", mock_call)
    plan = await planner.plan(NodeExecutionContext(node=node))
    assert observed == [HarnessKind.OPENCODE]
    assert plan.nodes and plan.nodes[0].objective == "Build room"


async def test_agent_config_inherits_and_cascades(tmp_path):
    cfg, store, runner = await _runtime(tmp_path, DeterministicWorker())
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
    live = await store.descendants(root.id)
    assert all(n.agent.harness == HarnessKind.PI for n in live)
    assert all(n.agent.model == pi.model and n.agent.reasoning == pi.reasoning for n in live)

    await store.dispose()


async def test_regeneration_has_no_fork_or_revision_branch(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    runner.registry.register_planner(FixedPlanner())
    root = await store.create_project("visible root fork", run_policy=RunPolicy(auto_run=False))

    await runner.regenerate_descendants(root.id)

    nodes, edges, _ = await store.get_workgraph(root.id)
    assert all(not hasattr(node, "revision") for node in nodes)
    assert all(not artifact.name.startswith("revision-") for artifact in (await store.get_artifacts(root.id)))
    assert all(edge.src == root.id or edge.dst != root.id for edge in edges)
    await store.dispose()


async def test_cli_plan_revision_replaces_an_expanded_subtree(tmp_path, monkeypatch):
    """A follow-up plan submitted in the retained planner session is applied."""
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    runner.registry.register_planner(FixedPlanner())
    root = await store.create_project(
        "revise the lantern",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    root.agent.session_id = "planner-session"
    await store._save_node(root)
    runner.terminal.supports_inject = True

    await runner._plan_node(root, root.id)
    original = await store.descendants(root.id)
    assert [node.objective for node in original] == ["Alternative ending"]
    await store.set_agent_status(root.id, state="failed", message="stale prior rejection")

    handoff = Path(root.repo_path) / ".turn" / "interactive" / f"{root.id}.plan.json"
    status = handoff.parent / f"{root.id}.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", str(root.id))
    monkeypatch.setenv("TURN_REPO", root.repo_path)
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan", "--payload",
        json.dumps({
            "nodes": [
                {"key": "chapters", "objective": "Plan chapters", "executor": "planner", "plan": True},
                {"key": "write", "objective": "Write chapters", "executor": "deterministic", "follows": ["chapters"]},
            ],
        }),
    ])
    assert agent_command(args) == 0

    for _ in range(100):
        revised = await store.descendants(root.id)
        if {node.objective for node in revised} == {"Plan chapters", "Write chapters"}:
            break
        await asyncio.sleep(0.01)

    assert {node.objective for node in revised} == {"Plan chapters", "Write chapters"}
    assert all(node.id not in {old.id for old in original} for node in revised)
    updated_root = await store.get_node(root.id)
    assert updated_root.status == NodeStatus.EXPANDED
    assert updated_root.agent_state is None
    assert updated_root.agent_message is None
    await runner.stop()
    await store.dispose()


async def test_runner_restores_retained_handoff_watchers_after_restart(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "restore planner editability",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    root.agent.session_id = "retained-planner-session"
    await store._save_node(root)
    terminal = MockTerminalTransport()
    terminal.supports_inject = True
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=terminal,
    )

    await runner.start()
    try:
        for _ in range(100):
            if root.id in runner._handoff_watchers:
                break
            await asyncio.sleep(0.01)
        assert root.id in runner._handoff_watchers
    finally:
        await runner.stop()
        await store.dispose()


async def test_cli_result_revision_updates_a_completed_executor_session(tmp_path, monkeypatch):
    """Every retained executor conversation can publish a corrected result."""
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    root = await store.create_project(
        "revise executor work",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    [work] = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="work", objective="Implement the feature", executor="deterministic")]),
    )
    work.agent = AgentConfig(harness=HarnessKind.CODEX, session_id="executor-session")
    await store._save_node(work)
    runner.terminal.supports_inject = True

    initial_run = await store.create_run(work, "codex")
    await store.set_status(work.id, NodeStatus.RUNNING)
    await runner._handle_outcome(
        work,
        initial_run,
        root.id,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="initial implementation",
            session_id="executor-session",
        ),
    )

    handoff = Path(root.repo_path) / ".turn" / "interactive" / f"{work.id}.result.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(handoff.parent / f"{work.id}.status.json"))
    monkeypatch.setenv("TURN_NODE_ID", str(work.id))
    args = parser().parse_args([
        "agent", "submit", "--kind", "result", "--payload",
        json.dumps({
            "outcome": "COMPLETE",
            "summary": "corrected implementation",
            "artifacts": ["src/corrected.py"],
        }),
    ])
    assert agent_command(args) == 0

    for _ in range(100):
        runs = await store.get_runs(work.id)
        if len(runs) == 2:
            break
        await asyncio.sleep(0.01)

    refreshed = await store.get_node(work.id)
    assert refreshed is not None and refreshed.status is NodeStatus.COMPLETE
    assert runs[-1].summary == "corrected implementation"
    assert any(artifact.ref == "src/corrected.py" for artifact in await store.get_artifacts(work.id))
    await runner.stop()
    await store.dispose()


async def test_regeneration_closes_removed_herdr_panes_and_projects_agent_status(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    runner.registry.register_planner(FixedPlanner())
    repo = tmp_path / "project"
    root = await store.create_project(
        "status and cleanup",
        repo_path=str(repo),
        run_policy=RunPolicy(auto_run=False),
    )
    assert await runner.ensure_node_terminal(root.id)
    await runner.regenerate_descendants(root.id)
    old_child = (await store.descendants(root.id))[0]
    assert await runner.ensure_node_terminal(old_child.id)
    old_pane = runner.terminal.pane_id(old_child.id)
    assert old_pane is not None

    status_path = repo / ".turn" / "interactive" / f"{old_child.id}.status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    watcher = asyncio.create_task(
        runner._watch_agent_status(old_child.id, root.id, status_path)
    )
    status_path.write_text(
        '{"node_id":"%s","state":"working","message":"writing domain files"}'
        % old_child.id
    )
    for _ in range(100):
        current = await store.get_node(old_child.id)
        if current and current.agent_message == "writing domain files":
            break
        await asyncio.sleep(0.01)
    current = await store.get_node(old_child.id)
    assert current is not None
    assert current.agent_state == "working"
    assert current.agent_message == "writing domain files"
    watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)

    await runner.regenerate_descendants(root.id)
    assert await store.get_node(old_child.id) is None
    with pytest.raises(HerdrResourceNotFound):
        await runner.terminal.adapter.get_pane(old_pane)
    await runner.stop()
    await store.dispose()


async def test_run_again_resets_provider_session_and_forbids_reuse(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    planner = SessionPlanner("new-session")
    runner.registry.register_planner(planner)
    root = await store.create_project(
        "fresh planner run",
        agent=AgentConfig(harness=HarnessKind.OPENCODE),
        run_policy=RunPolicy(auto_run=False),
    )
    root.agent.session_id = "old-session"
    await store._save_node(root)
    await store.set_status(root.id, NodeStatus.EXPANDED)
    await store.add_artifacts(
        root.id,
        [ArtifactSpec(kind=ArtifactKind.TEXT, name="old-artifact", content="old")],
    )
    terminal = MockTerminalTransport()
    runner.terminal = terminal
    runner.shell = terminal
    await terminal.ensure_persistent_shell(root.id, cwd=str(tmp_path))
    running_process = asyncio.create_task(
        terminal.run(root.id, ["provider"], cwd=str(tmp_path))
    )
    for _ in range(100):
        if terminal.snapshot(root.id)["active"]:
            break
        await asyncio.sleep(0.01)

    result = await runner.regenerate_descendants(root.id, fresh_session=True)

    assert result["created"]
    assert planner.forbidden_session_id == "old-session"
    assert planner.seen_session_id is None
    assert planner.command is not None
    assert "--session" not in planner.command
    assert planner.command[-2:] == ["--prompt", "fresh planner prompt"]
    assert running_process.done()
    assert root.id in terminal.closed_nodes
    rerun_root = await store.get_node(root.id)
    assert rerun_root.agent.session_id == "new-session"
    run = (await store.get_runs(root.id))[-1]
    assert run.session_id == "new-session"
    assert run.error is None
    artifacts = await store.get_artifacts(root.id)
    assert [artifact.name for artifact in artifacts] == ["plan-submission"]
    await store.dispose()


async def test_stop_cancels_an_inflight_regeneration(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    started = asyncio.Event()
    planner = SessionPlanner("never-reached", started)
    runner.registry.register_planner(planner)
    runner.terminal = MockTerminalTransport()
    root = await store.create_project(
        "cancel planner run",
        agent=AgentConfig(harness=HarnessKind.MOCK),
        run_policy=RunPolicy(auto_run=False),
    )
    root.agent.session_id = "old-session"
    await store._save_node(root)
    await store.set_status(root.id, NodeStatus.EXPANDED)

    task = asyncio.create_task(runner.regenerate_descendants(root.id, fresh_session=True))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert runner.generation_active(root.id)

    await runner.cancel(root.id)
    await asyncio.gather(task, return_exceptions=True)

    cancelled = await store.get_node(root.id)
    assert cancelled.status == NodeStatus.CANCELLED
    assert not runner.generation_active(root.id)
    assert (await store.get_runs(root.id))[-1].status == RunStatus.CANCELLED
    await store.dispose()


async def test_store_preserves_explicit_planner_agent_type(tmp_path):
    store_path = tmp_path / "planner-agent"
    store = Store(store_path)
    await store.init()
    root = await store.create_project("legacy planner")
    root.agent.type_id = "executor"
    await store._save_node(root)
    await store.dispose()

    reopened = Store(store_path)
    await reopened.init()
    persisted = await reopened.get_node(root.id)
    assert persisted.agent.type_id == "executor"
    await reopened.dispose()


async def test_new_plan_children_inherit_config_but_not_parent_session(tmp_path):
    _, store, _ = await _runtime(tmp_path, DeterministicWorker())
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


async def test_plan_can_assign_integrator_specialization_without_new_harness_config(tmp_path):
    _, store, _ = await _runtime(tmp_path, DeterministicWorker())
    parent = await store.create_project(
        "assemble product",
        agent=AgentConfig(
            harness=HarnessKind.MOCK,
            model="deterministic",
            session_id="planner-thread",
        ),
        run_policy=RunPolicy(auto_run=False),
    )
    created = await store.apply_plan(
        parent,
        PlanResult(
            nodes=[
                NodeSpec(
                    key="integrate",
                    objective="Integrate product",
                    executor="deterministic",
                    agent_type=AgentType.INTEGRATOR,
                )
            ]
        ),
    )

    assert created[0].agent.type_id is AgentType.INTEGRATOR
    assert created[0].agent.harness is HarnessKind.MOCK
    assert created[0].agent.model == "deterministic"
    assert created[0].agent.session_id is None
    await store.dispose()


async def test_explicit_same_harness_keeps_dynamic_model_assignment(tmp_path):
    _, store, _ = await _runtime(tmp_path, DeterministicWorker())
    parent = await store.create_project(
        "explicit adapter",
        agent=AgentConfig(
            harness=HarnessKind.MOCK,
            model="deterministic",
            reasoning=ReasoningLevel.DEFAULT,
            session_id="planner-thread",
        ),
        run_policy=RunPolicy(auto_run=False),
    )
    created = await store.apply_plan(
        parent,
        PlanResult(nodes=[NodeSpec(key="child", objective="Implement leaf", executor="deterministic")]),
    )
    assert created[0].agent.harness == HarnessKind.MOCK
    assert created[0].agent.model == "deterministic"
    assert created[0].agent.session_id is None
    await store.dispose()


async def test_scheduler_cancels_child_created_after_parent_cancellation(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    cancelled_parent = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Cancelled parent",
        executor="planner",
        status=NodeStatus.CANCELLED,
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    late_child = await store.create_node(
        project_id=root.id,
        parent_id=cancelled_parent.id,
        objective="Late verifier replacement",
        executor="deterministic",
        status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    worker = asyncio.create_task(asyncio.Event().wait())
    runner._running[late_child.id] = worker

    await runner._schedule_project(root.id)

    repaired = await store.get_node(late_child.id)
    assert repaired.status == NodeStatus.CANCELLED
    assert worker.cancelled()
    await store.dispose()


async def test_scheduler_terminalizes_persisted_running_rows_without_live_tasks(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    orphan = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Interrupted work",
        status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.MOCK, session_id="stale-session"),
    )
    live = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Owned work",
        status=NodeStatus.RUNNING, agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    orphan_run = await store.create_run(orphan, "deterministic")
    await runner.terminal.ensure_persistent_shell(orphan.id, cwd=str(tmp_path))
    live_run = await store.create_run(live, "deterministic")
    live_task = asyncio.create_task(asyncio.Event().wait())
    runner._running[live.id] = live_task

    await runner._schedule_project(root.id)

    assert (await store.get_runs(orphan.id))[-1].status == RunStatus.CANCELLED
    repaired_orphan = await store.get_node(orphan.id)
    assert repaired_orphan.status == NodeStatus.RUNNABLE
    assert repaired_orphan.agent.session_id == "stale-session"
    assert await runner.terminal.has_persistent_session(orphan.id)
    assert (await store.get_runs(live.id))[-1].status == RunStatus.RUNNING
    assert orphan_run.id != live_run.id
    live_task.cancel()
    await asyncio.gather(live_task, return_exceptions=True)
    await store.dispose()


async def test_retry_prepares_a_fresh_provider_call(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    runner.terminal = MockTerminalTransport()
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    node = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Retryable work",
        status=NodeStatus.FAILED,
        agent=AgentConfig(harness=HarnessKind.MOCK, session_id="stale-session"),
    )
    await runner.terminal.ensure_persistent_shell(node.id, cwd=str(tmp_path))

    await runner.retry(node.id)

    retried = await store.get_node(node.id)
    assert retried.status == NodeStatus.RUNNABLE
    assert retried.agent.session_id is None
    assert node.id in runner.terminal.close_requests
    assert not await runner.terminal.has_persistent_session(node.id)
    await store.dispose()


async def test_late_failure_retries_a_running_node(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    node = await store.create_node(
        project_id=root.id, parent_id=root.id, objective="Node while running",
        executor="deterministic", status=NodeStatus.RUNNING,
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    run = await store.create_run(node, "deterministic", 1)

    await runner._handle_outcome(
        node, run, root.id,
        WorkerResult(outcome=Outcome.FAIL, summary="late failure", retry_recommended=True),
    )

    repaired = await store.get_node(node.id)
    saved_run = (await store.get_runs(node.id))[-1]
    assert repaired.status == NodeStatus.RUNNABLE
    assert saved_run.status == RunStatus.FAILED
    assert runner._retries.get(node.id, 0) == 1
    await store.dispose()


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

    async def mock_subprocess(*args, **kwargs):
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return Process()

    monkeypatch.setattr(planner_module.shutil, "which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(planner_module.asyncio, "create_subprocess_exec", mock_subprocess)
    cfg = Settings()
    planner = CodexPlanner(settings=cfg)
    agent = AgentConfig(harness=HarnessKind.CODEX, session_id="planner-session")
    terminal = StubTerminal(b'{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n', captured)
    _, _, session = await planner._call_codex("revise plan", str(tmp_path), agent=agent, terminal=terminal)

    args = captured["args"]
    assert args[:4] == (cfg.codex_binary, "exec", "--json", "resume")
    assert "project_root_markers=[]" in args
    assert "--ephemeral" not in args
    assert "planner-session" in args
    assert captured["cwd"] == str(tmp_path)
    assert session == "planner-session"


async def test_graph_explorer_is_read_only_and_integrators_get_glue_contract(tmp_path):
    data_dir = tmp_path / "turn"
    store = Store(data_dir)
    await store.init()
    root = await store.create_project("Assemble the adventure")
    ctx = NodeExecutionContext(node=root)
    block = render_context_block(ctx)
    assert block.startswith("TURN_CONTEXT\n")
    assert shutil.which("turn") is not None
    assert "graph_explorer.py" not in block
    assert "turn graph" not in block
    assert "--state-file" not in block
    assert "INTEGRATOR CONTRACT" not in block
    assert "integrator-only directory" not in block
    project_root = store.project_path(root.id)
    assert project_root is not None
    basics = project_root / ".turn" / "capabilities" / "turn-basics" / "skills" / "turn-basics" / "SKILL.md"
    integrating = Path.cwd() / "turn" / "capabilities" / "builtin" / "turn-integrating" / "skills" / "turn-integrating" / "SKILL.md"
    assert "turn graph" in basics.read_text()
    assert "integrator-specific directory" in integrating.read_text()

    state_file = data_dir / "projects" / f"proj-{root.id.hex[:8]}" / ".turn" / "state.json"
    await graph_explorer._query(str(state_file), str(root.id), str(root.id), "tree")
    cli_run = subprocess.run(
        [
            shutil.which("turn"),
            "graph",
            str(root.id),
            "--requester",
            str(root.id),
            "--tree",
        ],
        cwd=state_file.parent.parent,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Assemble the adventure" in cli_run.stdout
    state = json.loads(state_file.read_text())
    assert "audits" not in state
    await store.dispose()


async def test_graph_explorer_exposes_full_coordination_state(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    root = await store.create_project("Original user intention")
    worker = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Build the domain",
        generated_prompt="Implement the domain contract from the original intention.",
        executor="deterministic",
        agent=AgentConfig(
            harness=HarnessKind.MOCK,
            model="deterministic",
            session_id="worker-session",
        ),
        status=NodeStatus.RUNNING,
    )
    dependent = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Integrate the product",
        generated_prompt="Read the domain output and assemble the product.",
        executor="deterministic",
        agent=AgentConfig(harness=HarnessKind.MOCK, model="deterministic"),
        status=NodeStatus.BLOCKED,
    )
    state = store._states[root.id]
    dependency = Edge(src=worker.id, dst=dependent.id, type=EdgeType.FOLLOWS)
    state["edges"][dependency.id] = dependency
    await store._persist_project(root.id)
    await store.set_agent_status(worker.id, state="working", message="writing the domain")
    run = await store.create_run(worker, "deterministic")
    await store.update_run(run.id, summary="Started domain work", session_id="worker-session")
    await store.add_artifacts(worker.id, [ArtifactSpec(kind=ArtifactKind.FILE, name="domain.py")])

    state_file = tmp_path / "turn" / "projects" / f"proj-{root.id.hex[:8]}" / ".turn" / "state.json"
    nodes, children = await graph_explorer._query(str(state_file), str(root.id), str(worker.id), "tree")
    by_id = {item["id"]: item for item in nodes}
    root_view = by_id[str(root.id)]
    worker_view = by_id[str(worker.id)]
    dependent_view = by_id[str(dependent.id)]

    # A worker can inspect the root request and sibling instructions through
    # the read-only graph explorer; requester is metadata, not an ACL filter.
    assert root_view["objective"] == "Original user intention"
    assert root_view["instructions"] == "Original user intention"
    assert worker_view["instructions"] == "Implement the domain contract from the original intention."
    assert dependent_view["instructions"] == "Read the domain output and assemble the product."
    assert worker_view["agent"]["harness"] == "mock"
    assert worker_view["agent"]["model"] == "deterministic"
    assert worker_view["session_id"] == "worker-session"
    assert worker_view["agent_state"] == "working"
    assert worker_view["agent_message"] == "writing the domain"
    assert worker_view["runs"][0]["session_id"] == "worker-session"
    assert worker_view["files"] == ["domain.py"]
    assert dependent_view["follows"] == [str(worker.id)]
    assert children[root.id.hex] == [worker.id.hex, dependent.id.hex]
    summary = graph_explorer._summary(worker_view)
    assert str(worker.id) in summary
    assert "worker-session" in summary
    assert "Implement the domain contract" in summary
    await store.dispose()


def test_planner_honors_editable_planning_instructions():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Create a tiny adventure",
        generated_prompt="Use two nested planning branches and a small integration leaf.",
    )
    prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=node))
    assert "instructions=Use two nested planning branches" in prompt


def test_initial_setup_planner_chooses_a_domain_appropriate_board():
    root = Node(
        project_id=uuid.uuid4(),
        objective="Launch a small software product",
        generated_prompt="Build a product for a clearly defined audience.",
    )
    root_prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=root))

    assert "instructions=Build a product for a clearly defined audience." in root_prompt
    assert "Set up the board" not in root_prompt

    nested = Node(
        project_id=root.project_id,
        parent_id=root.id,
        objective="Development architecture",
        generated_prompt="Turn the validated product direction into an implementation plan.",
    )
    nested_prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=nested))
    assert "instructions=Turn the validated product direction into an implementation plan." in nested_prompt
    assert "SCOPED PLANNING" not in nested_prompt


def test_root_planner_rejects_a_narrow_leaf_for_an_app_factory():
    root = Node(
        project_id=uuid.uuid4(),
        objective="Build an app factory",
        generated_prompt="Create the organization and platform for an entire organization.",
    )
    narrow_plan = PlanResult(
        nodes=[NodeSpec(key="research", objective="Research the market", executor="codex")]
    )

    with pytest.raises(RuntimeError, match="organization-scale request"):
        CodexPlanner._validate_setup_scope(NodeExecutionContext(node=root), narrow_plan)

    nested_plan = PlanResult(
        nodes=[
            NodeSpec(
                key="factory_setup",
                objective="Plan the app factory",
                executor="planner",
                agent_type="planner",
                plan=True,
            )
        ]
    )
    CodexPlanner._validate_setup_scope(NodeExecutionContext(node=root), nested_plan)


def test_setup_plan_shape_preserves_stage_handoffs():
    payload = {
        "project_name": "Software Product",
        "nodes": [
            {
                "key": "market_research",
                "objective": "Research the market",
                "executor": "codex",
                "agent_type": "executor",
                "capabilities": ["secret-word"],
            },
            {
                "key": "market_validation",
                "objective": "Validate market research",
                "executor": "codex",
                "agent_type": "verifier",
                "follows": ["market_research"],
            },
            {
                "key": "ui_ux_design",
                "objective": "Design the experience",
                "executor": "codex",
                "agent_type": "executor",
                "follows": ["market_validation"],
                "capabilities": ["turn-executing"],
            },
            {
                "key": "development_architecture",
                "objective": "Plan development",
                "executor": "planner",
                "agent_type": "planner",
                "plan": True,
                "follows": ["ui_ux_design"],
                "capabilities": ["turn-planning"],
            },
            {
                "key": "distribution_planning",
                "objective": "Plan distribution",
                "executor": "planner",
                "agent_type": "planner",
                "plan": True,
                "follows": ["development_architecture"],
                "capabilities": ["turn-integrating"],
            },
        ],
    }
    plan = AgentPlanner._parse_plan(json.dumps(payload), "Launch a software product")

    assert plan is not None
    assert plan.project_name == "Software Product"
    assert [node.key for node in plan.nodes] == [
        "market_research",
        "market_validation",
        "ui_ux_design",
        "development_architecture",
        "distribution_planning",
    ]
    assert plan.nodes[1].agent_type is AgentType.VERIFIER
    assert plan.nodes[3].agent_type is AgentType.PLANNER and plan.nodes[3].plan
    assert plan.nodes[4].follows == ["development_architecture"]
    assert plan.document_refs == []
    assert all(not node.artifacts for node in plan.nodes)
    assert [node.capabilities for node in plan.nodes] == [
        ["secret-word"],
        [],
        ["turn-executing"],
        ["turn-planning"],
        ["turn-integrating"],
    ]


def test_initial_setup_planner_can_use_nested_planners_for_broad_stages():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Build a product",
        generated_prompt="Build a product for one user request.",
    )
    prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=node))
    assert "instructions=Build a product for one user request." in prompt
    assert "complete finished product" not in prompt


def test_planner_only_uses_limited_delivery_scope_when_user_requests_it():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Build an MVP of the product",
        generated_prompt="Build an MVP with the smallest demonstrable slice.",
    )
    prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=node))
    assert "instructions=Build an MVP with the smallest demonstrable slice." in prompt
    assert "complete finished product" not in prompt


def test_planner_requires_capability_research_and_visual_references_when_relevant():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Build a visual interactive game",
        generated_prompt="Create a coherent playable visual experience.",
    )
    prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=node))
    assert "turn capabilities search" not in prompt
    assert "image embeds" not in prompt


def test_planner_does_not_turn_project_prompt_into_a_capability():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Build a greeting CLI",
        generated_prompt=(
            "Create the complete command, tests, launch instructions, and acceptance evidence."
        ),
    )
    prompt = CodexPlanner()._build_prompt(NodeExecutionContext(node=node))

    assert "instructions=Create the complete command" in prompt
    assert "do not create a project capability merely to carry the assignment" not in prompt


def test_worker_context_delivers_assignment_and_live_graph_access():
    from turn.workers.codex_worker import CodexWorker

    node = Node(
        project_id=uuid.uuid4(),
        objective="Build a greeting CLI",
        generated_prompt="Write the executable command and tests.",
    )
    prompt = CodexWorker()._build_prompt(NodeExecutionContext(node=node))

    assert "objective=Build a greeting CLI" in prompt
    assert "instructions=Write the executable command and tests." in prompt
    assert "GRAPH EXPLORATION TOOL" not in prompt
    assert "turn graph" not in prompt


def test_codex_worker_keeps_handoff_protocol_in_turn_basics_skill(tmp_path):
    from turn.workers.codex_worker import CodexWorker

    load_builtin_capabilities(tmp_path, ["turn-basics"])
    node = Node(
        project_id=uuid.uuid4(),
        objective="Create an artifact",
        generated_prompt="Create the requested artifact.",
        agent=AgentConfig(type_id=AgentType.EXECUTOR, harness=HarnessKind.CODEX),
    )
    prompt = CodexWorker()._build_prompt(
        NodeExecutionContext(node=node, repo_path=str(tmp_path))
    )

    assert "$turn-basics" in prompt
    assert "TURN HANDOFF GATE" not in prompt
    assert "turn agent submit" not in prompt
    assert "turn agent verify" not in prompt
    assert "never write Turn protocol files directly" not in prompt
    assert len(prompt) < 700


def test_codex_choked_output_is_not_a_false_success():
    from turn.workers.codex_worker import CodexWorker

    # A choked/non-structured result must be a clean FAIL, never a false
    # success. Automatic respawn is disabled by design (a node is only
    # re-run on an explicit user action), so the worker does not recommend a
    # retry here.
    parsed = CodexWorker()._parse_result("I will inspect the project now.")
    assert parsed.outcome == Outcome.FAIL
    assert parsed.retry_recommended is False
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


def test_graph_tool_json_cannot_be_mistaken_for_a_worker_plan():
    graph_output = '{"nodes":[{"id":"existing","objective":"already built"}],"edges":[]}'
    assert parsing.first_plan_json(graph_output) is None
    fenced = '```turn-plan\n{"nodes":[{"key":"new","objective":"new work"}],"edges":[]}\n```'
    assert parsing.first_plan_json(fenced)["nodes"][0]["key"] == "new"
    mixed = graph_output + '\n```turn-result\n{"outcome":"COMPLETE","summary":"verified"}\n```'
    assert parsing.first_result_json(mixed)["summary"] == "verified"


def test_protocol_envelope_is_removed_from_human_run_summary():
    summary = (
        "Tests passed.\n\n"
        '```turn-result\n{"outcome":"COMPLETE","summary":"Duplicate"}\n```'
    )
    assert parsing.clean_summary(summary) == "Tests passed."


def test_bare_schema_plan_is_accepted_and_explicit_edges_follow_domain_direction():
    bare = '{"nodes":[{"key":"a","objective":"write story"},{"key":"b","objective":"build UI"}],"edges":[{"src":"a","dst":"b"}]}'
    assert parsing.first_plan_json(bare)["nodes"][0]["key"] == "a"
    plan = AgentPlanner._parse_plan(bare)
    assert plan is not None
    assert plan.nodes[0].follows == []
    assert plan.nodes[1].follows == ["a"]


def test_plan_parser_preserves_sequence_when_nodes_are_not_topologically_ordered():
    payload = {
        "nodes": [
            {"key": "development", "objective": "Plan development", "follows": ["research"]},
            {"key": "research", "objective": "Research the product"},
        ]
    }

    plan = AgentPlanner._parse_plan(json.dumps(payload))

    assert plan is not None
    assert plan.nodes[0].follows == ["research"]


def test_plan_parser_accepts_document_only_and_empty_graph_handoffs():
    plan = AgentPlanner._parse_plan(json.dumps({
        "nodes": [],
        "document_refs": [{"ref": "docs/architecture.md"}],
        "artifacts": ["docs/architecture.md"],
    }))

    assert plan is not None
    assert plan.nodes == []
    assert [item.ref for item in plan.document_refs] == ["docs/architecture.md"]
    assert [item.ref for item in plan.artifacts] == ["docs/architecture.md"]


def test_plan_parser_migrates_prompt_sized_labels_to_prompt_copy():
    long_label = "x" * 73
    plan = AgentPlanner._parse_plan(json.dumps({
        "nodes": [{
            "key": "work",
            "objective": long_label,
        }],
    }))

    assert plan is not None
    assert len(plan.nodes[0].objective) <= 72
    assert plan.nodes[0].generated_prompt == long_label


def test_agent_plan_parser_preserves_specializations_and_nested_planners():
    payload = {
        "nodes": [
            {
                "key": "world",
                "objective": "Build world",
                "executor": "codex",
                "agent_type": "executor",
                "plan": False,
            },
            {
                "key": "systems",
                "objective": "Build systems",
                "executor": "planner",
                "agent_type": "planner",
                "plan": True,
            },
            {
                "key": "integrate",
                "objective": "Integrate game",
                "executor": "codex",
                "agent_type": "integrator",
                "follows": ["world", "systems"],
            },
        ],
        "edges": [],
    }
    plan = AgentPlanner._parse_plan(json.dumps(payload), "Build game")

    assert plan is not None
    assert plan.nodes[0].agent_type is AgentType.EXECUTOR
    assert plan.nodes[1].agent_type is AgentType.PLANNER
    assert plan.nodes[1].plan is True
    assert plan.nodes[2].agent_type is AgentType.INTEGRATOR
    assert plan.nodes[2].follows == ["world", "systems"]

    from turn.workers.codex_worker import CodexWorker

    worker_plan = CodexWorker._to_plan(payload)
    assert worker_plan.nodes[1].agent_type is AgentType.PLANNER
    assert worker_plan.nodes[1].plan is True
    assert worker_plan.nodes[2].agent_type is AgentType.INTEGRATOR


def test_final_structured_worker_result_wins():
    messages = (
        '{"outcome":"COMPLETE","summary":"I will inspect"}\n'
        '{"outcome":"COMPLETE","summary":"finished integration"}'
    )
    assert parsing.first_result_json(messages)["summary"] == "finished integration"


def test_workers_have_no_parent_verifier_path():
    source = (Path(__file__).parents[1] / "workers" / "codex_worker.py").read_text()
    assert "PARENT VERIFICATION TASK" not in source
    assert 'ctx.purpose == "verify"' not in source
    assert 'type_id == "validator"' not in source
    assert "is_verification" not in source


def test_local_harnesses_share_the_bidirectional_terminal_transport():
    workers = Path(__file__).parents[1] / "workers"
    for name in ("codex_worker.py", "planner.py", "harnesses.py"):
        source = (workers / name).read_text()
        assert ".run(" in source and "LocalPtyTransport" in source, name


async def test_harness_switch_clears_provider_session_in_store(tmp_path):
    store = Store(tmp_path / "session")
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


async def test_scheduler_snapshot_cannot_regress_a_fresh_complete_node(tmp_path):
    store = Store(tmp_path / "scheduler-race")
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
    runner = Runner(store, WorkerRegistry(), EventBus(), Settings(), herdr_adapter=MockHerdrAdapter())
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
        agent=AgentConfig(harness=HarnessKind.MOCK),
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


async def test_terminal_launch_failure_cannot_leave_node_running(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="provider launch failure",
        executor="deterministic",
        status=NodeStatus.RUNNABLE,
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )

    async def unavailable(_node_id):
        return False

    # Simulate the terminal adapter reporting that its pane could not be
    # allocated. The scheduler must settle the node instead of allowing the
    # worker to run and leaving the UI's RUNNING projection behind.
    runner.node_executor.ensure_terminal = unavailable
    await runner.run_node(child.id)
    for _ in range(100):
        current = await store.get_node(child.id)
        if current is not None and current.status == NodeStatus.FAILED and not runner.generation_active(child.id):
            break
        await asyncio.sleep(0.01)

    failed = await store.get_node(child.id)
    assert failed is not None
    assert failed.status == NodeStatus.FAILED
    assert not runner.generation_active(child.id)
    assert child.id not in runner.active_node_ids()
    await store.dispose()


async def test_run_after_manual_stop_starts_a_fresh_provider_session(tmp_path):
    class SessionWorker(Worker):
        name = "deterministic"

        def __init__(self):
            self.seen_sessions: list[str | None] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
            self.seen_sessions.append(ctx.node.agent.session_id if ctx.node.agent else None)
            if len(self.seen_sessions) == 1:
                self.first_started.set()
                await self.release_first.wait()
            return WorkerResult(
                outcome=Outcome.COMPLETE,
                summary="session boundary verified",
                session_id=f"new-session-{len(self.seen_sessions)}",
            )

    worker = SessionWorker()
    _, store, runner = await _runtime(tmp_path, worker)
    runner.terminal = MockTerminalTransport()
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    node = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="manual rerun",
        executor="deterministic",
        status=NodeStatus.RUNNABLE,
        agent=AgentConfig(harness=HarnessKind.MOCK, session_id="old-session"),
    )

    await runner.run_node(node.id)
    await asyncio.wait_for(worker.first_started.wait(), timeout=1)
    await runner.cancel(node.id)

    stopped = await store.get_node(node.id)
    assert stopped.status == NodeStatus.CANCELLED
    assert stopped.agent.session_id == "old-session"

    await runner.run_node(node.id)
    for _ in range(100):
        if len(worker.seen_sessions) == 2 and node.id not in runner._running:
            break
        await asyncio.sleep(0.01)

    rerun = await store.get_node(node.id)
    assert worker.seen_sessions == ["old-session", None]
    assert rerun.agent.session_id == "new-session-2"
    assert node.id in runner.terminal.close_requests
    await store.dispose()


async def test_disconnected_terminal_output_is_not_persisted_after_release(tmp_path):
    store = Store(tmp_path / "terminal-output")
    await store.init()
    root = await store.create_project("root", repo_path=str(tmp_path / "project"))
    runner = Runner(store, WorkerRegistry(), EventBus(), Settings(), herdr_adapter=MockHerdrAdapter())
    # The live terminal may contain output while it is attached, but that
    # output belongs to Herdr's session log, not Turn's graph state.
    runner.terminal = LocalPtyTransport()

    await runner.terminal.run(
        root.id,
        [
            sys.executable,
            "-c",
            "print('\\x1b[32mcolored output\\x1b[0m', flush=True)",
        ],
        cwd=str(tmp_path),
        timeout=5,
    )
    assert runner.terminal.release(root.id)

    artifacts = await store.get_artifacts(root.id)
    assert all(a.name not in {"transcript", "codex-output"} for a in artifacts)
    await store.dispose()


async def test_resume_respects_step_and_auto_modes(tmp_path):
    _, store, runner = await _runtime(tmp_path, DeterministicWorker())
    root = await store.create_project("root", run_policy=RunPolicy(auto_run=False))
    await store.set_status(root.id, NodeStatus.EXPANDED)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="resumable",
        executor="deterministic",
        agent=AgentConfig(harness=HarnessKind.MOCK),
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
