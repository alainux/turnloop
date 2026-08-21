from __future__ import annotations

import io
import json
import uuid

import pytest

from turn.__main__ import _project_info, organization_command, parser, work_command
from turn.__main__ import discover_project_state
from turn.config import Settings
from turn.db.store import Store
from turn.core import TurnCore
from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, Node, NodeSpec, PlanResult, RunPolicy
from turn.logging import EventLog
from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport


def test_cli_exposes_headless_commands_and_policy_flags():
    parsed = parser().parse_args(
        ["create", "build it", "--harness", "pi", "--reasoning", "high", "--manual", "--run"]
    )
    assert parsed.command == "create"
    assert parsed.harness == "pi" and parsed.reasoning == "high"
    assert parsed.manual and parsed.run
    assert parser().parse_args(["doctor"]).command == "doctor"
    assert parser().parse_args(["capabilities", "delete", "created"]).capabilities_command == "delete"
    assert parser().parse_args(["project", "info"]).project_command == "info"
    assert parser().parse_args(["serve", "--port", "9000"]).port == 9000
    assert parser().parse_args(["work", "list"]).work_command == "list"
    assert parser().parse_args(["tickets", "claim", str(uuid.uuid4())]).work_command == "claim"
    assert parser().parse_args(["org", "show"]).organization_command == "show"


@pytest.mark.asyncio
async def test_cli_exposes_real_work_items_and_organization_metrics(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "turn-data"
    projects_dir = tmp_path / "projects"
    monkeypatch.setattr("turn.__main__.settings.data_dir", str(data_dir))
    monkeypatch.setattr("turn.__main__.settings.projects_dir", str(projects_dir))
    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project(
        "Deliver a complete interactive workgraph",
        repo_path=str(projects_dir / "workgraph"),
    )
    created = await store.apply_plan(root, PlanResult(nodes=[
        NodeSpec(key="product", objective="Run product", agent_type=AgentType.PLANNER),
        NodeSpec(key="verify", objective="Verify", agent_type=AgentType.VERIFIER, follows=["product"]),
    ]))
    await store.dispose()

    await work_command(parser().parse_args(["work", "list", str(root.id)]))
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 2
    item_id = uuid.UUID(listed[0]["id"])
    await work_command(parser().parse_args(["work", "claim", str(item_id)]))
    claimed = json.loads(capsys.readouterr().out)
    assert claimed["status"] == "CLAIMED"

    await organization_command(parser().parse_args(["organization", "show", str(root.id)]))
    organization = json.loads(capsys.readouterr().out)
    assert organization["metrics"]["boundary_count"] == 2
    assert organization["organizations"]


def test_project_info_exposes_defaults_and_native_harness_surfaces(tmp_path, monkeypatch):
    project_id = uuid.uuid4()
    project = tmp_path / "project"
    state_file = project / ".turn" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({
        "project_id": str(project_id),
        "nodes": [Node(
            project_id=project_id,
            objective="Native capability demo",
            project_name="Native capability demo",
            repo_path=str(project),
            agent=AgentConfig(type_id="planner", harness="codex", model="gpt-test"),
        ).model_dump(mode="json")],
        "edges": [],
        "runs": [],
        "artifacts": [],
    }))
    data_dir = tmp_path / "turn-data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(json.dumps({
        "settings": {
            "agent_defaults": json.dumps({
                "planner": {"harness": "claude", "model": "claude-test", "reasoning": "high"},
                "executor": {"harness": "codex", "model": "", "reasoning": "default"},
                "integrator": {"harness": "codex", "model": "", "reasoning": "default"},
                "verifier": {"harness": "codex", "model": "", "reasoning": "default"},
            })
        }
    }))
    monkeypatch.chdir(project)
    monkeypatch.setenv("TURN_DATA_DIR", str(data_dir))

    info = _project_info()

    assert info["project"]["id"] == str(project_id)
    assert info["root_agent"]["harness"] == "codex"
    assert info["agent_defaults"]["planner"]["harness"] == "claude"
    assert info["harnesses"]["codex"]["native"]["skill_activator"] == "$<skill-id>"
    assert "codex mcp list" in info["harnesses"]["codex"]["native"]["mcp_discovery_commands"]
    assert info["harnesses"]["pi"]["native"]["mcp_discovery_commands"] == []


def test_graph_cli_discovers_the_nearest_parent_project_state(tmp_path):
    project = tmp_path / "project"
    nested = project / "src" / "package"
    nested.mkdir(parents=True)
    state = project / ".turn" / "state.json"
    state.parent.mkdir()
    state.write_text("{}")

    assert discover_project_state(nested) == state

    nearer = nested / ".turn" / "state.json"
    nearer.parent.mkdir()
    nearer.write_text("{}")
    assert discover_project_state(nested) == nearer


async def test_headless_run_explicitly_drives_a_manual_project(tmp_path):
    cfg = Settings()
    cfg.data_dir = str(tmp_path / "turn")
    cfg.projects_dir = str(tmp_path / "projects")
    cfg.planner = "heuristic"
    cfg.default_executor = "deterministic"
    cfg.runner_tick_seconds = 0.001
    async with TurnCore(
        cfg,
        test_mode=True,
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
    ) as core:
        project = await core.create_project(
            "Create a compact deterministic demo",
            agent=AgentConfig(harness=HarnessKind.MOCK),
            run_policy=RunPolicy(auto_run=False),
        )
        from turn.tests.capability_fixtures import load_builtin_capabilities

        load_builtin_capabilities(project.repo_path)
        nodes = await core.run_until_settled(project.id, max_rounds=100)
        root = next(node for node in nodes if node.id == project.id)
        assert root.auto_run is True
        assert not any(node.status.value in {"PENDING", "RUNNABLE", "RUNNING"} for node in nodes)
        assert all(node.status.value == "COMPLETE" for node in nodes)


def test_agent_cli_writes_atomic_status_and_result_handoffs(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.result.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", "node-1")

    args = parser().parse_args([
        "agent", "status", "--state", "working", "--message", "editing files"
    ])
    assert agent_command(args) == 0
    assert json.loads(status.read_text()) == {
        "node_id": "node-1", "state": "working", "message": "editing files"
    }

    args = parser().parse_args([
        "agent", "submit", "--kind", "result",
        "--stdin",
    ])
    monkeypatch.setattr("sys.stdin", io.StringIO('{"outcome":"COMPLETE","summary":"done","artifacts":["src"]}'))
    assert agent_command(args) == 0
    assert json.loads(handoff.read_text()) == {
        "outcome": "COMPLETE", "summary": "done", "artifacts": ["src"]
    }
    assert json.loads(status.read_text())["state"] == "submitted"


def test_agent_cli_replaces_prior_plan_handoff_atomically(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.plan.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", "node-1")
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    from turn.tests.capability_fixtures import load_builtin_capabilities

    load_builtin_capabilities(tmp_path)
    first = {"nodes": [{"key": "old", "objective": "Old branch"}]}
    second = {
        "project_name": "Revised project",
        "nodes": [
            {"key": "chapters", "objective": "Plan chapters", "executor": "planner", "plan": True},
            {"key": "write", "objective": "Write chapters", "executor": "deterministic", "follows": ["chapters"]},
        ],
    }

    args = parser().parse_args(["agent", "submit", "--kind", "plan", "--stdin"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(first)))
    assert agent_command(args) == 0
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan", "--payload", json.dumps(second)
    ])
    assert agent_command(args) == 0

    assert json.loads(handoff.read_text()) == second
    assert json.loads(status.read_text())["state"] == "submitted"


def test_agent_cli_does_not_clobber_a_handoff_on_malformed_stdin(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.plan.json"
    original = {"nodes": [{"key": "old", "objective": "Old branch"}]}
    handoff.write_text(json.dumps(original))
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))

    args = parser().parse_args(["agent", "submit", "--kind", "plan", "--stdin"])
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    with pytest.raises(SystemExit, match="invalid agent submission"):
        agent_command(args)

    assert json.loads(handoff.read_text()) == original


def test_agent_cli_accepts_verification_stdin_and_completes_status(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.verification.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", "node-1")
    payload = {
        "decision": "APPROVE",
        "summary": "verified",
        "findings": [],
        "required_changes": [],
        "evidence_refs": [],
        "target_node_id": None,
    }

    args = parser().parse_args(["agent", "verify", "--stdin"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert agent_command(args) == 0
    assert json.loads(handoff.read_text()) == payload
    assert json.loads(status.read_text())["state"] == "submitted"


def test_verification_parser_promotes_structured_evidence_refs():
    from turn.contracts.dag import parse_verification

    embedded = {
        "criterion_id": "journey",
        "status": "PASS",
        "summary": "The complete user journey was exercised.",
        "refs": ["README.md", "tests/test_cli.py"],
    }
    result = parse_verification({
        "decision": "APPROVE",
        "summary": "verified",
        "evidence_refs": [embedded, json.dumps({
            "criterion_id": "quality",
            "status": "PASS",
            "summary": "The quality checks passed.",
            "refs": ["tests/"],
        }), "README.md"],
    })

    assert [item.criterion_id for item in result.evidence] == ["journey", "quality"]
    assert result.evidence_refs == ["README.md"]


def test_agent_cli_rejects_json_that_does_not_match_the_turn_contract(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.result.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))

    args = parser().parse_args([
        "agent", "submit", "--kind", "result",
        "--payload", '{"outcome":"COMPLETE","summary":42}',
    ])
    with pytest.raises(SystemExit, match="invalid result submission"):
        agent_command(args)
    assert not handoff.exists()

    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"a","objective":"A","follows":["b"]},'
            '{"key":"b","objective":"B","follows":["a"]}],"edges":[]}'
        ),
    ])
    with pytest.raises(SystemExit, match="invalid plan submission"):
        agent_command(args)


def test_agent_cli_rejects_a_plan_capability_missing_from_the_project(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.plan.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"work","objective":"Work",'
            '"capabilities":["visual-qa"]}]}'
        ),
    ])

    with pytest.raises(SystemExit, match="visual-qa.*not loaded"):
        agent_command(args)
    assert not handoff.exists()


def test_agent_cli_rejects_a_model_not_in_the_selected_harness_catalog(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.plan.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    monkeypatch.setattr(
        "turn.workers.harnesses.harness_capabilities",
        lambda: [{"id": "opencode", "models": [{"id": "opencode/gpt-5.6-luna"}]}],
    )
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"work","objective":"Work",'
            '"agent":{"harness":"opencode","model":"gpt-5.6-luna"}}]}'
        ),
    ])

    with pytest.raises(SystemExit, match="incorrect model.*did you mean: opencode/gpt-5.6-luna"):
        agent_command(args)
    assert not handoff.exists()


def test_agent_cli_requires_capability_plugins_in_the_project(tmp_path, monkeypatch):
    from turn.__main__ import agent_command
    from turn.tests.capability_fixtures import load_builtin_capabilities

    handoff = tmp_path / "node.plan.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"work","objective":"Work",'
            '"capabilities":["secret-word"]}]}'
        ),
    ])

    load_builtin_capabilities(tmp_path, ["secret-word", "turn-executing"])
    assert agent_command(args) == 0
    assert json.loads(handoff.read_text())["nodes"][0]["capabilities"] == ["secret-word"]


async def test_capabilities_cli_loads_a_builtin_into_turn_repo(tmp_path, monkeypatch):
    from turn.__main__ import async_main

    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    args = parser().parse_args(["capabilities", "load", "secret-word"])

    assert await async_main(args) == 0
    assert (tmp_path / ".turn" / "capabilities" / "secret-word" / "plugin.json").is_file()


async def test_capabilities_cli_deletes_a_catalog_package(tmp_path, monkeypatch):
    from turn.__main__ import async_main
    from turn.tests.capability_fixtures import load_builtin_capabilities

    data_dir = tmp_path / "turn-data"
    load_builtin_capabilities(tmp_path, ["secret-word"])
    catalog_root = data_dir / "capabilities"
    catalog_root.mkdir(parents=True)
    source = tmp_path / "created"
    skill = source / "skills" / "created"
    skill.mkdir(parents=True)
    (source / "plugin.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "created",
        "description": "Created for deletion",
    }))
    (skill / "SKILL.md").write_text(
        "---\nname: created\ndescription: Created for deletion\n---\nUse it.\n"
    )

    from turn.capabilities.catalog import CapabilityCatalog
    CapabilityCatalog(catalog_root).import_directory(source)
    monkeypatch.setenv("TURN_DATA_DIR", str(data_dir))
    args = parser().parse_args(["capabilities", "delete", "created"])

    assert await async_main(args) == 0
    assert not (catalog_root / "created").exists()


@pytest.mark.asyncio
async def test_agent_and_capability_cli_actions_are_logged(tmp_path, monkeypatch):
    from turn.__main__ import agent_command, async_main

    data_dir = tmp_path / "turn-data"
    project_id = uuid.uuid4()
    project_root = tmp_path / "project"
    (project_root / ".turn").mkdir(parents=True)
    (project_root / ".turn" / "state.json").write_text(
        json.dumps({"project_id": str(project_id), "nodes": []})
    )
    monkeypatch.setenv("TURN_DATA_DIR", str(data_dir))
    monkeypatch.setenv("TURN_PROJECT_ID", str(project_id))
    monkeypatch.setenv("TURN_REPO", str(project_root))

    await async_main(parser().parse_args(["capabilities", "search", "secret"]))
    await async_main(parser().parse_args(["capabilities", "load", "secret-word"]))

    handoff = tmp_path / "node.result.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", "node-1")
    agent_command(parser().parse_args([
        "agent", "status", "--state", "working", "--message", "searching capabilities"
    ]))
    agent_command(parser().parse_args([
        "agent", "submit", "--kind", "result",
        "--payload", '{"outcome":"COMPLETE","summary":"done","artifacts":[]}',
    ]))

    reader = EventLog(data_dir)
    reader.bind_project(project_id, project_root)
    records = reader.read(project_id)
    actions = [record["action"] for record in records if record.get("kind") == "agent.action"]
    assert actions == [
        "capabilities.search",
        "capabilities.search",
        "capabilities.load",
        "capabilities.load",
        "agent.status",
        "agent.status",
        "agent.submit",
        "agent.submit",
    ]
    assert records[1]["data"]["response_status"] == "accepted"
    assert records[1]["data"]["query"] == "secret"
    assert records[3]["data"]["operation"] == "load"
    assert records[-1]["data"]["response_status"] == "accepted"
    assert records[-1]["data"]["argv"] == [
        "turn", "agent", "submit", "--kind", "result", "--payload"
    ]
