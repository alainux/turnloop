"""Codex worker — the software-engineering / general agent worker.

Shells out to `codex exec`, isolates software work in a Git worktree, and parses
the agent's structured result. This adapter is the *only* place Codex concepts
enter Turn; the data model stays clean.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from turn.config import settings
from turn.domain.schemas import (
    ArtifactKind,
    ArtifactSpec,
    EdgeType,
    EdgeSpec,
    InputKind,
    InputSpec,
    NodeSpec,
    Outcome,
    PlanResult,
    WorkerResult,
)
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers import codex_schemas
from turn.workers import parsing


class CodexWorker(Worker):
    name = "codex"

    def __init__(self, settings=settings):
        self.s = settings

    # -- public ----------------------------------------------------------

    async def execute(self, ctx: "NodeExecutionContext") -> WorkerResult:
        worktree = self._prepare_worktree(ctx.node.id) if self._repo_is_git() else None
        # Safety: with a repo configured we MUST run in an isolated worktree.
        # Refusing here is far better than letting Codex rewrite the main repo.
        if self.s.repo_path and worktree is None:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="could not create an isolated git worktree; refused to run Codex in the main repository",
                retry_recommended=False,
            )
        cwd = worktree or os.getcwd()
        # Run inside the isolated worktree, and keep Codex pointed AT that
        # worktree (never the main repo) so it cannot rewrite files outside
        # the isolation boundary.
        prompt = self._build_prompt(ctx, cwd=cwd)

        schema_path = codex_schemas.write_schema(codex_schemas.RESULT_SCHEMA)

        # Full-permission mode: when the caller passes the bypass flag we must
        # NOT also pass the weaker sandbox/approve flags (they conflict).
        bypass = any("bypass" in a for a in self.s.codex_args)
        sandbox_flags = [] if bypass else ["-s", "workspace-write", "--approve-for-me"]

        model_flags = ["-m", self.s.codex_model] if self.s.codex_model else []
        cmd = [
            self.s.codex_binary,
            "exec",
            *model_flags,
            *sandbox_flags,
            "--ephemeral",
            "--output-schema",
            schema_path,
            "-C",
            cwd,
            *self.s.codex_args,
            prompt,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )

            out_buf: list[bytes] = []
            err_buf: list[bytes] = []

            async def _pump(stream, buf):
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    buf.append(chunk)
                    if ctx.stream is not None and stream is proc.stderr:
                        # stream the human-readable (stderr) side live; stdout is
                        # the JSON result and is surfaced at the end.
                        try:
                            await ctx.stream(
                                ctx.node.id,
                                parsing.strip_ansi(chunk.decode(errors="replace")),
                            )
                        except Exception:
                            pass

            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        _pump(proc.stdout, out_buf),
                        _pump(proc.stderr, err_buf),
                    ),
                    timeout=self.s.default_run_timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary="codex timed out",
                    error="execution exceeded run timeout",
                    retry_recommended=False,
                )
            await proc.wait()
        except asyncio.CancelledError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        except FileNotFoundError:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="codex not found",
                error=f"codex binary '{self.s.codex_binary}' is not available",
                retry_recommended=False,
            )

        text = (b"".join(out_buf) or b"").decode(errors="replace")
        stderr_text = (b"".join(err_buf) or b"").decode(errors="replace")
        try:
            os.unlink(schema_path)
        except OSError:
            pass

        result = self._parse_result(text)
        if worktree:
            result.artifacts.extend(self._capture_worktree(worktree))
        # One-way mirror of the unaltered Codex output for the node-detail
        # terminal pane. TODO(real-pty): replace with a real bidirectional PTY.
        transcript = parsing.strip_ansi(stderr_text + "\n" + text)
        if transcript.strip():
            result.artifacts.append(
                ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=transcript)
            )
        else:
            result.artifacts.append(
                ArtifactSpec(kind=ArtifactKind.TEXT, name="codex-output", content=text[:8000])
            )
        return result

    # -- prompt ----------------------------------------------------------

    def _build_prompt(self, ctx: NodeExecutionContext, cwd=None) -> str:
        gp = ctx.node.generated_prompt or "Complete the objective above using the available tools."
        # If we execute inside an isolated worktree, rewrite any mention of the
        # main repository path so Codex operates on the worktree, not the source
        # tree it was cloned from.
        if cwd and self.s.repo_path and self.s.repo_path != cwd:
            gp = gp.replace(self.s.repo_path, cwd)
        return f"""{render_context_block(ctx)}
OBJECTIVE:
{ctx.node.objective}

TASK:
{gp}

When you finish, append a fenced code block labeled `turn-result` containing JSON:
{{"outcome": "COMPLETE"|"BLOCK"|"FAIL", "summary": "...", "missing_inputs": [{{"id":"...","label":"...","kind":"text|decision|credential|account|approval|file"}}]}}

If the task is too broad to complete directly, set "outcome": "EXPAND" and ALSO append a
separate fenced block labeled `turn-plan` containing JSON:
{{"nodes":[{{"key":"a","objective":"...","executor":"codex"}}], "edges":[]}}

Never invent missing information. If something required (input, account, credential, file,
decision, or approval) is missing, return outcome "BLOCK" with explicit missing_inputs.
"""

    # -- parsing ---------------------------------------------------------

    def _parse_result(self, text: str) -> WorkerResult:
        fences = parsing.extract_fences(text)
        result_json = parsing.first_result_json(text)
        plan_json = parsing.first_plan_json(text)

        if result_json is None and plan_json is not None:
            result_json = {"outcome": "EXPAND"}

        data = result_json or {}
        outcome = Outcome(data.get("outcome", "COMPLETE"))
        children = self._to_plan(plan_json) if (outcome == Outcome.EXPAND and plan_json) else None

        return WorkerResult(
            outcome=outcome,
            summary=data.get("summary", text[:500]),
            artifacts=[
                ArtifactSpec(
                    kind=parsing.safe_artifact_kind(a.get("kind")),
                    name=a["name"],
                    content=a.get("content"),
                    ref=a.get("ref"),
                )
                for a in data.get("artifacts", [])
            ],
            children=children,
            missing_inputs=[
                InputSpec(
                    id=i["id"],
                    label=i.get("label", i["id"]),
                    kind=parsing.safe_input_kind(i.get("kind")),
                    description=i.get("description"),
                )
                for i in data.get("missing_inputs", [])
            ],
            error=data.get("error"),
            retry_recommended=bool(data.get("retry_recommended", False)),
        )

    @staticmethod
    def _to_plan(plan_json: dict) -> PlanResult:
        nodes = [
            NodeSpec(
                key=n["key"],
                objective=n["objective"],
                generated_prompt=n.get("generated_prompt"),
                executor=n.get("executor"),
                required_inputs=[
                    InputSpec(
                        id=i["id"],
                        label=i.get("label", i["id"]),
                        kind=parsing.safe_input_kind(i.get("kind")),
                        description=i.get("description"),
                    )
                    for i in n.get("required_inputs", [])
                ],
                resource_refs=list(n.get("resource_refs", [])),
                parent_key=n.get("parent_key"),
                depends_on=list(n.get("depends_on", [])),
            )
            for n in plan_json.get("nodes", [])
        ]
        edges = [
            EdgeSpec(type=EdgeType(e["type"]), src=e["src"], dst=e["dst"])
            for e in plan_json.get("edges", [])
        ]
        return PlanResult(nodes=nodes, edges=edges, notes=plan_json.get("notes"))

    # -- git worktree ----------------------------------------------------

    def _repo_is_git(self) -> bool:
        if not self.s.repo_path:
            return False
        return (Path(self.s.repo_path) / ".git").exists()

    def _prepare_worktree(self, node_id) -> str | None:
        repo = Path(self.s.repo_path)
        branch = f"turn-{node_id.hex[:8]}"
        wt = repo / ".turn" / "worktrees" / node_id.hex
        # Clean up any prior worktree/branch for this node so re-runs isolate
        # cleanly instead of failing and falling back to the main repo.
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", branch],
            capture_output=True,
            text=True,
        )
        try:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", str(wt), "-b", branch],
                capture_output=True,
                text=True,
                check=True,
            )
            return str(wt)
        except (subprocess.CalledProcessError, OSError):
            # Never return the main repo: callers must refuse to run Codex here.
            return None

    def _capture_worktree(self, worktree: str) -> list[ArtifactSpec]:
        def run(args):
            try:
                return subprocess.run(
                    args, capture_output=True, text=True, cwd=worktree
                ).stdout
            except (subprocess.CalledProcessError, OSError):
                return ""

        arts: list[ArtifactSpec] = []
        diff = run(["git", "diff", "HEAD"])
        log = run(["git", "log", "--oneline", "-n", "20"])
        status = run(["git", "status", "--porcelain"])
        if diff.strip():
            arts.append(ArtifactSpec(kind=ArtifactKind.CODE_DIFF, name="git-diff", content=diff))
        if log.strip():
            arts.append(ArtifactSpec(kind=ArtifactKind.LOG, name="git-log", content=log))
        if status.strip():
            arts.append(
                ArtifactSpec(kind=ArtifactKind.EVIDENCE, name="git-status", content=status)
            )
        arts.append(
            ArtifactSpec(kind=ArtifactKind.FILE, name="worktree-path", content=worktree, ref=worktree)
        )
        return arts
