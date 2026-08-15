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

from turn.config import settings
from turn.contracts.dag import validate_agent_submission
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
    value = _read_agent_object(args)
    try:
        validate_agent_submission(args.kind, value)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"invalid {args.kind} submission: {error}") from error
    target = _agent_protocol_path(args.kind)
    _write_agent_json(target, value)
    status_path = os.getenv("TURN_STATUS_FILE")
    if status_path:
        _write_agent_json(Path(status_path), {
            "node_id": os.getenv("TURN_NODE_ID"),
            "state": "complete" if args.kind == "result" else "working",
            "message": "submission received",
        })
    return 0


async def async_main(args) -> int:
    if args.command == "agent":
        return agent_command(args)
    if args.command == "doctor":
        from turn.workers.harnesses import harness_capabilities

        print(json.dumps({"harnesses": harness_capabilities()}, indent=2))
        return 0
    async with TurnCore(settings) as core:
        if args.command == "projects":
            projects = await core.store.list_projects()
            print(json.dumps([p.model_dump(mode="json") for p in projects], indent=2))
            return 0
        if args.command == "graph":
            nodes, edges, artifacts = await core.graph(args.project_id)
            print(json.dumps({"nodes": [n.model_dump(mode="json") for n in nodes], "edges": [e.model_dump(mode="json") for e in edges], "artifacts": [a.model_dump(mode="json") for a in artifacts]}, indent=2))
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
