"""CLI entry point for the headless core and optional local UI server."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

from pydantic import ValidationError

from turn.config import settings
from turn.contracts.dag import (
    compact_validation_error,
    validate_agent_submission,
    validate_subgraph_sources,
)
from turn.core import TurnCore
from turn.domain.schemas import AgentConfig, HarnessKind, Node, ReasoningLevel, RunPolicy
from turn.logging import EventLog


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="turn", description="Turn adaptive workgraph runtime")
    sub = root.add_subparsers(dest="command")
    server = sub.add_parser("server", aliases=["serve"], help="run the global local Turn daemon")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)

    create = sub.add_parser("create", help="create a project without the UI")
    create.add_argument("objective")
    create.add_argument("--name")
    create.add_argument(
        "--dir",
        help="use this existing project directory; defaults to the current directory",
    )
    create.add_argument("--open", action="store_true")
    create.add_argument("--harness", choices=[x.value for x in HarnessKind], default=settings.default_executor)
    create.add_argument("--model")
    create.add_argument("--reasoning", choices=[x.value for x in ReasoningLevel], default="default")
    create.add_argument("--manual", action="store_true")
    create.add_argument("--run", action="store_true", help="execute until settled or blocked")

    sub.add_parser("projects", help="list projects")
    project = sub.add_parser("project", help="inspect the current local project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_info = project_sub.add_parser(
        "info", help="show project identity, agent defaults, graph root, and harness surfaces"
    )
    project_info.add_argument("--format", choices=["json", "text"], default="json")
    graph = sub.add_parser("graph", help="print a project's workgraph as JSON")
    graph.add_argument("project_id", type=uuid.UUID, nargs="?")
    graph.add_argument(
        "--state-file",
        help="explicit local state path; normally discovered from the current directory",
    )
    graph.add_argument("--node", help="show one node from a local graph")
    graph.add_argument("--children", help="show direct children of a local node")
    graph.add_argument("--ancestors", help="show the parent chain of a local node")
    graph.add_argument("--requester", help="node id performing this read")
    graph.add_argument(
        "--subgraph-file", "--import-file", dest="subgraph_file",
        help="explore a linked subgraph source without flattening it into the project graph",
    )
    graph.add_argument("--format", choices=["tree", "json"], default="json")
    graph.add_argument("--tree", action="store_const", dest="format", const="tree")
    run = sub.add_parser("run", help="execute a project headlessly until settled")
    run.add_argument("project_id", type=uuid.UUID)
    logs = sub.add_parser("logs", help="read a project's stitched JSONL event history")
    logs.add_argument("project_id", nargs="?", type=uuid.UUID, help="defaults to the project in the current directory")
    logs.add_argument("--search", default="", help="free-text search across structured records")
    logs.add_argument("--follow", action="store_true", help="continue polling for new records")
    logs.add_argument("--format", choices=["jsonl", "text"], default="jsonl")
    logs.add_argument("--poll", type=float, default=0.25)
    agent = sub.add_parser("agent", help="small local protocol used by a running agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    status = agent_sub.add_parser("status", help="publish the agent's current state")
    status.add_argument("--state", required=True, choices=["working", "waiting", "complete", "blocked", "failed"])
    status.add_argument("--message", default="")
    submit = agent_sub.add_parser("submit", help="atomically submit a plan or execution result")
    submit.add_argument("--kind", required=True, choices=["plan", "result"])
    payload = submit.add_mutually_exclusive_group(required=True)
    payload.add_argument("--payload", help="JSON object supplied to the Turn protocol")
    payload.add_argument("--stdin", action="store_true", help="read the JSON object from stdin")
    payload.add_argument(
        "--graph-file", "--file", dest="graph_file",
        help="submit a planner graph from this project-relative JSON source file",
    )
    submit.add_argument(
        "--force", action="store_true",
        help="allow replacing a composed graph after preserving links has been checked",
    )
    verify = agent_sub.add_parser("verify", help="submit an approve/reject review decision")
    verification_payload = verify.add_mutually_exclusive_group(required=True)
    verification_payload.add_argument("--payload", help="verification JSON object")
    verification_payload.add_argument("--stdin", action="store_true", help="read verification JSON from stdin")
    capabilities = sub.add_parser("capabilities", help="browse and load capability plugins")
    capabilities_sub = capabilities.add_subparsers(dest="capabilities_command", required=True)
    capabilities_sub.add_parser("list", help="list the local capability catalog")
    search_capabilities = capabilities_sub.add_parser("search", help="fuzzy-search the local capability catalog")
    search_capabilities.add_argument("query")
    show_capability = capabilities_sub.add_parser("show", help="inspect a capability plugin")
    show_capability.add_argument("capability")
    load_capability = capabilities_sub.add_parser("load", help="add a directory to the catalog or load an id into this project")
    load_capability.add_argument("capability")
    delete_capability = capabilities_sub.add_parser("delete", help="delete a user-authored capability from the local catalog")
    delete_capability.add_argument("capability")
    doctor = sub.add_parser("doctor", help="show available coding harnesses")
    doctor.add_argument("--format", choices=["json", "text"], default="json")
    return root


def _agent_protocol_path(kind: str) -> Path:
    raw = os.getenv("TURN_HANDOFF_FILE")
    if not raw:
        raise SystemExit("TURN_HANDOFF_FILE is not set; this command must run inside a Turn agent")
    path = Path(raw).expanduser()
    expected = f".{kind}.json"
    # Any node may submit a review decision when its work discovers a defect
    # elsewhere. Ordinary workers use the result handoff path, while verifier
    # workers keep their dedicated verification path.
    accepted = (expected, ".result.json") if kind == "verification" else (expected,)
    if not any(path.name.endswith(suffix) for suffix in accepted):
        raise SystemExit(f"TURN_HANDOFF_FILE is not a {kind} handoff: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_agent_object(args) -> dict:
    try:
        if getattr(args, "graph_file", None):
            project_root = Path(os.getenv("TURN_REPO") or Path.cwd()).expanduser().resolve()
            source = Path(args.graph_file).expanduser()
            if not source.is_absolute():
                source = project_root / source
            source = source.resolve()
            try:
                source.relative_to(project_root)
            except ValueError as error:
                raise ValueError("--graph-file must point inside TURN_REPO") from error
            if source.suffix.lower() != ".json":
                raise ValueError("--graph-file must point to a .json file")
            raw = source.read_text(encoding="utf-8")
        else:
            raw = sys.stdin.read() if args.stdin else args.payload
        if not raw:
            raise ValueError("empty submission")
        value = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid agent submission: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit("agent submission must be a JSON object")
    if getattr(args, "graph_file", None):
        project_root = Path(os.getenv("TURN_REPO") or Path.cwd()).expanduser().resolve()
        source = Path(args.graph_file).expanduser()
        if not source.is_absolute():
            source = project_root / source
        relative = source.resolve().relative_to(project_root).as_posix()
        refs = value.setdefault("subgraph_refs", [])
        if not isinstance(refs, list):
            raise SystemExit("subgraph_refs must be a JSON array")
        if not any(
            (item == relative)
            or (isinstance(item, dict) and item.get("ref") == relative)
            for item in refs
        ):
            refs.append({"ref": relative, "title": Path(relative).name})
    return value


def _configured_log_limit() -> int:
    """Read the durable limit without starting the server runtime."""
    config_path = Path(os.getenv("TURN_DATA_DIR", settings.data_dir)).expanduser() / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        value = raw.get("settings", {}).get("log_max_records")
        return max(1, int(value)) if value is not None else settings.log_max_records
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return settings.log_max_records


def _cli_project_id(args: argparse.Namespace | None = None) -> str | None:
    """Resolve the project identity carried by an agent CLI invocation."""
    explicit = os.getenv("TURN_PROJECT_ID")
    if explicit:
        return explicit

    project_argument = getattr(args, "project_id", None) if args is not None else None
    if project_argument is not None:
        return str(project_argument)

    repo = os.getenv("TURN_REPO")
    if not repo:
        return None
    state_path = Path(repo).expanduser() / ".turn" / "state.json"
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    project_id = raw.get("project_id")
    if project_id:
        return str(project_id)
    roots = [
        item for item in raw.get("nodes", [])
        if isinstance(item, dict) and item.get("parent_id") is None
    ]
    if len(roots) == 1 and roots[0].get("project_id"):
        return str(roots[0]["project_id"])
    return None


def _cli_project_root(project_id: str | None) -> Path | None:
    """Resolve a project root for CLI log routing."""
    repo = os.getenv("TURN_REPO")
    if repo:
        return Path(repo).expanduser().resolve()
    if not project_id:
        return None
    config_path = Path(os.getenv("TURN_DATA_DIR", settings.data_dir)).expanduser() / "config.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        project_path = (raw.get("projects") or {}).get(project_id)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return Path(project_path).expanduser().resolve() if project_path else None


def _cli_event_log(project_id: str | None) -> EventLog:
    log = EventLog(
        os.getenv("TURN_DATA_DIR", settings.data_dir),
        _configured_log_limit(),
    )
    project_root = _cli_project_root(project_id)
    if project_id and project_root is not None:
        log.bind_project(project_id, project_root)
    return log


def _emit_cli_event(
    project_id: str | None,
    *,
    action: str,
    status: str,
    message: str,
    data: dict[str, object] | None = None,
) -> None:
    """Write a best-effort event for a command executed by an agent."""
    try:
        _cli_event_log(project_id).emit_sync(
            project_id,
            kind="agent.action",
            action=action,
            status=status,
            source="cli",
            message=message,
            data=data,
        )
    except Exception:
        # An operational log must never change the CLI result.
        return


def _cli_action(args: argparse.Namespace) -> str:
    parts = [str(args.command)]
    for name in ("agent_command", "capabilities_command", "project_command"):
        value = getattr(args, name, None)
        if value:
            parts.append(str(value))
    return "cli." + ".".join(parts)


def _cli_invocation_data(args: argparse.Namespace) -> dict[str, object]:
    """Return safe command metadata without recording submitted payloads."""
    data: dict[str, object] = {
        "node_id": os.getenv("TURN_NODE_ID"),
        "command": _cli_action(args),
    }
    for name in ("kind", "state", "query", "capability", "format", "project_id", "graph_file", "force"):
        value = getattr(args, name, None)
        if value is not None:
            data[name] = value
    return data


def _write_agent_json(path: Path, value: dict) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def agent_command(args) -> int:
    """Publish the only protocol messages Turn reads from an agent.

    The agent still runs in a completely ordinary PTY. This command is the
    control-plane interface: the CLI validates the payload and writes Turn's
    internal record. Agents never write status or outcome files themselves;
    terminal output is never used as an API.
    """
    project_id = _cli_project_id(args)
    logger = _cli_event_log(project_id)
    action = f"agent.{args.agent_command}"
    logger.emit_sync(project_id, kind="agent.action", action=action, status="started", source="cli", message="agent CLI action started", data=_cli_invocation_data(args))
    if args.agent_command == "status":
        raw = os.getenv("TURN_STATUS_FILE")
        if not raw:
            logger.emit_sync(project_id, kind="agent.action", action=action, status="error", source="cli", message="TURN_STATUS_FILE is not set", data={"response_status": "error", "node_id": os.getenv("TURN_NODE_ID")})
            raise SystemExit("TURN_STATUS_FILE is not set; this command must run inside a Turn agent")
        path = Path(raw).expanduser()
        _write_agent_json(path, {
            "node_id": os.getenv("TURN_NODE_ID"),
            "state": args.state,
            "message": args.message,
        })
        logger.emit_sync(project_id, kind="agent.action", action=action, status="ok", source="cli", message="agent status published", data={"node_id": os.getenv("TURN_NODE_ID"), "state": args.state, "message": args.message, "response_status": "accepted"})
        return 0
    kind = "verification" if args.agent_command == "verify" else args.kind
    try:
        value = _read_agent_object(args)
    except SystemExit as error:
        logger.emit_sync(project_id, kind="agent.action", action=action, status="error", source="cli", message=str(error), data={"response_status": "error", "node_id": os.getenv("TURN_NODE_ID")})
        raise
    try:
        if getattr(args, "graph_file", None) and kind != "plan":
            raise ValueError("--graph-file is only valid for plan submissions")
        validated = validate_agent_submission(kind, value)
        plan_to_validate = validated if kind == "plan" else getattr(validated, "children", None)
        if plan_to_validate is not None:
            if kind == "plan":
                from turn.workers.harnesses import validate_plan_agent_models

                validate_plan_agent_models(plan_to_validate.model_dump(mode="json"))
            from turn.capabilities.catalog import CapabilityCatalog

            project_root = os.getenv("TURN_REPO")
            if not project_root:
                raise ValueError("TURN_REPO is required to check loaded capabilities")
            catalog = CapabilityCatalog(
                Path(os.getenv("TURN_DATA_DIR", settings.data_dir)) / "capabilities"
            )
            payload = plan_to_validate.model_dump(mode="json")
            catalog.load_plan_role_capabilities(payload, project_root)
            catalog.validate_plan(
                payload,
                project_root,
                planner_capabilities=[
                    item for item in os.getenv("TURN_AGENT_CAPABILITIES", "").split(",")
                    if item
                ],
            )
            validate_subgraph_sources(plan_to_validate, project_root)
    except (TypeError, ValueError) as error:
        detail = (
            compact_validation_error(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        logger.emit_sync(project_id, kind="agent.action", action=action, status="error", source="cli", message=detail, data={"node_id": os.getenv("TURN_NODE_ID"), "kind": kind, "response_status": "rejected"})
        raise SystemExit(f"invalid {kind} submission: {detail}") from error
    try:
        target = _agent_protocol_path(kind)
    except SystemExit as error:
        logger.emit_sync(project_id, kind="agent.action", action=action, status="error", source="cli", message=str(error), data={"response_status": "error", "node_id": os.getenv("TURN_NODE_ID"), "kind": kind})
        raise
    handoff_value = dict(value)
    if getattr(args, "force", False):
        handoff_value["__turn_force"] = True
    _write_agent_json(target, handoff_value)
    status_path = os.getenv("TURN_STATUS_FILE")
    if status_path:
        _write_agent_json(Path(status_path), {
            "node_id": os.getenv("TURN_NODE_ID"),
            "state": "complete" if kind in {"result", "verification"} else "working",
            "message": "submission received",
        })
    logger.emit_sync(project_id, kind="agent.action", action=action, status="ok", source="cli", message="agent submission published", data={"node_id": os.getenv("TURN_NODE_ID"), "kind": kind, "response_status": "accepted"})
    return 0


def logs_command(args) -> int:
    project_id = args.project_id
    project_root = None
    if project_id is None:
        state_file = discover_project_state()
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        project_id = uuid.UUID(str(raw.get("project_id") or next(item["project_id"] for item in raw.get("nodes", []) if item.get("parent_id") is None)))
        project_root = state_file.parent.parent
    log = _cli_event_log(str(project_id))
    if project_root is not None:
        log.bind_project(str(project_id), project_root)
    records = log.follow(project_id, search=args.search, poll_seconds=max(0.05, args.poll)) if args.follow else iter(log.read(project_id, search=args.search, limit=100_000))
    try:
        for record in records:
            if args.format == "text":
                print(f"{record.get('timestamp', '')} {record.get('status', 'info'):>5} {record.get('kind', '')}: {record.get('message', '')}", flush=True)
            else:
                print(json.dumps(record, ensure_ascii=False, default=str), flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


def capabilities_command(args) -> int:
    """Run a capability CLI action and record its response status."""
    from turn.capabilities.catalog import CapabilityCatalog
    from turn.capabilities.plugin import load_capability_plugin

    project_id = _cli_project_id(args)
    action = f"capabilities.{args.capabilities_command}"
    invocation = _cli_invocation_data(args)
    _emit_cli_event(
        project_id,
        action=action,
        status="started",
        message="capability CLI action started",
        data=invocation,
    )
    catalog = CapabilityCatalog(
        Path(os.getenv("TURN_DATA_DIR", settings.data_dir)) / "capabilities"
    )

    def finish(data: dict[str, object]) -> int:
        _emit_cli_event(
            project_id,
            action=action,
            status="ok",
            message="capability CLI action completed",
            data={**invocation, **data, "response_status": "accepted"},
        )
        return 0

    try:
        if args.capabilities_command == "list":
            entries = [entry.as_dict() for entry in catalog.list()]
            print(json.dumps(entries, indent=2))
            return finish({"result_count": len(entries)})
        if args.capabilities_command == "search":
            entries = [entry.as_dict() for entry in catalog.search(args.query)]
            print(json.dumps(entries, indent=2))
            return finish({"result_count": len(entries)})
        if args.capabilities_command == "show":
            candidate = Path(args.capability).expanduser()
            plugin = load_capability_plugin(candidate) if candidate.is_dir() else catalog.get(args.capability)
            print(json.dumps({
                "id": plugin.id,
                "version": plugin.version,
                "description": plugin.description,
                "path": str(plugin.path),
                "skills": [{"name": item.name, "description": item.description, "path": str(item.path)} for item in plugin.skills],
                "mcps": [{"name": item.name, "config": item.config} for item in plugin.mcp_servers],
            }, indent=2))
            return finish({"capability_id": plugin.id})
        if args.capabilities_command == "delete":
            deleted = catalog.delete(args.capability)
            print(json.dumps({"id": args.capability, "deleted": True, "catalog_path": str(deleted)}, indent=2))
            return finish({"capability_id": args.capability, "deleted": True})

        candidate = Path(args.capability).expanduser()
        if candidate.is_dir():
            plugin = catalog.import_directory(candidate)
            print(json.dumps({"catalog_path": str(plugin.path), "id": plugin.id}, indent=2))
            return finish({"capability_id": plugin.id, "operation": "import"})
        project_root = os.getenv("TURN_REPO") or str(Path.cwd())
        target = catalog.load_into_project(args.capability, project_root)
        print(json.dumps({"project_path": str(target), "id": args.capability}, indent=2))
        return finish({"capability_id": args.capability, "project_path": str(target), "operation": "load"})
    except BaseException as error:
        _emit_cli_event(
            project_id,
            action=action,
            status="error",
            message=str(error),
            data={**invocation, "response_status": "error", "error_type": type(error).__name__},
        )
        raise


def discover_project_state(start: Path | None = None) -> Path:
    """Find the nearest project state while staying within the user's home."""
    current = (start or Path.cwd()).expanduser().resolve()
    home = Path.home().expanduser().resolve()
    while True:
        candidate = current / ".turn" / "state.json"
        if candidate.is_file():
            return candidate
        if current == home or current.parent == current:
            break
        current = current.parent
    raise SystemExit(
        "no project state found: run this command inside a project with .turn/state.json"
    )


async def local_graph_command(args) -> int:
    """Read a project-local graph through the installed Turn CLI."""
    from turn.tools import graph_explorer

    if getattr(args, "subgraph_file", None):
        payload = graph_explorer.read_subgraph_file(args.subgraph_file)
        if args.format == "json":
            print(json.dumps(payload, indent=2))
        else:
            graph_explorer.print_subgraph_tree(payload)
        return 0

    state_file = args.state_file or str(discover_project_state())
    if args.project_id is None:
        raise SystemExit("graph project_id is required unless --subgraph-file is used")
    query = (
        "node" if args.node
        else "children" if args.children
        else "ancestors" if args.ancestors
        else args.format
    )
    nodes, children = await graph_explorer._query(
        state_file, str(args.project_id), args.requester, query
    )
    by_id = {uuid.UUID(item["id"]).hex: item for item in nodes}
    if args.node:
        node_id = uuid.UUID(args.node).hex
        output = [by_id[node_id]] if node_id in by_id else []
    elif args.children:
        parent_id = uuid.UUID(args.children).hex
        output = [by_id[item] for item in children.get(parent_id, []) if item in by_id]
    elif args.ancestors:
        current_id = uuid.UUID(args.ancestors).hex
        output = []
        while current_id in by_id:
            current = by_id[current_id]
            output.append(current)
            current_id = uuid.UUID(current["parent_id"]).hex if current["parent_id"] else ""
    else:
        output = nodes

    if args.format == "json":
        print(json.dumps(output, indent=2))
    elif args.node or args.children or args.ancestors:
        for item in output:
            print("- " + graph_explorer._summary(item))
    else:
        graph_explorer._print_tree(nodes, children)
    return 0


def _project_info() -> dict:
    """Read project-local identity plus explicit runtime discovery metadata."""
    from turn.workers.harness_catalog import REAL_HARNESS_CATALOG

    state_file = discover_project_state()
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"unable to read project state: {state_file}: {error}") from error
    nodes = [Node.model_validate(item) for item in raw.get("nodes", [])]
    roots = [node for node in nodes if node.parent_id is None]
    if len(roots) != 1:
        raise SystemExit(f"project state must contain exactly one root node: {state_file}")
    root = roots[0]

    # The server persists role defaults in the shared local config. Read only
    # this non-secret setting so this command remains usable without starting
    # the daemon and without importing the storage event loop.
    config_file = Path(os.getenv("TURN_DATA_DIR", settings.data_dir)).expanduser() / "config.json"
    persisted_defaults = None
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        candidate = config.get("settings", {}).get("agent_defaults")
        if isinstance(candidate, str):
            candidate = json.loads(candidate)
        if isinstance(candidate, dict):
            persisted_defaults = candidate
    except (OSError, json.JSONDecodeError, TypeError):
        persisted_defaults = None

    loaded = sorted(
        path.name
        for path in (state_file.parent / "capabilities").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ) if (state_file.parent / "capabilities").is_dir() else []

    return {
        "project": {
            "id": str(root.project_id),
            "name": root.project_name,
            "objective": root.objective,
            "repo_path": root.repo_path or str(state_file.parent.parent),
            "state_file": str(state_file),
        },
        "root_agent": root.agent.model_dump(mode="json") if root.agent else None,
        "agent_defaults": persisted_defaults or settings.agent_defaults,
        "loaded_capabilities": loaded,
        "harnesses": REAL_HARNESS_CATALOG.as_dict(),
    }


def local_project_info_command(args) -> int:
    info = _project_info()
    if args.format == "text":
        project = info["project"]
        print(f"project: {project['name'] or project['id']}")
        print(f"id: {project['id']}")
        print(f"repo: {project['repo_path']}")
        print(f"root agent: {info['root_agent']}")
        print(f"agent defaults: {json.dumps(info['agent_defaults'], sort_keys=True)}")
        print("loaded capabilities: " + (", ".join(info["loaded_capabilities"]) or "none"))
        return 0
    print(json.dumps(info, indent=2))
    return 0


def _run_logged_cli(args: argparse.Namespace) -> int:
    """Run a non-protocol CLI command with an outcome event."""
    project_id = _cli_project_id(args)
    action = _cli_action(args)
    invocation = _cli_invocation_data(args)
    _emit_cli_event(
        project_id,
        action=action,
        status="started",
        message="CLI command started",
        data=invocation,
    )
    try:
        result = asyncio.run(async_main(args))
    except BaseException as error:
        _emit_cli_event(
            project_id,
            action=action,
            status="error",
            message=str(error),
            data={**invocation, "response_status": "error", "error_type": type(error).__name__},
        )
        raise
    _emit_cli_event(
        project_id,
        action=action,
        status="ok",
        message="CLI command completed",
        data={**invocation, "response_status": "accepted", "return_code": result},
    )
    return result


async def async_main(args) -> int:
    if args.command == "agent":
        return agent_command(args)
    if args.command == "logs":
        return logs_command(args)
    if args.command == "graph":
        return await local_graph_command(args)
    if args.command == "project":
        if args.project_command == "info":
            return local_project_info_command(args)
        raise SystemExit(f"unsupported project command: {args.project_command}")
    if args.command == "doctor":
        from turn.workers.harnesses import harness_capabilities

        payload = {"harnesses": harness_capabilities()}
        if args.format == "text":
            for item in payload["harnesses"]:
                print(f"{item['id']}: {'available' if item['available'] else 'not found'}")
            return 0
        print(json.dumps(payload, indent=2))
        return 0
    if args.command == "capabilities":
        return capabilities_command(args)
    async with TurnCore(settings) as core:
        if args.command == "projects":
            projects = await core.store.list_projects()
            print(json.dumps([p.model_dump(mode="json") for p in projects], indent=2))
            return 0
        if args.command == "create":
            project = await core.create_project(
                args.objective,
                name=args.name,
                working_dir=args.dir or str(Path.cwd()),
                open_existing=args.open,
                agent=AgentConfig(harness=args.harness, model=args.model, reasoning=args.reasoning),
                run_policy=RunPolicy(auto_run=not args.manual),
            )
            if args.run:
                await core.run_until_settled(project.id)
            print(json.dumps({"project_id": str(project.id), "repo_path": project.repo_path}, indent=2))
            return 0
        if args.command == "run":
            nodes = await core.run_until_settled(args.project_id)
            print(json.dumps({"project_id": str(args.project_id), "nodes": len(nodes), "statuses": {s: sum(n.status.value == s for n in nodes) for s in sorted({n.status.value for n in nodes})}}, indent=2))
            return 0
    return 1


def main() -> None:
    args = parser().parse_args()
    if args.command in (None, "server", "serve"):
        from turn.server.daemon import TurnDaemon

        TurnDaemon(settings).run(
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8000),
        )
        return
    if args.command in {"agent", "capabilities", "logs"}:
        raise SystemExit(asyncio.run(async_main(args)))
    raise SystemExit(_run_logged_cli(args))


if __name__ == "__main__":
    main()
