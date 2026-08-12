"""Robust extraction of structured output from Codex's free-form responses.

Codex is instructed to emit fenced blocks (```turn-result / ```turn-plan) but in
practice it may add prose, vary whitespace, or skip the fence entirely. These
helpers tolerate that: they pull fenced blocks first, then fall back to the first
balanced JSON object in the text.
"""
from __future__ import annotations

import json
import re

from turn.domain.schemas import ArtifactKind, InputKind

_FENCE_RE = re.compile(r"```(\w+)[ \t]*\n(.*?)```", re.DOTALL)


def extract_fences(text: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2).strip() for m in _FENCE_RE.finditer(text)}


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
    val = extract_json(text)
    if isinstance(val, dict) and "nodes" in val:
        return val
    return None


def first_result_json(text: str):
    val = extract_json(text)
    if isinstance(val, dict) and "outcome" in val:
        return val
    return None
