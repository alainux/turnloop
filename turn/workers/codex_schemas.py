"""JSON Schemas handed to `codex exec --output-schema`.

When Codex is given an output schema it constrains its *final* response to match,
so we get reliable structured output instead of hoping the model emits a fenced
block. These are the two shapes Turn needs.

IMPORTANT: Codex uses OpenAI's *strict* JSON-schema mode, which requires every
object schema to set ``"additionalProperties": false`` AND to list **every**
property key in ``required``. Both constraints are enforced below.
"""
from __future__ import annotations

import json
import tempfile
from typing import Any

# --- reusable sub-schemas -------------------------------------------------

_INPUT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "label": {"type": "string"},
        "kind": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["id", "label", "kind", "description"],
}

_NODE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "key": {"type": "string"},
        "objective": {"type": "string"},
        "generated_prompt": {"type": "string"},
        "executor": {"type": "string"},
        "required_inputs": {"type": "array", "items": _INPUT},
        "resource_refs": {"type": "array", "items": {"type": "string"}},
        "parent_key": {"type": ["string", "null"]},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "key",
        "objective",
        "generated_prompt",
        "executor",
        "required_inputs",
        "resource_refs",
        "parent_key",
        "depends_on",
    ],
}

_EDGE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {"type": "string", "enum": ["CONTAINS", "DEPENDS_ON"]},
        "src": {"type": "string"},
        "dst": {"type": "string"},
    },
    "required": ["type", "src", "dst"],
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "nodes": {"type": "array", "items": _NODE},
        "edges": {"type": "array", "items": _EDGE},
        "notes": {"type": "string"},
    },
    "required": ["nodes", "edges", "notes"],
}

_RESULT_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string"},
        "name": {"type": "string"},
        "content": {"type": "string"},
        "ref": {"type": "string"},
    },
    "required": ["kind", "name", "content", "ref"],
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
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
        "outcome",
        "summary",
        "artifacts",
        "missing_inputs",
        "error",
        "retry_recommended",
        "children",
    ],
}


def write_schema(schema: dict[str, Any]) -> str:
    """Write a schema to a temp file and return its path (caller must unlink)."""
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(schema, f)
    f.close()
    return f.name
