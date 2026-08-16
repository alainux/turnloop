"""Translate procured MCP access into harness-native runtime configuration.

Turn owns the assignment and the source reference. The user owns credentials,
installed binaries, and any OAuth setup. A ``configured`` access therefore
only advertises a server name; explicit stdio/HTTP entries are rendered into
the selected harness's supported configuration surface.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from turn.domain.schemas import AgentConfig, HarnessKind, MCPServerAccess, MCPTransport


@dataclass(frozen=True)
class MCPRuntime:
    """Provider-neutral launch additions prepared for one node."""

    environment: dict[str, str]
    codex_overrides: tuple[str, ...] = ()
    claude_config: str | None = None


def _active_servers(agent: AgentConfig) -> list[MCPServerAccess]:
    return [
        server
        for server in agent.mcp_servers
        if server.enabled and server.transport is not MCPTransport.CONFIGURED
    ]


def _server_name(name: str) -> str:
    """Return a stable config key without changing the displayed name."""
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_.-")
    return value or "mcp-server"


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return str(path)


def _claude_entry(server: MCPServerAccess) -> dict[str, Any]:
    if server.transport is MCPTransport.STDIO:
        return {
            "type": "stdio",
            "command": server.command,
            "args": server.args,
            "env": server.env,
        }
    headers = dict(server.headers)
    if server.bearer_token_env_var and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer ${{{server.bearer_token_env_var}}}"
    return {
        "type": "ws" if server.transport is MCPTransport.WS else (
            "sse" if server.transport is MCPTransport.SSE else "http"
        ),
        "url": server.url,
        "headers": headers,
    }


def _opencode_entry(server: MCPServerAccess) -> dict[str, Any]:
    if server.transport is MCPTransport.STDIO:
        return {
            "type": "local",
            "command": [server.command, *server.args],
            "environment": server.env,
        }
    headers = {
        key: re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"{env:\1}", value)
        for key, value in server.headers.items()
    }
    if server.bearer_token_env_var and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {{env:{server.bearer_token_env_var}}}"
    return {
        "type": "remote",
        "url": server.url,
        "headers": headers,
    }


def _pi_entry(server: MCPServerAccess) -> dict[str, Any]:
    # Pi MCP extensions intentionally follow the Claude-compatible mcpServers
    # shape. Pi itself is not responsible for discovering this file; the
    # configured extension is.
    return _claude_entry(server)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: list[str]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_table(values: dict[str, str]) -> str:
    return "{ " + ", ".join(
        f"{_toml_string(key)} = {_toml_string(value)}"
        for key, value in values.items()
    ) + " }"


def codex_mcp_overrides(agent: AgentConfig) -> tuple[str, ...]:
    """Build ``codex -c`` overrides for assigned servers."""
    overrides: list[str] = []
    for server in agent.mcp_servers:
        if server.transport is MCPTransport.CONFIGURED:
            overrides.append(
                f"mcp_servers.{_server_name(server.name)}.enabled={str(server.enabled).lower()}"
            )
            continue
        if not server.enabled:
            continue
        key = _server_name(server.name)
        prefix = f"mcp_servers.{key}"
        if server.transport is MCPTransport.STDIO:
            overrides.extend([
                f"{prefix}.command={_toml_string(server.command or '')}",
                f"{prefix}.args={_toml_array(server.args)}",
            ])
            for env_name, env_value in server.env.items():
                overrides.append(f"{prefix}.env.{env_name}={_toml_string(env_value)}")
        else:
            headers = dict(server.headers)
            if server.bearer_token_env_var:
                headers.setdefault("Authorization", f"Bearer ${{{server.bearer_token_env_var}}}")
            overrides.extend([
                f"{prefix}.url={_toml_string(server.url or '')}",
                f"{prefix}.http_headers={_toml_table(headers)}",
            ])
            if server.bearer_token_env_var:
                overrides.append(
                    f"{prefix}.bearer_token_env_var={_toml_string(server.bearer_token_env_var)}"
                )
        overrides.append(f"{prefix}.enabled=true")
    return tuple(overrides)


def prepare_runtime(
    cwd: str | Path,
    node_id: object,
    agent: AgentConfig | None,
) -> MCPRuntime:
    """Prepare only the selected harness's MCP delivery surface.

    The generated files are under ``.turn/interactive`` except Pi's
    extension-discovered project file. Existing Pi configuration is merged so
    user-managed servers are never discarded.
    """
    if agent is None or not agent.mcp_servers:
        return MCPRuntime(environment={})

    root = Path(cwd)
    servers = _active_servers(agent)
    names = [server.name for server in agent.mcp_servers]
    environment = {
        "TURN_AGENT_MCP_SERVERS": ",".join(names),
        "TURN_AGENT_MCP_SOURCES": json.dumps(
            {
                server.name: server.source_url
                for server in agent.mcp_servers
                if server.source_url
            },
            ensure_ascii=False,
        ),
    }
    configured = [
        server for server in agent.mcp_servers
        if server.enabled and server.transport is MCPTransport.CONFIGURED
    ]
    if not servers and not configured:
        return MCPRuntime(environment=environment)

    harness = agent.harness
    if harness is HarnessKind.CODEX:
        return MCPRuntime(environment=environment, codex_overrides=codex_mcp_overrides(agent))

    if not servers and harness in {HarnessKind.CLAUDE, HarnessKind.PI}:
        return MCPRuntime(environment=environment)

    if harness is HarnessKind.CLAUDE:
        path = root / ".turn" / "interactive" / f"{node_id}.mcp.json"
        config_path = _write_json(
            path,
            {"mcpServers": {_server_name(server.name): _claude_entry(server) for server in servers}},
        )
        environment["TURN_AGENT_MCP_CONFIG"] = config_path
        return MCPRuntime(
            environment=environment,
            claude_config=config_path,
        )

    if harness is HarnessKind.OPENCODE:
        payload = {
            "mcp": {
                "servers": {
                    _server_name(server.name): _opencode_entry(server)
                    for server in servers
                }
            },
            "tools": {
                f"{_server_name(server.name)}_*": True
                for server in configured
            },
        }
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(payload, ensure_ascii=False)
        return MCPRuntime(environment=environment)

    if harness is HarnessKind.PI:
        path = root / ".pi" / "mcp.json"
        existing: dict[str, Any] = {}
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                existing = loaded
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        configured = existing.get("mcpServers")
        if not isinstance(configured, dict):
            configured = {}
        configured.update({
            _server_name(server.name): _pi_entry(server)
            for server in servers
        })
        _write_json(path, {**existing, "mcpServers": configured})
        environment["TURN_AGENT_MCP_CONFIG"] = str(path)
        return MCPRuntime(environment=environment)

    return MCPRuntime(environment=environment)
