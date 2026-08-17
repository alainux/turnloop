"""Echo worker and planner — deterministic, used for tests and demos.

If `generated_prompt` is a JSON directive it is obeyed literally; otherwise the
worker echoes the objective as a COMPLETE result. This makes it a precise test
double for exercising all four outcomes without external services. Directives
may provide a ``sequence`` for successive runs and a ``delay_ms`` for
cancellation/stop coverage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from turn.domain.schemas import (
    ArtifactKind,
    ArtifactSpec,
    InputKind,
    InputSpec,
    Outcome,
    PlanResult,
    VerificationResult,
    WorkerResult,
)
from turn.workers.base import NodeExecutionContext, Planner, Worker
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
        data = self._directive_payload(ctx)
        if data is not None:
            delay_ms = data.get("delay_ms", 0)
            if delay_ms:
                await asyncio.sleep(float(delay_ms) / 1000)
            return self._result_from_directive(data)

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
    def _directive_payload(ctx: NodeExecutionContext) -> dict | None:
        gp = ctx.node.generated_prompt
        if not gp:
            return None
        try:
            data = json.loads(gp)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        sequence = data.get("sequence")
        if sequence is not None:
            if not isinstance(sequence, list) or not sequence:
                raise ValueError("Echo sequence must contain at least one directive")
            selected = sequence[min(max(ctx.attempt - 1, 0), len(sequence) - 1)]
            if not isinstance(selected, dict):
                raise ValueError("Echo sequence entries must be objects")
            data = {**data, **selected}
        return data if data.get("outcome") is not None else None

    @staticmethod
    def _result_from_directive(data: dict) -> WorkerResult:
        return WorkerResult(
            outcome=Outcome(data["outcome"]),
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
            children=(
                PlanResult.model_validate(data["children"])
                if data.get("children") is not None
                else None
            ),
            verification=(
                VerificationResult.model_validate(data["verification"])
                if data.get("verification") is not None
                else None
            ),
        )


class EchoPlanner(Planner):
    """Deterministic planner for server-rendered Echo workflow fixtures.

    A fixture stores its plan in a project-local ``echo-plan.json`` resource.
    Keeping the plan in the project means reruns use the same graph contract
    without embedding a scenario registry in the runner.
    """

    name = "echo-planner"

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        payload = None
        for resource in ctx.resources:
            if Path(resource.ref).name == "echo-plan.json" and resource.content:
                payload = json.loads(resource.content)
                break
        if payload is None:
            try:
                payload = json.loads(ctx.node.generated_prompt or "")
            except (json.JSONDecodeError, ValueError) as error:
                raise RuntimeError("Echo planner requires an echo-plan.json resource") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Echo planner plan payload must be an object")
        return PlanResult.model_validate(payload)
