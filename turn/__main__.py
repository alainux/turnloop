"""CLI entry point for the headless core and optional local UI server."""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import uvicorn

from turn.config import settings
from turn.core import TurnCore
from turn.domain.schemas import AgentConfig, HarnessKind, ReasoningLevel, RunPolicy


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="turn", description="Turn adaptive workgraph runtime")
    sub = root.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the local ADE")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    create = sub.add_parser("create", help="create a project without the UI")
    create.add_argument("objective")
    create.add_argument("--name")
    create.add_argument("--dir")
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
    sub.add_parser("doctor", help="show available coding harnesses")
    return root


async def async_main(args) -> int:
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
                working_dir=args.dir,
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
    # Backward compatibility: `turn` and `python -m turn` still open the ADE.
    if args.command in (None, "serve"):
        uvicorn.run("turn.server.app:app", host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 8000), log_level="info")
        return
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
