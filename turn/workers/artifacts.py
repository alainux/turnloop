"""Capture durable evidence from any harness worktree."""
from __future__ import annotations

import re
import subprocess
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
        or (spec.kind == ArtifactKind.EVIDENCE and spec.name == "git-status" and bool(spec.content))
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
            # this worktree, so existence enforcement does not apply.
            continue
        if not target.is_file():
            missing.append(str(candidate))
    return missing


def capture_worktree(path: str) -> list[ArtifactSpec]:
    """Describe changed files before a worker branch is committed and merged."""

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args], cwd=path, capture_output=True, text=True
            ).stdout
        except OSError:
            return ""

    artifacts: list[ArtifactSpec] = []
    diff = git("diff", "HEAD")
    status = git("status", "--porcelain")
    if diff.strip():
        artifacts.append(
            ArtifactSpec(kind=ArtifactKind.CODE_DIFF, name="git-diff", content=diff)
        )
    if status.strip():
        artifacts.append(
            ArtifactSpec(kind=ArtifactKind.EVIDENCE, name="git-status", content=status)
        )
        seen: set[str] = set()
        for line in status.splitlines():
            relative = line[3:].strip().strip('"')
            if " -> " in relative:
                relative = relative.split(" -> ", 1)[1]
            if not relative or relative in seen:
                continue
            seen.add(relative)
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
            name="worktree-path",
            content=path,
            ref=path,
        )
    )
    return artifacts
