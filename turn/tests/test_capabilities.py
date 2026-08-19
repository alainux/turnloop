from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from turn.capabilities.catalog import CapabilityCatalog
from turn.capabilities.plugin import CapabilityPluginError, load_capability_plugin
from turn.db.store import Store
from turn.domain.capability_contracts import ROLE_CAPABILITY_IDS, SETUP_CAPABILITY_ID
from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, Node, NodeSpec, PlanResult
from turn.workers.capabilities import harness_capability_adapter
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.base import NodeExecutionContext, render_context_block
from turn.workers.planner import AgentPlanner, CodexPlanner


def test_catalog_discovers_skill_only_mcp_only_and_mixed_plugins():
    catalog = CapabilityCatalog(Path.cwd() / "turn" / "capabilities")
    entries = {entry.id: entry for entry in catalog.list()}

    assert entries["turn-executing"].skill_count == 1
    assert entries["turn-executing"].mcp_count == 0
    assert entries["turn-basics"].skill_count == 1
    assert entries["turn-basics"].mcp_count == 0
    assert entries["secret-word"].skill_count == 1
    assert entries["secret-word"].mcp_count == 1
    assert catalog.search("secret")[0].id == "secret-word"


def test_plugin_loader_keeps_unknown_manifest_metadata_nonfatal(tmp_path):
    source = tmp_path / "plugin"
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    (source / "plugin.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "demo",
        "description": "A demo",
        "futureField": {"ignored": True},
    }))
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: A demo skill\n---\nUse the demo.\n"
    )

    package = load_capability_plugin(source)
    assert package.id == "demo"
    assert package.skill_count == 1


def test_plugin_loader_skips_a_skill_that_escapes_the_package(tmp_path):
    source = tmp_path / "plugin"
    skill = source / "skills" / "demo"
    skill.mkdir(parents=True)
    (source / "plugin.json").write_text(json.dumps({
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "demo",
    }))
    (skill / "SKILL.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("---\nname: demo\ndescription: outside\n---\n")

    with pytest.raises(CapabilityPluginError, match="no supported"):
        load_capability_plugin(source)


def test_catalog_delete_removes_only_a_user_authored_package(tmp_path):
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

    catalog = CapabilityCatalog(tmp_path / "catalog")
    catalog.import_directory(source)
    deleted = catalog.delete("created")

    assert deleted == (tmp_path / "catalog" / "created").resolve()
    assert not deleted.exists()
    with pytest.raises(CapabilityPluginError, match="not found"):
        catalog.get("created")


def test_catalog_delete_refuses_builtins():
    catalog = CapabilityCatalog(Path.cwd() / "turn" / "capabilities")

    with pytest.raises(CapabilityPluginError, match="cannot delete packaged"):
        catalog.delete("turn-basics")


@pytest.mark.asyncio
async def test_project_load_is_separate_from_harness_install(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Set up a small workflow")
    project = store.project_path(root.id)
    assert project is not None

    assert SETUP_CAPABILITY_ID in root.agent.capabilities
    assert "turn-basics" in root.agent.capabilities
    assert (project / ".turn" / "capabilities" / "turn-basics" / "plugin.json").is_file()
    assert (project / ".turn" / "capabilities" / SETUP_CAPABILITY_ID / "plugin.json").is_file()
    assert (project / ".turn" / "capabilities" / "turn-authoring-capabilities" / "plugin.json").is_file()
    assert not (project / ".agents" / "skills").exists()

    CapabilityCatalog(store.data_dir / "capabilities").load_into_project(
        "secret-word", project
    )
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(
            key="architect",
            objective="Plan application architecture",
            executor="planner",
            plan=True,
            capabilities=["secret-word"],
        )]),
    )
    architect = created[0]
    assert architect.agent is not None
    assert architect.agent.type_id is AgentType.PLANNER
    assert "secret-word" in architect.agent.capabilities
    assert (project / ".turn" / "capabilities" / "secret-word" / "plugin.json").is_file()


def test_role_contracts_are_capability_ids():
    assert AgentConfig(type_id=AgentType.PLANNER).capabilities == list(ROLE_CAPABILITY_IDS["planner"])
    assert AgentConfig(type_id=AgentType.EXECUTOR).capabilities == list(ROLE_CAPABILITY_IDS["executor"])
    assert AgentConfig(type_id=AgentType.INTEGRATOR).capabilities == list(ROLE_CAPABILITY_IDS["integrator"])
    assert AgentConfig(type_id=AgentType.VERIFIER).capabilities == list(ROLE_CAPABILITY_IDS["verifier"])

    verifier = AgentConfig(
        type_id=AgentType.EXECUTOR,
        capabilities=["project-specific"],
        session_id="session-1",
    ).as_type(AgentType.VERIFIER)
    assert verifier.capabilities == ["turn-basics", "turn-verifying", "project-specific"]
    assert verifier.session_id == "session-1"


def test_setup_role_capability_does_not_cascade_to_descendants():
    root = AgentConfig(type_id=AgentType.PLANNER)
    root.capabilities.append("turn-setup")

    executor = root.as_type(AgentType.EXECUTOR)

    assert executor.capabilities == list(ROLE_CAPABILITY_IDS["executor"])


@pytest.mark.parametrize(
    ("harness", "marker"),
    [
        (HarnessKind.CODEX, "$secret-word"),
        (HarnessKind.CLAUDE, "/secret-word"),
        (HarnessKind.OPENCODE, "/secret-word"),
        (HarnessKind.PI, "/skill:secret-word"),
    ],
)
def test_launch_prompt_explicitly_invokes_native_skill_for_each_harness(tmp_path, harness, marker):
    project = tmp_path / "project"
    project.mkdir()
    CapabilityCatalog(tmp_path / "catalog").load_into_project("secret-word", project)
    context = NodeExecutionContext(
        node=Node(
            project_id=uuid.uuid4(),
            objective="Use the secret word",
            agent=AgentConfig(
                type_id=AgentType.EXECUTOR,
                harness=harness,
                capabilities=["secret-word"],
            ),
        ),
        repo_path=str(project),
    )
    assert marker in render_context_block(context)


def test_initial_prompt_is_only_node_data_and_activations():
    project_id = uuid.uuid4()
    context = NodeExecutionContext(
        node=Node(
            project_id=project_id,
            objective="Plan a native capability demo",
            agent=AgentConfig(type_id=AgentType.PLANNER, harness=HarnessKind.CODEX),
        )
    )

    prompt = CodexPlanner()._build_prompt(context)

    assert f"project_id={project_id}" in prompt
    assert "node_id=" in prompt
    assert "objective=Plan a native capability demo" in prompt
    assert "turn project info" not in prompt
    assert "GRAPH EXPLORATION TOOL" not in prompt
    assert "REQUIRED DISCOVERY GATE" not in prompt
    assert prompt.count("TURN_CONTEXT") == 1
    assert "production_trigger_policy" not in prompt
    assert len(prompt) < 600


def test_basics_and_planning_guidance_live_in_capabilities():
    root = Path.cwd() / "turn" / "capabilities" / "builtin"
    basics = (root / "turn-basics" / "skills" / "turn-basics" / "SKILL.md").read_text()
    setup = (root / "turn-setup" / "skills" / "turn-setup" / "SKILL.md").read_text()
    planning = (root / "turn-planning" / "skills" / "turn-planning" / "SKILL.md").read_text()
    executing = (root / "turn-executing" / "skills" / "turn-executing" / "SKILL.md").read_text()
    integrating = (root / "turn-integrating" / "skills" / "turn-integrating" / "SKILL.md").read_text()
    verifying = (root / "turn-verifying" / "skills" / "turn-verifying" / "SKILL.md").read_text()

    assert "turn project info" in basics
    assert "turn graph <project-id>" in basics
    assert "turn agent submit --kind result" in basics
    assert "The CLI is the only control-plane" in basics
    assert "Project files are ordinary workspace files" in basics
    assert "adaptive workflow planner" in basics
    assert "super-planner" in setup
    assert "Scope classification gate" in setup
    assert "department-shaped" in setup
    assert "native" in planning
    assert "turn capabilities search" in planning
    assert "generated_prompt" in planning
    assert "Harness/model selection" in planning
    assert "project's current harness/model catalog" in planning
    assert "provider-specific spelling" in planning
    assert "Submit the result and its small artifact list through the Turn CLI" in executing
    assert "Small work" in planning and "Medium work" in planning and "Large work" in planning
    assert "self-trigger loop" in basics
    assert "self-trigger loop" in planning
    assert "self-trigger loop" in executing
    assert "exported contract" in executing
    assert "convergence gate" in integrating
    assert "full usability spectrum" in verifying


def test_plan_parser_uses_only_capability_plugin_ids():
    plan = AgentPlanner._parse_plan(json.dumps({
        "nodes": [{
            "key": "build",
            "objective": "Build the product",
            "agent_type": "executor",
            "capabilities": ["turn-executing"],
        }],
    }))
    assert plan.nodes[0].capabilities == ["turn-executing"]
    with pytest.raises(ValueError):
        AgentPlanner._parse_plan(json.dumps({
            "nodes": [{"key": "build", "objective": "Build", "skills": ["old"]}],
        }))


def test_plan_validation_requires_project_loaded_capabilities(tmp_path):
    catalog = CapabilityCatalog(tmp_path / "catalog")
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(CapabilityPluginError, match="not loaded"):
        catalog.validate_plan(
            {"nodes": [{"key": "work", "capabilities": ["secret-word"]}]},
            project,
            planner_capabilities=[],
        )
    catalog.load_into_project("secret-word", project)
    catalog.load_into_project("turn-executing", project)
    catalog.load_into_project("turn-basics", project)
    catalog.validate_plan(
        {"nodes": [{"key": "work", "capabilities": ["secret-word"]}]},
        project,
        planner_capabilities=[],
    )


@pytest.mark.parametrize("harness", [HarnessKind.CODEX, HarnessKind.CLAUDE, HarnessKind.OPENCODE, HarnessKind.PI])
def test_secret_word_capability_prepares_native_launch_for_every_harness(tmp_path, harness):
    catalog = CapabilityCatalog(tmp_path / "catalog")
    project = tmp_path / harness.value
    project.mkdir()
    catalog.load_into_project("secret-word", project)
    package = catalog.resolve_project("secret-word", project)
    adapter = harness_capability_adapter(harness)

    assert adapter.install(package, project) is True
    assert adapter.verify(package, project).installed
    launch = adapter.prepare_launch([package], project, "node")
    assert launch.skill_paths or launch.codex_overrides or launch.claude_config or launch.opencode_config or launch.pi_mcp_config
    assert adapter.install(package, project) is False

    if harness is HarnessKind.CODEX:
        assert any("secret-word-echo" in value for value in launch.codex_overrides)
    elif harness is HarnessKind.CLAUDE:
        assert json.loads(Path(launch.claude_config or "").read_text())["mcpServers"]["secret-word-echo"]["command"] == "python3"
    elif harness is HarnessKind.OPENCODE:
        assert "secret-word-echo" in json.loads(launch.opencode_config or "")["mcp"]
    else:
        assert json.loads(Path(launch.pi_mcp_config or "").read_text())["mcpServers"]["secret-word-echo"]["command"] == "python3"

    assert adapter.uninstall("secret-word", project) is True
    assert not adapter.verify(package, project).installed
    assert adapter.uninstall("secret-word", project) is False


def test_native_commands_use_project_scoped_capability_paths():
    factory = HarnessCommandFactory()
    command = factory.worker_command(
        HarnessKind.PI,
        AgentConfig(harness=HarnessKind.PI),
        "prompt",
        "/tmp/project",
        skill_paths=["/tmp/project/.turn/capabilities/secret-word/skills/secret-word"],
    )
    assert command[command.index("--skill") + 1].startswith("/tmp/project/.turn/")


def test_secret_word_fixture_exposes_distinct_skill_and_mcp_words():
    package = load_capability_plugin(Path.cwd() / "turn" / "capabilities" / "builtin" / "secret-word")
    skill_text = package.skills[0].path.read_text(encoding="utf-8")
    assert "cobalt" in skill_text
    assert "amber" not in skill_text

    process = subprocess.Popen(
        [sys.executable, str(package.path / "server.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, separators=(",", ":"))
        process.stdin.write((request + "\n").encode())
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["result"]["content"][0]["text"] == "amber"
    finally:
        process.kill()
        process.wait()
