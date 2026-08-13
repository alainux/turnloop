"""Graph-exploration tool for Codex agents.

This is Turn's first agent tool. Rather than injecting a frozen snapshot of
the project into every prompt, the agent can *query* the live workgraph at
runtime from its shell:

    python /abs/path/to/graph_explorer.py --project <id> --db "<url>" --tree

It prints every node already planned/built in the current project: its
objective, parent, status, executor, and the files it produced. Filters let
the agent drill in:

    --node <id>        show one node
    --children <id>    show a node's direct children
    --ancestors <id>   show a node's parent chain
    --format json      machine-readable output

The command is self-contained: it talks to the project database directly with
raw SQLAlchemy, so it needs NO `turn` package on the path and NO environment
variables (the absolute DB url and project id are passed explicitly). This is
what lets a nested planner see that "audio" or "the engine" already exists
elsewhere in the graph and build on it instead of recreating it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import create_async_engine

# Artifact kinds that represent real deliverable files vs. internal bookkeeping.
_DELIVERABLE_KINDS = {"file", "code_diff", "log", "evidence"}
_INTERNAL_NAMES = {"transcript", "worktree-path", "codex-output", "plan"}


async def _query(db_url: str, project_id: str, requester: str | None = None, query: str = "tree"):
    pid = uuid.UUID(project_id).hex
    engine = create_async_engine(db_url, connect_args={"check_same_thread": False})
    try:
        async with engine.begin() as conn:
            if requester:
                requester_id = uuid.UUID(requester).hex
                # Keep this tool self-contained: an agent may run it against a
                # database created by an older Turn build before app startup.
                await conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS graph_inspections ("
                    "id CHAR(32) PRIMARY KEY, project_id CHAR(32) NOT NULL, "
                    "requester_node_id CHAR(32) NOT NULL, query TEXT NOT NULL, "
                    "created_at TEXT NOT NULL)"
                ))
                await conn.execute(
                    text(
                        "INSERT INTO graph_inspections "
                        "(id, project_id, requester_node_id, query, created_at) "
                        "VALUES (:id, :pid, :requester, :query, :created_at)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "pid": pid,
                        "requester": requester_id,
                        "query": query,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            nrows = (
                await conn.execute(
                    text(
                        "SELECT id, parent_id, objective, status, executor, needs_review "
                        "FROM nodes WHERE project_id = :pid"
                    ),
                    {"pid": pid},
                )
            ).fetchall()
            ids = [r.id for r in nrows]
            erows = []
            arows = []
            if ids:
                erows = (
                    await conn.execute(
                        text("SELECT src, dst FROM edges WHERE src IN :ids").bindparams(
                            bindparam("ids", expanding=True)
                        ),
                        {"ids": tuple(ids)},
                    )
                ).fetchall()
                arows = (
                    await conn.execute(
                        text(
                            "SELECT node_id, name, kind, ref "
                            "FROM artifacts WHERE node_id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": tuple(ids)},
                    )
                ).fetchall()
    finally:
        await engine.dispose()

    arts: dict[str, list] = {}
    for a in arows:
        arts.setdefault(a.node_id, []).append((a.name, str(a.kind), a.ref))

    nodes = []
    for r in nrows:
        nodes.append(
            {
                "id": r.id,
                "parent_id": r.parent_id,
                "objective": r.objective,
                "status": r.status,
                "executor": r.executor,
                "needs_review": bool(r.needs_review),
                "files": _files_for(arts.get(r.id, [])),
            }
        )
    children: dict[str, list[str]] = {}
    for e in erows:
        children.setdefault(e.src, []).append(e.dst)
    return nodes, children


def _files_for(rows) -> list[str]:
    out = []
    for name, kind, ref in rows or []:
        kind = (kind or "").lower()
        if kind not in _DELIVERABLE_KINDS:
            continue
        name = name or ref or ""
        if not name or name in _INTERNAL_NAMES or name.startswith("git-"):
            continue
        out.append(name)
    return out


def _summary(s) -> str:
    line = f"[{s['status']}|{s['executor']}] {s['objective']}"
    if s["files"]:
        line += "  -> " + ", ".join(s["files"])
    return line


def _print_tree(nodes, children):
    by_id = {n["id"]: n for n in nodes}
    roots = [n for n in nodes if not n["parent_id"]]

    def show(n, depth):
        print("  " * depth + "- " + _summary(n))
        for c in children.get(n["id"], []):
            if c in by_id:
                show(by_id[c], depth + 1)

    for r in roots:
        show(r, 0)


async def _main_async():
    ap = argparse.ArgumentParser(description="Explore the live Turn project graph.")
    ap.add_argument("--project", required=True, help="Project id (hyphenated or 32-hex).")
    ap.add_argument("--db", required=True, help="Absolute SQLAlchemy DB url, e.g. sqlite+aiosqlite:////abs/path/turnloop.db")
    ap.add_argument("--node", help="Show only this node id.")
    ap.add_argument("--children", help="Show only the direct children of this node id.")
    ap.add_argument("--ancestors", help="Show only the parent chain of this node id.")
    ap.add_argument(
        "--requester",
        help="Node id performing this inspection; records durable audit evidence.",
    )
    ap.add_argument("--format", default="tree", choices=["tree", "json"])
    ap.add_argument("--tree", action="store_const", dest="format", const="tree",
                    help="alias for --format tree (the default)")
    args = ap.parse_args()

    query = "node" if args.node else "children" if args.children else "ancestors" if args.ancestors else args.format
    nodes, children = await _query(args.db, args.project, args.requester, query)
    by_id = {n["id"]: n for n in nodes}

    if args.node:
        out = [by_id[uuid.UUID(args.node).hex]] if uuid.UUID(args.node).hex in by_id else []
    elif args.children:
        pid = uuid.UUID(args.children).hex
        out = [by_id[i] for i in children.get(pid, []) if i in by_id]
    elif args.ancestors:
        aid = uuid.UUID(args.ancestors).hex
        chain = []
        cur = by_id.get(aid)
        while cur:
            chain.append(cur)
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
        out = chain
    else:
        out = nodes

    if args.format == "json":
        print(json.dumps(out, indent=2))
    elif args.node or args.children or args.ancestors:
        for s in out:
            print("- " + _summary(s))
    else:
        _print_tree(nodes, children)
    if args.requester:
        print(f"[turn] graph inspection recorded for {uuid.UUID(args.requester).hex}")
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
