"""Generate the browser's TypeScript contract from the server domain schema.

The Pydantic models in ``turn.domain.schemas`` are the only contract source.
This generator intentionally targets serialized response objects: fields with
server defaults are still required in the generated client types because the
server includes them in ``model_dump`` responses.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "ui" / "src" / "generated" / "domain.ts"
sys.path.insert(0, str(ROOT))


def _ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    return ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _type_for(schema: dict[str, Any]) -> str:
    reference = _ref_name(schema)
    if reference:
        return reference
    if "const" in schema:
        return _literal(schema["const"])
    if "enum" in schema:
        values = schema["enum"]
        return " | ".join(_literal(value) for value in values) or "never"
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        types = [_type_for(option) for option in alternatives]
        unique: list[str] = []
        for type_name in types:
            if type_name not in unique:
                unique.append(type_name)
        return " | ".join(unique)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return " | ".join(_type_for({"type": item}) for item in schema_type)
    if schema_type == "string":
        return "string"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "null":
        return "null"
    if schema_type == "array":
        return f"Array<{_type_for(schema.get('items', {}))}>"
    if schema_type == "object":
        additional = schema.get("additionalProperties")
        if additional:
            return f"Record<string, {_type_for(additional)}>"
        properties = schema.get("properties")
        if properties:
            fields = "; ".join(
                f"{name}: {_type_for(value)}" for name, value in properties.items()
            )
            return "{ " + fields + " }"
    return "unknown"


def _collect_definitions(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions: dict[str, dict[str, Any]] = {}
    for schema in document["models"].values():
        for name, definition in schema.get("$defs", {}).items():
            definitions.setdefault(name, definition)
    return definitions


def _interface(name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties", {})
    lines = [f"export interface {name} {{"]
    for property_name, property_schema in properties.items():
        lines.append(f"  {property_name}: {_type_for(property_schema)};")
    lines.append("}")
    return "\n".join(lines)


def _declaration(name: str, schema: dict[str, Any]) -> str:
    if "enum" in schema:
        return f"export type {name} = {_type_for(schema)};"
    if schema.get("type") == "object" or "properties" in schema:
        return _interface(name, schema)
    return f"export type {name} = {_type_for(schema)};"


def generate(document: dict[str, Any] | None = None) -> str:
    if document is None:
        from turn.contracts.schema import public_schema

        document = public_schema()
    definitions = _collect_definitions(document)
    declarations: dict[str, str] = {}
    for name, schema in definitions.items():
        declarations[name] = _declaration(name, schema)
    for name, schema in document["models"].items():
        # Pydantic emits recursive models as a top-level $ref plus their
        # concrete object in $defs. Do not replace that concrete declaration
        # with the self-referential alias `type Section = Section`.
        if _ref_name(schema) == name and name in definitions:
            continue
        declarations[name] = _declaration(name, schema)

    sections = [
        "/* GENERATED FILE. Source: turn.contracts.schema.public_schema. Do not edit. */",
        *[declarations[name] for name in sorted(declarations)],
    ]
    return "\n\n".join(sections) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    generated = generate()
    current = args.output.read_text() if args.output.exists() else None
    if args.check:
        if current != generated:
            print(f"generated contract is stale: {args.output}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
