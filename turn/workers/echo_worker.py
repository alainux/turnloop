"""Echo worker — deterministic, used for tests and trivial nodes.

If `generated_prompt` is a JSON directive it is obeyed literally; otherwise the
worker echoes the objective as a COMPLETE result. This makes it a precise test
double for exercising all four outcomes without external services.
"""
from __future__ import annotations

import json

from turn.domain.schemas import (
    ArtifactKind,
    ArtifactSpec,
    InputKind,
    InputSpec,
    Outcome,
    VerificationResult,
    WorkerResult,
)
from turn.workers.base import NodeExecutionContext, Worker
from turn.workers import parsing


def _as_input(d: dict) -> InputSpec:
    return InputSpec(
        id=d["id"],
        label=d.get("label", d["id"]),
        kind=parsing.safe_input_kind(d.get("kind")),
        description=d.get("description"),
    )


class EchoWorker(Worker):
    name = "echo"

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        directive = self._parse_directive(ctx)
        if directive is not None:
            return directive

        # default: COMPLETE, echoing the objective and any supplied inputs
        summary = ctx.node.objective
        provided = [i for i in ctx.node.required_inputs if i.satisfied_by]
        if provided:
            summary += "\nSupplied inputs: " + "; ".join(
                f"{i.label}" for i in provided
            )
        return WorkerResult(
            outcome=Outcome.COMPLETE,
            summary=summary,
            artifacts=[
                ArtifactSpec(kind=ArtifactKind.TEXT, name="echo", content=summary)
            ],
        )

    @staticmethod
    def _parse_directive(ctx: NodeExecutionContext) -> WorkerResult | None:
        gp = ctx.node.generated_prompt
        if not gp:
            return None
        try:
            data = json.loads(gp)
        except (json.JSONDecodeError, ValueError):
            return None
        outcome = data.get("outcome")
        if outcome is None:
            return None
        return WorkerResult(
            outcome=Outcome(outcome),
            summary=data.get("summary", ""),
            artifacts=[
                ArtifactSpec(
                    kind=ArtifactKind(a.get("kind", "text")),
                    name=a["name"],
                    content=a.get("content"),
                    ref=a.get("ref"),
                )
                for a in data.get("artifacts", [])
            ],
            missing_inputs=[
                _as_input(i) for i in data.get("missing_inputs", [])
            ],
            error=data.get("error"),
            retry_recommended=bool(data.get("retry_recommended", False)),
            children=None,
            verification=(
                VerificationResult.model_validate(data["verification"])
                if data.get("verification") is not None
                else None
            ),
        )
