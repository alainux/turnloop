"""Robust extraction of structured output from Codex's free-form responses.

Codex is instructed to emit fenced blocks (```turn-result / ```turn-plan) but in
practice it may add prose, vary whitespace, or skip the fence entirely. These
helpers tolerate that: they pull fenced blocks first, then fall back to the first
balanced JSON object in the text.
"""
from __future__ import annotations

import json
import re

from turn.domain.schemas import ArtifactKind, ArtifactSpec, InputKind

_FENCE_RE = re.compile(r"```([\w-]+)[ \t]*\n(.*?)```", re.DOTALL)


def extract_fences(text: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2).strip() for m in _FENCE_RE.finditer(text)}


def clean_summary(value: object) -> str:
    """Return the human summary without an echoed turn-result protocol block."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    marker = re.search(r"```turn-result\s*([\s\S]*?)```", text, re.IGNORECASE)
    if not marker:
        return text
    prefix = text[: marker.start()].strip()
    payload = _safe_json(marker.group(1).strip())
    if prefix:
        return prefix
    if isinstance(payload, dict) and isinstance(payload.get("summary"), str):
        return payload["summary"].strip()
    return text[marker.end() :].strip() or text


def _safe_json(s: str):
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def extract_json(text: str):
    """Return the first JSON value that parses, from a fence or bare in the text."""
    for content in extract_fences(text).values():
        val = _safe_json(content)
        if isinstance(val, (dict, list)):
            return val
    # fall back to the first balanced JSON object anywhere
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        val = _safe_json(text[start : i + 1])
                        if isinstance(val, (dict, list)):
                            return val
                        break
        start = text.find("{", start + 1)
    return None


def extract_json_objects(text: str) -> list[dict]:
    """Return every balanced JSON object embedded in ``text`` in order."""
    values: list[dict] = []
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        val = _safe_json(text[start : i + 1])
                        if isinstance(val, dict):
                            values.append(val)
                            start = text.find("{", i + 1)
                        else:
                            start = text.find("{", start + 1)
                        break
        else:
            break
    return values


def safe_input_kind(value: str) -> InputKind:
    """Coerce a free-form kind string to a valid InputKind (defaults to TEXT)."""
    try:
        return InputKind(value)
    except ValueError:
        return InputKind.TEXT


def safe_artifact_kind(value: str) -> ArtifactKind:
    try:
        return ArtifactKind(value)
    except ValueError:
        return ArtifactKind.TEXT


def artifact_specs(values) -> list[ArtifactSpec]:
    """Normalize the artifact shorthand commonly emitted by coding agents."""
    specs: list[ArtifactSpec] = []
    for value in values or []:
        if isinstance(value, str):
            specs.append(ArtifactSpec(kind=ArtifactKind.FILE, name=value.rsplit("/", 1)[-1], ref=value))
            continue
        if not isinstance(value, dict):
            continue
        ref = value.get("ref") or value.get("path")
        name = value.get("name") or (str(ref).rsplit("/", 1)[-1] if ref else "artifact")
        kind = safe_artifact_kind(value.get("kind") or ("file" if ref else "text"))
        specs.append(
            ArtifactSpec(
                kind=kind,
                name=name,
                content=value.get("content") or value.get("description"),
                ref=ref,
            )
        )
    return specs


def strip_ansi(s: str) -> str:
    """Remove ANSI/terminal escape sequences so raw Codex output reads cleanly."""
    return _ANSI_RE.sub("", s)


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"          # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1b[()][AB0]"                    # charset selectors
    r"|\x1b[=>]"                         # keypad / mode
)


def first_plan_json(text: str):
    fences = extract_fences(text)
    val = _safe_json(fences.get("turn-plan", ""))
    if _is_plan(val):
        return val
    # Accept only the planner identity (key/objective child specs), never
    # graph-inspector rows (id).
    for candidate in reversed(extract_json_objects(text)):
        if _is_plan(candidate):
            return candidate
    return None


def first_result_json(text: str):
    labeled = _safe_json(extract_fences(text).get("turn-result", ""))
    if isinstance(labeled, dict) and "outcome" in labeled:
        return labeled
    # Streaming harnesses can emit more than one schema-shaped progress
    # message. Only the final structured result is authoritative.
    for val in reversed(extract_json_objects(text)):
        if "outcome" in val:
            return val
    return None


def _is_plan(value) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("nodes"), list):
        return False
    return all(
        isinstance(node, dict)
        and isinstance(node.get("key"), str)
        and bool(node["key"].strip())
        and isinstance(node.get("objective"), str)
        and bool(node["objective"].strip())
        and "id" not in node
        for node in value["nodes"]
    )
