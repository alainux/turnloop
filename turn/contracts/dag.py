"""Canonical DAG JSON schemas and boundary codecs.

The domain models remain the source of truth for validated values. These JSON
schemas are the wire contract given to external harnesses; parsing belongs at
this boundary so planners and workers do not each invent their own coercion.
"""
from __future__ import annotations

import json
import tempfile
from typing import Any, Literal

from pydantic import ValidationError

from turn.domain.schemas import PlanResult, VerificationResult, WorkerResult

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
    document referenced by ``document_refs``; the structured index is
    deliberately small. Dependencies belong in node ``depends_on``; omitting
    the redundant top-level ``edges`` field avoids a second enum-shaped way
    for planners to describe the same relationship.
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
                    "depends_on": [],
                }
            ],
            "document_refs": ["docs/project-plan.md"],
            "artifacts": [{"kind": "file", "name": "project-plan.md", "ref": "docs/project-plan.md"}],
        },
        separators=(",", ":"),
    )


def _normalize_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload["artifacts"] = _normalize_artifacts(payload.get("artifacts"))
    payload["document_refs"] = _normalize_document_refs(payload.get("document_refs"))
    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        normalized_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                normalized_nodes.append(node)
                continue
            item = dict(node)
            item["document_refs"] = _normalize_document_refs(item.get("document_refs"))
            item["artifacts"] = _normalize_artifacts(item.get("artifacts"))
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
