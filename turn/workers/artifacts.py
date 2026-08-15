"""Capture durable evidence from a worker's assigned project directory."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from turn.domain.schemas import ArtifactKind, ArtifactSpec


_MATERIAL_ACTION = re.compile(
    r"\b(write|create|implement|build|edit|modify|patch|fix|assemble|integrate|combine|stitch)\b",
    re.IGNORECASE,
)


def requires_material_change(objective: str, generated_prompt: str | None) -> bool:
    """Whether a successful coding objective promises a filesystem change."""
    text = f"{objective}\n{generated_prompt or ''}"
    if re.search(r"\b(only )?(inspect|review|verify|audit)\b", text, re.IGNORECASE) and not _MATERIAL_ACTION.search(text):
        return False
    return bool(_MATERIAL_ACTION.search(text))


def has_material_change(specs: list[ArtifactSpec]) -> bool:
    return any(
        spec.kind == ArtifactKind.CODE_DIFF
        or (spec.kind == ArtifactKind.EVIDENCE and spec.name == "filesystem-status" and bool(spec.content))
        for spec in specs
    )


def missing_declared_files(specs: list[ArtifactSpec], path: str) -> list[str]:
    """Return relative file outputs an agent claimed but did not materialize."""
    root = Path(path).resolve()
    missing: list[str] = []
    for spec in specs:
        if spec.kind != ArtifactKind.FILE:
            continue
        candidate = spec.ref or spec.name
        target = Path(candidate)
        if not target.is_absolute():
            target = root / target
        try:
            target.resolve().relative_to(root)
        except ValueError:
            # External absolute references are evidence, not outputs owned by
            # this project directory, so existence enforcement does not apply.
            continue
        if not target.is_file():
            missing.append(str(candidate))
    return missing


def snapshot_filesystem(path: str) -> dict[str, str]:
    """Return text-file contents for the project, excluding Turn metadata."""
    root = Path(path).resolve()
    snapshot: dict[str, str] = {}
    if not root.is_dir():
        return snapshot
    for target in root.rglob("*"):
        if not target.is_file():
            continue
        relative = target.relative_to(root)
        if any(part in {".turn", ".git", "node_modules", "__pycache__"} for part in relative.parts):
            continue
        try:
            data = target.read_bytes()
            if b"\x00" in data or len(data) > 2_000_000:
                continue
            snapshot[str(relative)] = data.decode("utf-8", errors="replace")
        except OSError:
            continue
    return snapshot


def capture_filesystem(path: str, before: dict[str, str] | None = None) -> list[ArtifactSpec]:
    """Describe files changed by a worker without relying on version control."""
    before = before or {}
    after = snapshot_filesystem(path)
    changed = sorted(set(before) | set(after))
    changed = [relative for relative in changed if before.get(relative) != after.get(relative)]
    artifacts: list[ArtifactSpec] = []
    status_lines = []
    diff_parts: list[str] = []
    for relative in changed:
        old = before.get(relative, "").splitlines(keepends=True)
        new = after.get(relative, "").splitlines(keepends=True)
        status_lines.append(("A" if relative not in before else "D" if relative not in after else "M") + " " + relative)
        diff_parts.extend(
            difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}")
        )
    status = "\n".join(status_lines)
    diff = "".join(diff_parts)[:2_000_000]
    if diff.strip():
        artifacts.append(
            ArtifactSpec(kind=ArtifactKind.CODE_DIFF, name="filesystem-diff", content=diff)
        )
    if status.strip():
        artifacts.append(
            ArtifactSpec(kind=ArtifactKind.EVIDENCE, name="filesystem-status", content=status)
        )
        for relative in changed:
            target = Path(path, relative)
            if target.is_file():
                artifacts.append(
                    ArtifactSpec(
                        kind=ArtifactKind.FILE,
                        name=relative,
                        ref=str(target.resolve()),
                    )
                )
    artifacts.append(
        ArtifactSpec(
            kind=ArtifactKind.FILE,
            name="filesystem-path",
            content=path,
            ref=path,
        )
    )
    return artifacts
