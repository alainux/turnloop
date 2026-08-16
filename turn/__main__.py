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
from turn.contracts.dag import compact_validation_error, validate_agent_submission
from turn.core import TurnCore
from turn.domain.schemas import AgentConfig, HarnessKind, ReasoningLevel, RunPolicy


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
    graph = sub.add_parser("graph", help="print a project's workgraph as JSON")
    graph.add_argument("project_id", type=uuid.UUID)
    graph.add_argument(
        "--state-file",
        help="explicit local state path; normally discovered from the current directory",
    )
    graph.add_argument("--node", help="show one node from a local graph")
    graph.add_argument("--children", help="show direct children of a local node")
    graph.add_argument("--ancestors", help="show the parent chain of a local node")
    graph.add_argument("--requester", help="node id performing this read")
    graph.add_argument("--format", choices=["tree", "json"], default="json")
    graph.add_argument("--tree", action="store_const", dest="format", const="tree")
    run = sub.add_parser("run", help="execute a project headlessly until settled")
    run.add_argument("project_id", type=uuid.UUID)
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
    verify = agent_sub.add_parser("verify", help="approve or reject the predecessor's work")
    verification_payload = verify.add_mutually_exclusive_group(required=True)
    verification_payload.add_argument("--payload", help="verification JSON object")
    verification_payload.add_argument("--stdin", action="store_true", help="read verification JSON from stdin")
    skills = sub.add_parser("skills", help="inspect the project-scoped skill library")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_sub.add_parser("list", help="list available skill ids")
    show_skill = skills_sub.add_parser("show", help="print one skill")
    show_skill.add_argument("skill_id")
    sub.add_parser("doctor", help="show available coding harnesses")
    return root


def _agent_protocol_path(kind: str) -> Path:
    raw = os.getenv("TURN_HANDOFF_FILE")
    if not raw:
        raise SystemExit("TURN_HANDOFF_FILE is not set; this command must run inside a Turn agent")
    path = Path(raw).expanduser()
    expected = f".{kind}.json"
    if not path.name.endswith(expected):
        raise SystemExit(f"TURN_HANDOFF_FILE is not a {kind} handoff: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_agent_object(args) -> dict:
    try:
        raw = sys.stdin.read() if args.stdin else args.payload
        if not raw:
            raise ValueError("empty submission")
        value = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid agent submission: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit("agent submission must be a JSON object")
    return value


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
    if args.agent_command == "status":
        raw = os.getenv("TURN_STATUS_FILE")
        if not raw:
            raise SystemExit("TURN_STATUS_FILE is not set; this command must run inside a Turn agent")
        path = Path(raw).expanduser()
        _write_agent_json(path, {
            "node_id": os.getenv("TURN_NODE_ID"),
            "state": args.state,
            "message": args.message,
        })
        return 0
    kind = "verification" if args.agent_command == "verify" else args.kind
    value = _read_agent_object(args)
    try:
        validate_agent_submission(kind, value)
    except (TypeError, ValueError) as error:
        detail = (
            compact_validation_error(error)
            if isinstance(error, ValidationError)
            else str(error)
        )
        raise SystemExit(f"invalid {kind} submission: {detail}") from error
    target = _agent_protocol_path(kind)
    _write_agent_json(target, value)
    status_path = os.getenv("TURN_STATUS_FILE")
    if status_path:
        _write_agent_json(Path(status_path), {
            "node_id": os.getenv("TURN_NODE_ID"),
            "state": "complete" if kind in {"result", "verification"} else "working",
            "message": "submission received",
        })
    return 0


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

    state_file = args.state_file or str(discover_project_state())
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


async def async_main(args) -> int:
    if args.command == "agent":
        return agent_command(args)
    if args.command == "graph":
        return await local_graph_command(args)
    if args.command == "doctor":
        from turn.workers.harnesses import harness_capabilities

        print(json.dumps({"harnesses": harness_capabilities()}, indent=2))
        return 0
    if args.command == "skills":
        from turn.skills.library import get_skill, list_skills

        if args.skills_command == "list":
            print(json.dumps([
                {
                    "id": item.id,
                    "title": item.title,
                    "description": item.description,
                    "source_url": item.source_url,
                }
                for item in list_skills()
            ], indent=2))
            return 0
        item = get_skill(args.skill_id)
        print(item.source_path.read_text(encoding="utf-8"))
        return 0
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
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
