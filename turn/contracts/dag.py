"""Canonical DAG JSON schemas and boundary codecs.

The domain models remain the source of truth for validated values. These JSON
schemas are the wire contract given to external harnesses; parsing belongs at
this boundary so planners and workers do not each invent their own coercion.
"""
from __future__ import annotations

import json
import tempfile
from typing import Any

from turn.domain.schemas import PlanResult, WorkerResult

_INPUT = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "id": {"type": "string"}, "label": {"type": "string"},
        "kind": {"type": "string"}, "description": {"type": "string"},
    },
    "required": ["id", "label", "kind", "description"],
}
_NODE = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "key": {"type": "string"}, "objective": {"type": "string"},
        "generated_prompt": {"type": "string"}, "executor": {"type": "string"},
        "required_inputs": {"type": "array", "items": _INPUT},
        "resource_refs": {"type": "array", "items": {"type": "string"}},
        "parent_key": {"type": ["string", "null"]},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "plan": {"type": "boolean"},
    },
    "required": [
        "key", "objective", "generated_prompt", "executor", "required_inputs",
        "resource_refs", "parent_key", "depends_on", "plan",
    ],
}
_EDGE = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": ["CONTAINS", "DEPENDS_ON"]},
        "src": {"type": "string"}, "dst": {"type": "string"},
    },
    "required": ["type", "src", "dst"],
}
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "nodes": {"type": "array", "items": _NODE},
        "edges": {"type": "array", "items": _EDGE},
        "notes": {"type": "string"},
    },
    "required": ["nodes", "edges", "notes"],
}
_RESULT_ITEM = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string"}, "name": {"type": "string"},
        "content": {"type": "string"}, "ref": {"type": "string"},
    },
    "required": ["kind", "name", "content", "ref"],
}
RESULT_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "outcome": {"type": "string", "enum": ["COMPLETE", "EXPAND", "BLOCK", "FAIL"]},
        "summary": {"type": "string"},
        "artifacts": {"type": "array", "items": _RESULT_ITEM},
        "missing_inputs": {"type": "array", "items": _INPUT},
        "error": {"type": "string"},
        "retry_recommended": {"type": "boolean"},
        "children": PLAN_SCHEMA,
    },
    "required": [
        "outcome", "summary", "artifacts", "missing_inputs", "error",
        "retry_recommended", "children",
    ],
}


def parse_plan(value: str | dict[str, Any]) -> PlanResult:
    return PlanResult.model_validate(_decode(value))


def parse_result(value: str | dict[str, Any]) -> WorkerResult:
    return WorkerResult.model_validate(_decode(value))


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
