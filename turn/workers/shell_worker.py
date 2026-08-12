"""Shell worker — runs a shell command and returns its output as artifacts.

Useful for concrete, deterministic operations (run a test, build, query). The
command comes from the node's generated_prompt.
"""
from __future__ import annotations

import asyncio

from turn.domain.schemas import ArtifactKind, ArtifactSpec, Outcome, WorkerResult
from turn.workers.base import NodeExecutionContext, Worker


class ShellWorker(Worker):
    name = "shell"

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        command = ctx.node.generated_prompt
        if not command:
            supplied = [i for i in ctx.node.required_inputs if i.satisfied_by]
            command = supplied[0].label if supplied else None
        if not command:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="no command provided",
                error="shell worker requires a command in generated_prompt",
                retry_recommended=False,
            )
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=600
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        except asyncio.TimeoutError:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="command timed out",
                error="timeout",
                retry_recommended=False,
            )
        out = (stdout or b"").decode(errors="replace").strip()
        err = (stderr or b"").decode(errors="replace").strip()
        if proc.returncode != 0:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"command exited {proc.returncode}",
                error=err or out,
                retry_recommended=True,
                artifacts=[
                    ArtifactSpec(kind=ArtifactKind.TEXT, name="stdout", content=out),
                    ArtifactSpec(kind=ArtifactKind.TEXT, name="stderr", content=err),
                ],
            )
        return WorkerResult(
            outcome=Outcome.COMPLETE,
            summary=out[:2000] or "(no output)",
            artifacts=[
                ArtifactSpec(kind=ArtifactKind.TEXT, name="stdout", content=out),
                ArtifactSpec(kind=ArtifactKind.TEXT, name="stderr", content=err),
            ],
        )
