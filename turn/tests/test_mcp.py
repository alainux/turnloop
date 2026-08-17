from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from turn.db.store import Store
from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, MCPTransport, Node, NodeSpec, PlanResult
from turn.mcp.runtime import codex_mcp_overrides, prepare_runtime
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.base import NodeExecutionContext, render_context_block
from turn.workers.planner import AgentPlanner


def explicit_server(transport: str = "http") -> dict:
    return {
        "name": "context7",
        "source_url": "https://github.com/upstash/context7",
        "transport": transport,
        "url": "https://mcp.context7.com/mcp",
        "bearer_token_env_var": "CONTEXT7_API_KEY",
    }


def test_mcp_assignments_are_typed_and_legacy_names_still_load():
    configured = AgentConfig(mcp_servers=["context7"])
    assert configured.mcp_servers[0].name == "context7"
    assert configured.mcp_servers[0].transport is MCPTransport.CONFIGURED

    plan = PlanResult(nodes=[
        NodeSpec(
            key="research",
            objective="Research docs",
            agent_type=AgentType.EXECUTOR,
            mcp_servers=[explicit_server()],
        )
    ])
    assert plan.nodes[0].mcp_servers[0].source_url == "https://github.com/upstash/context7"


def test_mcp_runtime_shapes_are_native_per_harness(tmp_path):
    node_id = uuid.uuid4()
    for harness in (HarnessKind.CODEX, HarnessKind.CLAUDE, HarnessKind.OPENCODE, HarnessKind.PI):
        agent = AgentConfig(harness=harness, mcp_servers=[explicit_server()])
        runtime = prepare_runtime(tmp_path / harness.value, node_id, agent)
        assert runtime.environment["TURN_AGENT_MCP_SERVERS"] == "context7"
        assert "https://github.com/upstash/context7" in runtime.environment["TURN_AGENT_MCP_SOURCES"]

        if harness is HarnessKind.CODEX:
            assert any("mcp_servers.context7.url" in item for item in runtime.codex_overrides)
            assert any("${CONTEXT7_API_KEY}" in item for item in runtime.codex_overrides)
        elif harness is HarnessKind.CLAUDE:
            assert runtime.claude_config is not None
            assert runtime.environment["TURN_AGENT_MCP_CONFIG"] == runtime.claude_config
            payload = json.loads((tmp_path / harness.value / ".turn" / "interactive" / f"{node_id}.mcp.json").read_text())
            assert payload["mcpServers"]["context7"]["type"] == "http"
        elif harness is HarnessKind.OPENCODE:
            payload = json.loads(runtime.environment["OPENCODE_CONFIG_CONTENT"])
            assert payload["mcp"]["servers"]["context7"]["type"] == "remote"
        else:
            payload = json.loads((tmp_path / harness.value / ".pi" / "mcp.json").read_text())
            assert payload["mcpServers"]["context7"]["url"] == "https://mcp.context7.com/mcp"


def test_stdio_mcp_is_rendered_for_all_local_server_adapters(tmp_path):
    definition = {
        "name": "filesystem",
        "source_url": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
    }
    claude = prepare_runtime(
        tmp_path / "claude", uuid.uuid4(), AgentConfig(harness=HarnessKind.CLAUDE, mcp_servers=[definition])
    )
    assert claude.claude_config is not None
    assert json.loads(Path(claude.claude_config).read_text())["mcpServers"]["filesystem"]["command"] == "npx"

    opencode = prepare_runtime(
        tmp_path / "opencode", uuid.uuid4(), AgentConfig(harness=HarnessKind.OPENCODE, mcp_servers=[definition])
    )
    assert json.loads(opencode.environment["OPENCODE_CONFIG_CONTENT"])["mcp"]["servers"]["filesystem"]["type"] == "local"

    codex = codex_mcp_overrides(AgentConfig(harness=HarnessKind.CODEX, mcp_servers=[definition]))
    assert any('mcp_servers.filesystem.command="npx"' == item for item in codex)


def test_pi_runtime_merges_user_servers(tmp_path):
    path = tmp_path / ".pi" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"user-server": {"type": "stdio", "command": "user"}}}))

    agent = AgentConfig(harness=HarnessKind.PI, mcp_servers=[explicit_server("http")])
    prepare_runtime(tmp_path, uuid.uuid4(), agent)
    payload = json.loads(path.read_text())
    assert set(payload["mcpServers"]) == {"user-server", "context7"}


def test_configured_mcp_is_metadata_only_and_keeps_user_owned_setup():
    agent = AgentConfig(harness=HarnessKind.CLAUDE, mcp_servers=[{"name": "github", "source_url": "https://github.com/modelcontextprotocol/servers"}])
    runtime = prepare_runtime("/tmp/turn-mcp-test", uuid.uuid4(), agent)
    assert runtime.claude_config is None
    assert runtime.environment["TURN_AGENT_MCP_SERVERS"] == "github"

    codex = AgentConfig(harness=HarnessKind.CODEX, mcp_servers=[{"name": "github"}])
    assert codex_mcp_overrides(codex) == ("mcp_servers.github.enabled=true",)

    opencode = AgentConfig(harness=HarnessKind.OPENCODE, mcp_servers=[{"name": "github"}])
    opencode_runtime = prepare_runtime("/tmp/turn-mcp-test-opencode", uuid.uuid4(), opencode)
    payload = json.loads(opencode_runtime.environment["OPENCODE_CONFIG_CONTENT"])
    assert payload["tools"]["github_*"] is True


def test_claude_adapter_receives_per_run_mcp_config():
    command = HarnessCommandFactory().worker_command(
        HarnessKind.CLAUDE,
        AgentConfig(harness=HarnessKind.CLAUDE, session_id="session"),
        "prompt",
        "/tmp/project",
        mcp_config="/tmp/project/.turn/mcp.json",
    )
    assert command[command.index("--mcp-config") + 1] == "/tmp/project/.turn/mcp.json"


def test_harness_adapters_do_not_inject_permission_policy():
    factory = HarnessCommandFactory()
    policy_flags = {
        "--approve-for-me",
        "--auto",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-skip-permissions",
        "--permission-mode",
        "--approve",
        "-s",
    }
    for harness in (HarnessKind.CODEX, HarnessKind.CLAUDE, HarnessKind.OPENCODE, HarnessKind.PI):
        agent = AgentConfig(harness=harness, session_id="session")
        commands = [factory.reconnect_command(agent, "/tmp/project", "session")]
        if harness != HarnessKind.CODEX:
            commands.extend([
                factory.worker_command(harness, agent, "prompt", "/tmp/project"),
                factory.planner_command(agent, "prompt", cwd="/tmp/project", native=False, resume=False),
            ])
        assert all(policy_flags.isdisjoint(command or []) for command in commands)


def test_codex_adapter_keeps_mcp_config_out_of_the_prompt():
    agent = AgentConfig(harness=HarnessKind.CODEX, mcp_servers=[explicit_server()])
    overrides = codex_mcp_overrides(agent)
    assert all("prompt" not in item for item in overrides)
    assert any(item.startswith("mcp_servers.context7") for item in overrides)
    assert any('"Authorization" = ' in item for item in overrides)


def test_planner_prompt_requires_mcp_research_and_assignment():
    prompt = AgentPlanner().codex._build_prompt(
        NodeExecutionContext(node=Node(project_id=uuid.uuid4(), objective="Build a docs tool"))
    )
    assert "find-mcps" in prompt
    assert "mcp_servers" in prompt
    assert "source_url" in prompt
    assert "harness capabilities" in prompt
    assert "duplicate a" in prompt


def test_worker_context_exposes_declared_harness_capabilities():
    node = Node(
        project_id=uuid.uuid4(),
        objective="Verify a browser game",
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )
    context = render_context_block(NodeExecutionContext(node=node))
    assert "harness-provided capabilities (before MCP procurement): browser, computer-use" in context


@pytest.mark.asyncio
async def test_mcp_assignments_are_inherited_by_created_workers(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a docs tool")
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(
            key="docs",
            objective="Query documentation",
            executor="codex",
            mcp_servers=[explicit_server()],
        )]),
    )
    assert created[0].agent is not None
    assert created[0].agent.mcp_servers[0].name == "context7"
    await store.dispose()
