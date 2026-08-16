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
    return PlanResult.model_validate(_decode(value))


def parse_result(value: str | dict[str, Any]) -> WorkerResult:
    return WorkerResult.model_validate(_normalize_result_payload(_decode(value)))


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

    A malformed nested architecture document can otherwise produce hundreds
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
    optional nesting). Dependencies belong in node ``depends_on``; omitting
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
            "architecture_spec": {
                "title": "Architecture title",
                "executive_summary": "What will be built and why",
                "approach": "How the work is shaped",
                "strategy": "How delivery and risk are managed",
                "filesystem_structure": "src/\n  feature/\ntests/",
                "research_sources": ["https://example.com/relevant-guidance"],
                "sections": [
                    {
                        "id": "overview",
                        "title": "Overview",
                        "content": "Markdown text only.",
                        "subsections": [],
                    }
                ],
            },
        },
        separators=(",", ":"),
    )


def _normalize_result_payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    raw_artifacts = payload.get("artifacts")
    if raw_artifacts is None:
        return payload
    if not isinstance(raw_artifacts, list):
        return payload

    artifacts: list[Any] = []
    for item in raw_artifacts:
        if isinstance(item, str):
            artifacts.append({
                "kind": "file",
                "name": item.rsplit("/", 1)[-1] or item,
                "ref": item,
            })
            continue
        if isinstance(item, dict):
            artifacts.append(dict(item))
            continue
        artifacts.append(item)
    payload["artifacts"] = artifacts
    return payload


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
