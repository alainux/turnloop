"""Canonical DAG JSON schemas and boundary codecs.

The domain models remain the source of truth for validated values. These JSON
schemas are the wire contract given to external harnesses; parsing belongs at
this boundary so planners and workers do not each invent their own coercion.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Literal

from pydantic import ValidationError

from turn.domain.schemas import (
    NODE_OBJECTIVE_MAX_LENGTH,
    PlanResult,
    VerificationResult,
    WorkerResult,
    concise_node_title,
)
from turn.graph.logic import validate_single_workflow_leaf

# The domain models are the single source of truth. Artifact strings are the
# one intentionally compact wire form accepted by the agent CLI; they are
# normalized before the domain model validates the result.
PLAN_SCHEMA: dict[str, Any] = PlanResult.model_json_schema(ref_template="#/$defs/{model}")
RESULT_SCHEMA: dict[str, Any] = WorkerResult.model_json_schema(ref_template="#/$defs/{model}")
artifact_schema = RESULT_SCHEMA["$defs"]["ArtifactSpec"]
RESULT_SCHEMA["$defs"]["ArtifactSpec"] = {
    "oneOf": [
        {"type": "string"},
        artifact_schema,
    ]
}


def parse_plan(value: str | dict[str, Any]) -> PlanResult:
    return PlanResult.model_validate(_normalize_payload(_decode(value)))


def parse_result(value: str | dict[str, Any]) -> WorkerResult:
    return WorkerResult.model_validate(_normalize_payload(_decode(value)))


def parse_verification(value: str | dict[str, Any]) -> VerificationResult:
    return VerificationResult.model_validate(_decode(value))


def validate_agent_submission(
    kind: Literal["plan", "result", "verification"], value: dict[str, Any]
) -> PlanResult | WorkerResult | VerificationResult:
    """Validate one agent handoff against the shared operation contract.

    The CLI accepts the deliberately small artifact shorthand used in agent
    prompts (for example ``"src"``). It is normalized into an ``ArtifactSpec``
    before Pydantic validation. No path lookup, content check, or filesystem
    comparison happens here.
    """
    if kind == "plan":
        return parse_plan(value)
    if kind == "verification":
        return parse_verification(value)
    return parse_result(value)


def validate_subgraph_sources(
    plan: PlanResult,
    project_root: str | Path,
    *,
    max_depth: int = 32,
) -> None:
    """Validate every local source link without ingesting linked subgraphs.

    A linked file is parsed as an independent plan contract so malformed
    sources are rejected at submission time. Its nodes are intentionally not
    merged into the current graph; only the submitted boundary is applied.
    """
    root = Path(project_root).expanduser().resolve()
    visiting: set[Path] = set()

    def visit(current: PlanResult, source: Path | None, depth: int) -> None:
        if depth > max_depth:
            raise ValueError(f"subgraph reference depth exceeds {max_depth}")
        validate_single_workflow_leaf(current)
        references = [
            *current.subgraph_refs,
            *(reference for node in current.nodes for reference in node.subgraph_refs),
        ]
        for reference in references:
            raw = reference.ref
            if raw.startswith(("http://", "https://")):
                continue
            target = (root / raw.split("?", 1)[0].split("#", 1)[0]).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise ValueError(f"subgraph reference escapes project: {raw}") from error
            if not target.is_file():
                raise ValueError(f"subgraph source does not exist: {raw}")
            if target in visiting:
                continue
            visiting.add(target)
            try:
                try:
                    nested = parse_plan(json.loads(target.read_text(encoding="utf-8")))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                    raise ValueError(f"invalid subgraph source {raw}: {error}") from error
                visit(nested, target, depth + 1)
            finally:
                visiting.remove(target)

    visit(plan, None, 0)


def compact_validation_error(error: ValidationError, *, limit: int = 8) -> str:
    """Render contract failures compactly enough for an agent to act on.

    A malformed nested document reference can otherwise produce hundreds
    of repeated Pydantic lines in the PTY. The full payload remains available
    to server-side logs; the agent only needs the first actionable fields to
    correct and resubmit.
    """
    issues = error.errors(include_url=False)
    rendered = []
    for issue in issues[:limit]:
        location = ".".join(str(part) for part in issue.get("loc", ())) or "payload"
        rendered.append(f"{location}: {issue.get('msg', 'invalid value')}")
    suffix = f"; ... {len(issues) - limit} more" if len(issues) > limit else ""
    return "; ".join(rendered) + suffix


def plan_handoff_example() -> str:
    """Return the one canonical, minimal plan payload shown to agents.

    Sections intentionally contain only a title and Markdown text (plus
    optional nesting). Substantial prose belongs in an ordinary project
    document submitted as an artifact by the worker that creates it; the
    structured index is deliberately small. Sequence predecessors belong in
    node ``follows``; omitting the redundant top-level ``edges`` field avoids
    a second enum-shaped way for planners to describe the same relationship.
    """
    return json.dumps(
        {
            "nodes": [
                {
                    "key": "unique",
                    "objective": "Short title",
                    "executor": "codex",
                    "agent_type": "executor",
                    "plan": False,
                    "generated_prompt": "Detailed instructions",
                    "follows": [],
                }
            ],
            "project_name": "Short project name",
        },
        separators=(",", ":"),
    )


def _normalize_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    if "subgraph_refs" not in payload:
        for alias in ("graph_refs", "graph_files", "graph_ref", "graph_file"):
            if alias in payload:
                payload["subgraph_refs"] = payload[alias]
                break
    for alias in ("graph_refs", "graph_files", "graph_ref", "graph_file"):
        payload.pop(alias, None)
    payload["subgraph_refs"] = _normalize_subgraph_refs(payload.get("subgraph_refs"))
    payload["artifacts"] = _normalize_artifacts(payload.get("artifacts"))
    payload["document_refs"] = _normalize_document_refs(payload.get("document_refs"))
    edges = payload.get("edges")
    if isinstance(edges, list):
        payload["edges"] = [
            {**edge, "type": edge.get("type", "FOLLOWS")}
            if isinstance(edge, dict) else edge
            for edge in edges
        ]
    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        normalized_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                normalized_nodes.append(node)
                continue
            item = dict(node)
            if "subgraph_refs" not in item:
                for alias in ("graph_refs", "graph_files", "graph_ref", "graph_file"):
                    if alias in item:
                        item["subgraph_refs"] = item[alias]
                        break
            for alias in ("graph_refs", "graph_files", "graph_ref", "graph_file"):
                item.pop(alias, None)
            item["document_refs"] = _normalize_document_refs(item.get("document_refs"))
            item["subgraph_refs"] = _normalize_subgraph_refs(item.get("subgraph_refs"))
            item["artifacts"] = _normalize_artifacts(item.get("artifacts"))
            objective = item.get("objective")
            if isinstance(objective, str) and len(objective) > NODE_OBJECTIVE_MAX_LENGTH:
                item["objective"] = concise_node_title(objective)
                if not item.get("generated_prompt"):
                    item["generated_prompt"] = objective
            normalized_nodes.append(item)
        payload["nodes"] = normalized_nodes
    children = payload.get("children")
    if isinstance(children, dict):
        payload["children"] = _normalize_payload(children)
    return payload


def _normalize_artifacts(raw: Any) -> Any:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw
    artifacts: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            artifacts.append({
                "kind": "file",
                "name": item.rsplit("/", 1)[-1] or item,
                "ref": item,
            })
        elif isinstance(item, dict):
            artifacts.append(dict(item))
        else:
            artifacts.append(item)
    return artifacts


def _normalize_document_refs(raw: Any) -> Any:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return raw
    refs: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            refs.append({"ref": item})
        elif isinstance(item, dict):
            value = dict(item)
            value["imports"] = _normalize_document_refs(value.get("imports")) or []
            refs.append(value)
        else:
            refs.append(item)
    return refs


def _normalize_subgraph_refs(raw: Any) -> Any:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [{"ref": raw}]
    if not isinstance(raw, list):
        return raw
    refs: list[Any] = []
    for item in raw:
        if isinstance(item, str):
            refs.append({"ref": item})
        elif isinstance(item, dict):
            refs.append(dict(item))
        else:
            refs.append(item)
    return refs


def _decode(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("DAG protocol payload must be a JSON object")
    return value


def write_schema(schema: dict[str, Any]) -> str:
    stream = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    try:
        json.dump(schema, stream)
    finally:
        stream.close()
    return stream.name
