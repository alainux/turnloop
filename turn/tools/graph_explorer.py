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

from turn.contracts.graph_inspection import (
    GraphInspection,
    GraphInspectionArtifact,
    GraphInspectionNode,
    GraphInspectionRun,
)
from turn.domain.schemas import Artifact, Edge, Node, Run

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
    """Return the coordination-relevant graph state and CONTAINS children."""
    raw = await asyncio.to_thread(_load_state, state_file, project_id)
    project_uuid = uuid.UUID(project_id)
    parsed_nodes = [
        Node.model_validate(item)
        for item in raw.get("nodes", [])
        if uuid.UUID(str(item["project_id"])) == project_uuid
    ]
    by_id = {node.id: node for node in parsed_nodes}
    parsed_edges = [
        Edge.model_validate(item)
        for item in raw.get("edges", [])
        if uuid.UUID(str(item["src"])) in by_id and uuid.UUID(str(item["dst"])) in by_id
    ]
    parsed_runs = [Run.model_validate(item) for item in raw.get("runs", [])]
    parsed_artifacts = [Artifact.model_validate(item) for item in raw.get("artifacts", [])]

    children: dict[str, list[str]] = {}
    dependencies: dict[uuid.UUID, list[uuid.UUID]] = {}
    for parsed_edge in parsed_edges:
        if parsed_edge.type.value == "CONTAINS":
            children.setdefault(parsed_edge.src.hex, []).append(parsed_edge.dst.hex)
        elif parsed_edge.type.value == "DEPENDS_ON":
            dependencies.setdefault(parsed_edge.dst, []).append(parsed_edge.src)

    artifacts_by_node: dict[uuid.UUID, list[Artifact]] = {}
    for artifact in parsed_artifacts:
        if artifact.node_id in by_id:
            artifacts_by_node.setdefault(artifact.node_id, []).append(artifact)
    runs_by_node: dict[uuid.UUID, list[Run]] = {}
    for run in parsed_runs:
        if run.node_id in by_id:
            runs_by_node.setdefault(run.node_id, []).append(run)

    inspection_nodes: list[GraphInspectionNode] = []
    for node in parsed_nodes:
        node_artifacts = artifacts_by_node.get(node.id, [])
        node_runs = runs_by_node.get(node.id, [])
        inspection_nodes.append(
            GraphInspectionNode(
                id=node.id,
                parent_id=node.parent_id,
                objective=node.objective,
                instructions=node.generated_prompt,
                status=node.status,
                executor=node.executor,
                agent=node.agent,
                session_id=node.agent.session_id if node.agent else None,
                agent_state=node.agent_state,
                agent_message=node.agent_message,
                verification=node.verification,
                paused=node.paused,
                auto_run=node.auto_run,
                run_policy=node.run_policy,
                required_inputs=node.required_inputs,
                resource_refs=node.resource_refs,
                document_refs=node.document_refs,
                artifact_refs=node.artifact_refs,
                depends_on=dependencies.get(node.id, []),
                children=[uuid.UUID(child) for child in children.get(node.id.hex, [])],
                files=_files_for(
                    [(artifact.name, artifact.kind.value, artifact.ref) for artifact in node_artifacts]
                ),
                artifacts=[
                    GraphInspectionArtifact(
                        id=artifact.id,
                        node_id=artifact.node_id,
                        kind=artifact.kind,
                        name=artifact.name,
                        ref=artifact.ref,
                    )
                    for artifact in node_artifacts
                ],
                runs=[
                    GraphInspectionRun(
                        id=run.id,
                        attempt=run.attempt,
                        worker=run.worker,
                        status=run.status,
                        outcome=run.outcome,
                        summary=run.summary,
                        error=run.error,
                        started_at=run.started_at,
                        ended_at=run.ended_at,
                        session_id=run.session_id,
                    )
                    for run in node_runs
                ],
            )
        )

    inspection = GraphInspection(
        project_id=project_uuid,
        nodes=inspection_nodes,
        edges=parsed_edges,
    )
    return [node.model_dump(mode="json") for node in inspection.nodes], children


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
    line = f"[{item['id']}] [{item['status']}|{item['executor']}] {item['objective']}"
    if item.get("parent_id"):
        line += f"  parent={item['parent_id']}"
    if item.get("depends_on"):
        line += "\n  depends_on: " + ", ".join(item["depends_on"])
    if item.get("verification"):
        decision = item["verification"].get("decision", "unknown")
        line += f"\n  verification: {decision} — {item['verification'].get('summary', '')}"
    line += f"\n  execution: paused={item['paused']}, auto_run={item['auto_run']}"
    if item.get("run_policy"):
        line += "\n  run_policy: " + json.dumps(item["run_policy"], sort_keys=True)
    if item.get("required_inputs"):
        line += "\n  required_inputs: " + ", ".join(
            input_spec["id"] for input_spec in item["required_inputs"]
        )
    agent = item.get("agent") or {}
    if agent:
        config = ", ".join(
            f"{key}={agent.get(key) or 'default'}"
            for key in ("type_id", "harness", "model", "reasoning", "permission", "session_id")
        )
        line += "\n  agent: " + config
        if agent.get("skills"):
            line += "\n  skills: " + ", ".join(agent["skills"])
        if agent.get("tools"):
            line += "\n  tools: " + ", ".join(agent["tools"])
        if agent.get("mcp_servers"):
            line += "\n  mcp_servers: " + ", ".join(
                item.get("name", "") if isinstance(item, dict) else str(item)
                for item in agent["mcp_servers"]
            )
    if item.get("instructions"):
        instructions = str(item["instructions"]).strip()
        line += "\n  instructions:\n" + "\n".join(f"    {part}" for part in instructions.splitlines())
    if item.get("document_refs"):
        line += "\n  document_refs: " + ", ".join(
            ref["ref"] for ref in item["document_refs"]
        )
    if item.get("agent_state") or item.get("agent_message"):
        line += "\n  working: " + " — ".join(
            value for value in (item.get("agent_state"), item.get("agent_message")) if value
        )
    if item.get("runs"):
        sessions = [run["session_id"] for run in item["runs"] if run.get("session_id")]
        line += f"\n  runs: {len(item['runs'])}"
        if sessions:
            line += "; sessions=" + ", ".join(sessions)
    if item["files"]:
        line += "\n  files: " + ", ".join(item["files"])
    return line


def _print_tree(nodes, children):
    by_id = {uuid.UUID(item["id"]).hex: item for item in nodes}
    roots = [item for item in nodes if not item["parent_id"]]

    def show(item, depth):
        print("  " * depth + "- " + _summary(item))
        for child_id in children.get(uuid.UUID(item["id"]).hex, []):
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
            print("- " + _summary(item))
    else:
        _print_tree(nodes, children)
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
