"""Agent Plugins v1 loader for Turn capability packages.

Turn deliberately implements only the portable skills and MCP component types.
The loader validates package boundaries before a harness is allowed to see a
component. Harness-specific installation is kept in ``turn.workers``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


AGENT_PLUGINS_VERSION = "1.0.0"
PLUGIN_SCHEMA = f"https://agent-plugins.org/schemas/{AGENT_PLUGINS_VERSION}/plugin.schema.json"
MCP_SCHEMA = f"https://agent-plugins.org/schemas/{AGENT_PLUGINS_VERSION}/mcp.schema.json"
_PLUGIN_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CapabilityPluginError(ValueError):
    """A capability package is not a valid Agent Plugin v1 package."""


@dataclass(frozen=True)
class SkillComponent:
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class MCPComponent:
    name: str
    config: dict[str, Any]


@dataclass(frozen=True)
class CapabilityPlugin:
    """Validated package metadata and component locations."""

    id: str
    version: str | None
    description: str
    path: Path
    manifest: dict[str, Any]
    skills: tuple[SkillComponent, ...]
    mcp_servers: tuple[MCPComponent, ...]

    @property
    def skill_count(self) -> int:
        return len(self.skills)

    @property
    def mcp_count(self) -> int:
        return len(self.mcp_servers)

    @property
    def has_components(self) -> bool:
        return bool(self.skills or self.mcp_servers)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapabilityPluginError(f"invalid {label}: {path}") from error
    if not isinstance(value, dict):
        raise CapabilityPluginError(f"{label} must be a JSON object: {path}")
    return value


def _inside(root: Path, candidate: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise CapabilityPluginError(f"{label} escapes plugin root: {candidate}") from error
    return resolved


def _valid_data_relative(value: str) -> bool:
    suffix = value.removeprefix("${PLUGIN_DATA}").lstrip("/")
    depth = 0
    for part in suffix.replace("\\", "/").split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _frontmatter(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise CapabilityPluginError(f"cannot read skill: {path}") from error
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise CapabilityPluginError(f"skill must start with YAML frontmatter: {path}")
    end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        raise CapabilityPluginError(f"skill frontmatter is not closed: {path}")
    values: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in {"name", "description"}:
            values[key] = value
    name = values.get("name", "")
    description = values.get("description", "")
    if not _SKILL_NAME.fullmatch(name) or len(name) > 64:
        raise CapabilityPluginError(f"skill name is invalid in {path}")
    if not description or len(description) > 1024:
        raise CapabilityPluginError(f"skill description is invalid in {path}")
    return name, description


def _validate_mcp(root: Path, path: Path, value: dict[str, Any]) -> tuple[MCPComponent, ...]:
    if value.get("$schema") != MCP_SCHEMA or set(value) - {"$schema", "mcpServers"}:
        raise CapabilityPluginError(f"mcp.json does not conform to Agent Plugins v{AGENT_PLUGINS_VERSION}: {path}")
    servers = value.get("mcpServers")
    if not isinstance(servers, dict):
        raise CapabilityPluginError(f"mcpServers must be an object: {path}")
    components: list[MCPComponent] = []
    for name, raw in servers.items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            continue
        server = dict(raw)
        transport = server.get("type")
        if transport not in {"stdio", "streamable-http", "sse"}:
            continue
        allowed = {
            "stdio": {"type", "command", "args", "env", "cwd"},
            "streamable-http": {"type", "url", "headers"},
            "sse": {"type", "url", "headers"},
        }[transport]
        if set(server) - allowed:
            continue
        if transport == "stdio":
            command = server.get("command")
            if not isinstance(command, str) or not command or " " in command.strip():
                continue
            if "/" in command and not command.startswith("./"):
                continue
            args = server.get("args", [])
            env = server.get("env", {})
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise CapabilityPluginError(f"MCP args must be strings: {name}")
            if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
                raise CapabilityPluginError(f"MCP env must be a string map: {name}")
            cwd = server.get("cwd")
            if cwd is not None and not isinstance(cwd, str):
                continue
            if isinstance(cwd, str):
                if cwd.startswith("./"):
                    _inside(root, root / cwd, f"MCP {name} cwd")
                elif cwd == "${PLUGIN_ROOT}" or cwd.startswith("${PLUGIN_ROOT}/"):
                    _inside(root, root / cwd.removeprefix("${PLUGIN_ROOT}").lstrip("/"), f"MCP {name} cwd")
                elif cwd == "${PLUGIN_DATA}" or (
                    cwd.startswith("${PLUGIN_DATA}/") and _valid_data_relative(cwd)
                ):
                    pass
                else:
                    continue
        else:
            url = server.get("url")
            parsed = urlsplit(url) if isinstance(url, str) else None
            if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.fragment:
                continue
            if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                continue
            headers = server.get("headers", {})
            if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                continue
        components.append(MCPComponent(name=name, config=server))
    return tuple(components)


def load_capability_plugin(path: str | Path) -> CapabilityPlugin:
    """Validate and load one portable capability plugin directory."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise CapabilityPluginError(f"capability plugin directory does not exist: {root}")
    manifest_path = root / "plugin.json"
    _inside(root, manifest_path, "plugin manifest")
    manifest = _read_json(manifest_path, "plugin.json")
    if manifest.get("$schema") != PLUGIN_SCHEMA:
        raise CapabilityPluginError(f"plugin.json must target Agent Plugins v{AGENT_PLUGINS_VERSION}: {manifest_path}")
    allowed_manifest = {"$schema", "name", "version", "description", "author", "homepage", "repository", "license", "keywords", "extensions"}
    # Unknown manifest fields are reported and ignored by conformant clients.
    # Turn keeps the recognized subset in the validated package and never gives
    # extension code semantics outside the Agent Plugins contract.
    manifest = {key: value for key, value in manifest.items() if key in allowed_manifest}
    plugin_id = manifest.get("name")
    if (
        not isinstance(plugin_id, str)
        or not _PLUGIN_NAME.fullmatch(plugin_id)
        or "--" in plugin_id
        or ".." in plugin_id
        or len(plugin_id) > 64
    ):
        raise CapabilityPluginError(f"plugin name is invalid: {plugin_id!r}")
    description = manifest.get("description", "")
    if not isinstance(description, str):
        raise CapabilityPluginError("plugin description must be a string")
    if "version" in manifest and not isinstance(manifest["version"], str):
        raise CapabilityPluginError("plugin version must be a string")
    if "author" in manifest and (
        not isinstance(manifest["author"], dict)
        or set(manifest["author"]) - {"name", "email", "url"}
        or not all(isinstance(value, str) for value in manifest["author"].values())
    ):
        raise CapabilityPluginError("plugin author must contain only string name, email, and url fields")
    if "keywords" in manifest and (
        not isinstance(manifest["keywords"], list)
        or not all(isinstance(value, str) for value in manifest["keywords"])
    ):
        raise CapabilityPluginError("plugin keywords must be a string array")
    if "extensions" in manifest and not isinstance(manifest["extensions"], dict):
        manifest.pop("extensions")

    skills: list[SkillComponent] = []
    skills_root = root / "skills"
    if skills_root.exists():
        if skills_root.is_dir():
            skill_roots = sorted(skills_root.iterdir())
        else:
            skill_roots = []
        for skill_root in skill_roots:
            if not skill_root.is_dir():
                continue
            skill_path = skill_root / "SKILL.md"
            if not skill_path.is_file():
                continue
            try:
                _inside(root, skill_path, "skill")
                name, skill_description = _frontmatter(skill_path)
            except CapabilityPluginError:
                continue
            if name != skill_root.name:
                continue
            skills.append(SkillComponent(name=name, description=skill_description, path=skill_path))

    mcp_servers: tuple[MCPComponent, ...] = ()
    mcp_path = root / "mcp.json"
    if mcp_path.exists():
        _inside(root, mcp_path, "mcp.json")
        mcp_servers = _validate_mcp(root, mcp_path, _read_json(mcp_path, "mcp.json"))
    if not skills and not mcp_servers:
        raise CapabilityPluginError(f"capability plugin has no supported skills or MCP servers: {root}")
    return CapabilityPlugin(
        id=plugin_id,
        version=manifest.get("version"),
        description=description,
        path=root,
        manifest=manifest,
        skills=tuple(skills),
        mcp_servers=mcp_servers,
    )
