"""Read-only graph explorer for Turn agents.

The explorer reads the project's local ``.turn/state.json`` directly.  It is
deliberately self-contained so an agent can invoke the absolute script path
without importing the Turn application or opening a service.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

_DELIVERABLE_KINDS = {"file", "code_diff", "log", "evidence"}
_INTERNAL_NAMES = {"transcript", "filesystem-path", "codex-output", "plan"}


def _state_path(location: str) -> Path:
    path = Path(location).expanduser()
    if path.name == "state.json":
        return path
    if (path / "state.json").exists():
        return path / "state.json"
    return path / ".turn" / "state.json"


def _load_state(location: str, project_id: str) -> dict:
    path = _state_path(location)
    if not path.exists():
        raise FileNotFoundError(f"project state file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


async def _query(state_file: str, project_id: str, requester: str | None = None, query: str = "tree"):
    """Return nodes and CONTAINS children; ``requester`` is intentionally ignored."""
    raw = await asyncio.to_thread(_load_state, state_file, project_id)
    wanted = uuid.UUID(project_id).hex
    nodes = []
    by_id: dict[str, dict] = {}
    artifacts: dict[str, list] = {}
    for artifact in raw.get("artifacts", []):
        artifact_id = str(artifact.get("node_id", ""))
        artifacts.setdefault(artifact_id.replace("-", ""), []).append(
            (artifact.get("name"), artifact.get("kind"), artifact.get("ref"))
        )
    for item in raw.get("nodes", []):
        node_id = uuid.UUID(str(item["id"])).hex
        if uuid.UUID(str(item["project_id"])).hex != wanted:
            continue
        parent_id = item.get("parent_id")
        parent_id = uuid.UUID(str(parent_id)).hex if parent_id else None
        node = {
            "id": node_id,
            "parent_id": parent_id,
            "objective": item.get("objective", ""),
            "status": item.get("status", "PENDING"),
            "executor": item.get("executor"),
            "files": _files_for(artifacts.get(node_id, [])),
        }
        nodes.append(node)
        by_id[node_id] = node
    children: dict[str, list[str]] = {}
    for edge in raw.get("edges", []):
        if edge.get("type") != "CONTAINS":
            continue
        src = uuid.UUID(str(edge["src"])).hex
        dst = uuid.UUID(str(edge["dst"])).hex
        if src in by_id and dst in by_id:
            children.setdefault(src, []).append(dst)
    return nodes, children


def _files_for(rows) -> list[str]:
    output = []
    for name, kind, ref in rows or []:
        kind = str(kind or "").lower()
        if kind not in _DELIVERABLE_KINDS:
            continue
        name = name or ref or ""
        if not name or name in _INTERNAL_NAMES:
            continue
        output.append(name)
    return output


def _summary(item: dict) -> str:
    line = f"[{item['status']}|{item['executor']}] {item['objective']}"
    if item["files"]:
        line += "  -> " + ", ".join(item["files"])
    return line


def _print_tree(nodes, children):
    by_id = {item["id"]: item for item in nodes}
    roots = [item for item in nodes if not item["parent_id"]]

    def show(item, depth):
        print("  " * depth + "- " + _summary(item))
        for child_id in children.get(item["id"], []):
            if child_id in by_id:
                show(by_id[child_id], depth + 1)

    for root in roots:
        show(root, 0)


async def _main_async():
    parser = argparse.ArgumentParser(description="Explore the live Turn project graph.")
    parser.add_argument("--project", required=True, help="Project id (hyphenated or 32-hex).")
    parser.add_argument("--state-file", required=True, help="Project .turn/state.json path.")
    parser.add_argument("--node", help="Show only this node id.")
    parser.add_argument("--children", help="Show only the direct children of this node id.")
    parser.add_argument("--ancestors", help="Show only the parent chain of this node id.")
    parser.add_argument("--requester", help="Node id performing this read (not persisted).")
    parser.add_argument("--format", default="tree", choices=["tree", "json"])
    parser.add_argument("--tree", action="store_const", dest="format", const="tree")
    args = parser.parse_args()

    query = "node" if args.node else "children" if args.children else "ancestors" if args.ancestors else args.format
    nodes, children = await _query(args.state_file, args.project, args.requester, query)
    by_id = {item["id"]: item for item in nodes}
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
            current_id = current["parent_id"] or ""
    else:
        output = nodes

    if args.format == "json":
        print(json.dumps(output, indent=2))
    elif args.node or args.children or args.ancestors:
        for item in output:
            print("- " + _summary(item))
    else:
        _print_tree(nodes, children)
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
