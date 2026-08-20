"""Harness adapters for installing and launching portable capabilities."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from turn.capabilities.plugin import CapabilityPlugin, MCPComponent
from turn.domain.capability_contracts import validate_capability_id
from turn.domain.schemas import HarnessKind


def _config_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-") or "capability-mcp"


def _expand(value: str, package: CapabilityPlugin, data_root: Path) -> str:
    return (
        value
        .replace("${PLUGIN_ROOT}", str(package.path))
        .replace("${PLUGIN_DATA}", str(data_root / package.id))
    )


def _expanded_config(component: MCPComponent, package: CapabilityPlugin, data_root: Path) -> dict[str, Any]:
    value = json.loads(json.dumps(component.config))
    if isinstance(value.get("command"), str) and value["command"].startswith("./"):
        value["command"] = str((package.path / value["command"][2:]).resolve())
    for key in ("args",):
        value[key] = [_expand(item, package, data_root) for item in value.get(key, [])]
    for key in ("env",):
        value[key] = {name: _expand(item, package, data_root) for name, item in value.get(key, {}).items()}
    if isinstance(value.get("cwd"), str):
        value["cwd"] = _expand(value["cwd"], package, data_root)
    return value


@dataclass(frozen=True)
class CapabilityLaunch:
    skill_names: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    codex_overrides: tuple[str, ...] = ()
    claude_config: str | None = None
    opencode_config: str | None = None
    pi_mcp_config: str | None = None


@dataclass(frozen=True)
class CapabilityVerification:
    capability_id: str
    harness: str
    installed: bool
    skill_count: int
    mcp_count: int


class CapabilityHarnessAdapter:
    """Install, remove, prepare, and verify one harness's native surface."""

    def __init__(self, harness: HarnessKind):
        self.harness = harness

    @property
    def skill_root_name(self) -> str | None:
        return {
            HarnessKind.CODEX: ".agents/skills",
            HarnessKind.CLAUDE: ".claude/skills",
            HarnessKind.OPENCODE: ".opencode/skills",
        }.get(self.harness)

    @property
    def supports_cli_skill_paths(self) -> bool:
        return self.harness is HarnessKind.PI

    def _marker(self, project_root: Path, capability_id: str) -> Path:
        validate_capability_id(capability_id)
        return project_root / ".turn" / "capability-installations" / self.harness.value / f"{capability_id}.json"

    def install(self, package: CapabilityPlugin, project_root: str | Path) -> bool:
        """Deploy a loaded package exactly once for this project/harness."""
        root = Path(project_root).expanduser().resolve()
        marker = self._marker(root, package.id)
        if marker.is_file():
            try:
                recorded = json.loads(marker.read_text(encoding="utf-8"))
                if recorded.get("capability") == package.id and self.verify(package, root).installed:
                    return False
            except (OSError, json.JSONDecodeError):
                pass
        deployed: list[str] = []
        if self.skill_root_name:
            skill_root = root / self.skill_root_name
            for skill in package.skills:
                destination = skill_root / skill.name
                if destination.exists() and destination.resolve() != skill.path.parent.resolve():
                    raise ValueError(
                        f"cannot install capability {package.id}: native skill name is already occupied: {skill.name}"
                    )
                if not destination.exists():
                    shutil.copytree(skill.path.parent, destination)
                    deployed.append(str(destination))
        previous_pi_mcp: str | None = None
        if self.harness is HarnessKind.PI and package.mcp_servers:
            previous_pi_mcp = self._install_pi_mcp(package, root)
            deployed.append(str(root / ".pi" / "mcp.json"))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "capability": package.id,
            "deployed": deployed,
            "previous_pi_mcp": previous_pi_mcp,
        }, indent=2) + "\n", encoding="utf-8")
        return True

    def uninstall(self, capability_id: str, project_root: str | Path) -> bool:
        root = Path(project_root).expanduser().resolve()
        marker = self._marker(root, capability_id)
        if not marker.is_file():
            return False
        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid capability installation marker: {marker}") from error
        for raw in recorded.get("deployed", []):
            path = Path(raw).resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise ValueError(f"capability installation escapes project: {path}") from error
            if path.name == "mcp.json" and path.parent.name == ".pi":
                previous = recorded.get("previous_pi_mcp")
                if isinstance(previous, str):
                    path.write_text(previous, encoding="utf-8")
                elif path.is_file():
                    path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        marker.unlink()
        return True

    def load(self, package: CapabilityPlugin, project_root: str | Path) -> bool:
        return self.install(package, project_root)

    def unload(self, capability_id: str, project_root: str | Path) -> bool:
        return self.uninstall(capability_id, project_root)

    def prepare_launch(
        self,
        packages: list[CapabilityPlugin],
        project_root: str | Path,
        node_id: object,
    ) -> CapabilityLaunch:
        root = Path(project_root).expanduser().resolve()
        data_root = root / ".turn" / "capability-data"
        skills = tuple(str(skill.path) for package in packages for skill in package.skills)
        skill_names = tuple(skill.name for package in packages for skill in package.skills)
        configs = [
            (package, component.name, _expanded_config(component, package, data_root))
            for package in packages
            for component in package.mcp_servers
        ]
        names = [_config_name(name) for _, name, _ in configs]
        if len(names) != len(set(names)):
            raise ValueError("capability MCP server names must be unique for one launch")
        if self.harness is HarnessKind.CODEX:
            return CapabilityLaunch(
                skill_names=skill_names,
                codex_overrides=tuple(self._codex_overrides(configs)),
            )
        if self.harness is HarnessKind.CLAUDE:
            path = root / ".turn" / "interactive" / f"{node_id}.capabilities.mcp.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"mcpServers": {
                name: self._claude_server(config) for _, name, config in configs
            }}, indent=2) + "\n", encoding="utf-8")
            return (
                CapabilityLaunch(skill_names=skill_names, claude_config=str(path))
                if configs
                else CapabilityLaunch(skill_names=skill_names)
            )
        if self.harness is HarnessKind.OPENCODE:
            payload = {"mcp": {
                name: self._opencode_server(config) for _, name, config in configs
            }}
            return (
                CapabilityLaunch(skill_names=skill_names, opencode_config=json.dumps(payload))
                if configs
                else CapabilityLaunch(skill_names=skill_names)
            )
        if self.harness is HarnessKind.PI:
            path = root / ".pi" / "mcp.json"
            return CapabilityLaunch(
                skill_names=skill_names,
                skill_paths=skills,
                pi_mcp_config=str(path) if configs else None,
            )
        return CapabilityLaunch(skill_names=skill_names, skill_paths=skills)

    def verify(self, package: CapabilityPlugin, project_root: str | Path) -> CapabilityVerification:
        root = Path(project_root).expanduser().resolve()
        installed = self._marker(root, package.id).is_file()
        if self.skill_root_name:
            installed = installed and all((root / self.skill_root_name / skill.name).is_dir() for skill in package.skills)
        if self.harness is HarnessKind.PI and package.mcp_servers:
            try:
                payload = json.loads((root / ".pi" / "mcp.json").read_text(encoding="utf-8"))
                servers = payload.get("mcpServers", {}) if isinstance(payload, dict) else {}
                installed = installed and all(
                    _config_name(component.name) in servers for component in package.mcp_servers
                )
            except (OSError, json.JSONDecodeError):
                installed = False
        return CapabilityVerification(package.id, self.harness.value, installed, package.skill_count, package.mcp_count)

    def _install_pi_mcp(self, package: CapabilityPlugin, root: Path) -> str | None:
        path = root / ".pi" / "mcp.json"
        previous: str | None
        existing: dict[str, Any] = {}
        try:
            previous = path.read_text(encoding="utf-8")
            loaded = json.loads(previous)
            if isinstance(loaded, dict):
                existing = loaded
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            previous = None
            pass
        servers = existing.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        data_root = root / ".turn" / "capability-data"
        additions = {
            _config_name(component.name): self._pi_server(_expanded_config(component, package, data_root))
            for component in package.mcp_servers
        }
        conflicts = [
            name for name, config in additions.items()
            if name in servers and servers[name] != config
        ]
        if conflicts:
            raise ValueError(
                f"capability MCP server names are already configured in Pi: {', '.join(sorted(conflicts))}"
            )
        servers.update(additions)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**existing, "mcpServers": servers}, indent=2) + "\n", encoding="utf-8")
        return previous

    @staticmethod
    def _codex_overrides(configs: list[tuple[CapabilityPlugin, str, dict[str, Any]]]) -> list[str]:
        values: list[str] = []
        for _, name, config in configs:
            key = _config_name(name)
            prefix = f"mcp_servers.{key}"
            if config["type"] == "stdio":
                values.extend([
                    f'{prefix}.command={json.dumps(config["command"])}',
                    f'{prefix}.args={json.dumps(config.get("args", []))}',
                ])
                for env_name, env_value in config.get("env", {}).items():
                    values.append(f'{prefix}.env.{env_name}={json.dumps(env_value)}')
                if config.get("cwd"):
                    values.append(f'{prefix}.cwd={json.dumps(config["cwd"])}')
            else:
                values.extend([
                    f'{prefix}.url={json.dumps(config["url"])}',
                    f'{prefix}.http_headers={json.dumps(config.get("headers", {}))}',
                ])
            values.append(f"{prefix}.enabled=true")
        return values

    @staticmethod
    def _claude_server(config: dict[str, Any]) -> dict[str, Any]:
        if config["type"] == "stdio":
            return {key: config[key] for key in ("type", "command", "args", "env", "cwd") if key in config}
        return {"type": "sse" if config["type"] == "sse" else "http", "url": config["url"], **({"headers": config["headers"]} if config.get("headers") else {})}

    @staticmethod
    def _opencode_server(config: dict[str, Any]) -> dict[str, Any]:
        if config["type"] == "stdio":
            return {"type": "local", "command": [config["command"], *config.get("args", [])], "environment": config.get("env", {})}
        return {"type": "remote", "url": config["url"], **({"headers": config["headers"]} if config.get("headers") else {})}

    @staticmethod
    def _pi_server(config: dict[str, Any]) -> dict[str, Any]:
        if config["type"] == "stdio":
            return {"transport": "stdio", "command": config["command"], "args": config.get("args", []), **({"env": config["env"]} if config.get("env") else {})}
        return {"transport": "sse" if config["type"] == "sse" else "streamable-http", "url": config["url"], **({"headers": config["headers"]} if config.get("headers") else {})}


def harness_capability_adapter(harness: HarnessKind) -> CapabilityHarnessAdapter:
    return CapabilityHarnessAdapter(harness)


def capability_is_installed(capability_id: str, harness: HarnessKind, project_root: str | Path) -> bool:
    validate_capability_id(capability_id)
    marker = Path(project_root).expanduser().resolve() / ".turn" / "capability-installations" / harness.value / f"{capability_id}.json"
    return marker.is_file()
