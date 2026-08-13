"""Graph-exploration tool for Codex agents.

This is Turn's first agent tool. Rather than injecting a frozen snapshot of
the project into every prompt, the agent can *query* the live workgraph at
runtime from its shell:

    python -m turn.tools.graph_explorer --tree

It prints every node already planned/built in the current project: its
objective, parent, status, executor, and the files it produced. Filters let
the agent drill in:

    --node <id>        show one node
    --children <id>    show a node's direct children
    --ancestors <id>   show a node's parent chain
    --format json      machine-readable output

The worker sets TURN_PROJECT_ID and TURN_DATABASE_URL in the agent's
environment, so the command always targets the real, current project state.
This is what lets a nested planner see that "audio" or "the engine" already
exists elsewhere in the graph and build on it instead of recreating it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

from turn.config import settings
from turn.db.store import Store

# Artifact kinds that represent real deliverable files vs. internal bookkeeping.
_DELIVERABLE_KINDS = {"file", "code_diff", "log", "evidence"}
_INTERNAL_NAMES = {"transcript", "worktree-path", "codex-output", "plan"}


async def _collect(project_id: str):
    store = Store(settings.database_url)
    try:
        nodes, edges, _ = await store.get_workgraph(uuid.UUID(project_id))
        by_id = {n.id: n for n in nodes}
        children: dict[uuid.UUID, list[uuid.UUID]] = {}
        for e in edges:
            children.setdefault(e.src, []).append(e.dst)
        arts: dict[uuid.UUID, list] = {}
        for n in nodes:
            try:
                arts[n.id] = await store.get_artifacts(n.id)
            except Exception:
                arts[n.id] = []
        return nodes, by_id, children, arts
    finally:
        await store.engine.dispose()


def _files_for(nid, arts):
    out = []
    for a in arts.get(nid, []) or []:
        kind = a.kind.value if hasattr(a.kind, "value") else str(a.kind)
        kind = kind.lower()
        if kind not in _DELIVERABLE_KINDS:
            continue
        name = a.name or a.ref or ""
        if not name or name in _INTERNAL_NAMES or name.startswith("git-"):
            continue
        out.append(name)
    return out


def _summary(n, arts):
    return {
        "id": str(n.id),
        "objective": n.objective,
        "status": n.status,
        "executor": n.executor,
        "parent_id": str(n.parent_id) if n.parent_id else None,
        "needs_review": bool(getattr(n, "needs_review", False)),
        "files": _files_for(n.id, arts),
    }


def _print_tree(nodes, by_id, children, arts):
    roots = [n for n in nodes if not n.parent_id]

    def show(n, depth):
        s = _summary(n, arts)
        indent = "  " * depth
        line = f"{indent}- [{s['status']}|{s['executor']}] {s['objective']}"
        if s["files"]:
            line += "  -> " + ", ".join(s["files"])
        print(line)
        for c in children.get(n.id, []):
            if c in by_id:
                show(by_id[c], depth + 1)

    for r in roots:
        show(r, 0)


async def _main_async():
    ap = argparse.ArgumentParser(description="Explore the live Turn project graph.")
    ap.add_argument("--project", default=os.environ.get("TURN_PROJECT_ID"),
                    help="Project id (defaults to TURN_PROJECT_ID env).")
    ap.add_argument("--node", help="Show only this node id.")
    ap.add_argument("--children", help="Show only the direct children of this node id.")
    ap.add_argument("--ancestors", help="Show only the parent chain of this node id.")
    ap.add_argument("--format", default="tree", choices=["tree", "json"])
    ap.add_argument("--tree", action="store_const", dest="format", const="tree",
                    help="alias for --format tree (the default)")
    args = ap.parse_args()

    if not args.project:
        print("error: set TURN_PROJECT_ID or pass --project <id>", file=sys.stderr)
        return 1

    nodes, by_id, children, arts = await _collect(args.project)

    if args.node:
        target = uuid.UUID(args.node)
        out = [_summary(by_id[target], arts)] if target in by_id else []
    elif args.children:
        pid = uuid.UUID(args.children)
        out = [_summary(by_id[i], arts) for i in children.get(pid, []) if i in by_id]
    elif args.ancestors:
        aid = uuid.UUID(args.ancestors)
        chain = []
        cur = by_id.get(aid)
        while cur:
            chain.append(cur)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        out = [_summary(n, arts) for n in chain]
    else:
        out = [_summary(n, arts) for n in nodes]

    if args.format == "json":
        print(json.dumps(out, indent=2))
    else:
        if args.node or args.children or args.ancestors:
            for s in out:
                print(f"- [{s['status']}|{s['executor']}] {s['objective']}"
                      + (f"  -> {', '.join(s['files'])}" if s["files"] else ""))
        else:
            _print_tree(nodes, by_id, children, arts)
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
