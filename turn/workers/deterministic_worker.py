"""Deterministic worker and planner used by isolated unit tests.

This adapter is intentionally not a selectable harness and never appears in
the served capabilities catalog. Process-level scenarios use the Mock
harness; this small in-process double keeps pure state-machine tests fast and
does not represent a second user-facing provider.

If ``generated_prompt`` is a JSON directive it is obeyed literally; otherwise
the worker returns a COMPLETE result. Directives may provide a ``sequence``
for successive runs and a ``delay_ms`` for cancellation/stop coverage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from turn.domain.schemas import (
    ArtifactKind,
    ArtifactSpec,
    InputSpec,
    Outcome,
    PlanResult,
    VerificationResult,
    WorkerResult,
)
from turn.domain.organization import AcceptanceEvidence, EvidenceStatus
from turn.workers.base import NodeExecutionContext, Planner, Worker
from turn.workers import parsing


def _as_input(d: dict) -> InputSpec:
    return InputSpec(
        id=d["id"],
        label=d.get("label", d["id"]),
        kind=parsing.safe_input_kind(d.get("kind")),
        description=d.get("description"),
    )


class DeterministicWorker(Worker):
    name = "deterministic"

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        data = self._directive_payload(ctx)
        if data is not None:
            delay_ms = data.get("delay_ms", 0)
            if delay_ms:
                await asyncio.sleep(float(delay_ms) / 1000)
            return self._result_from_directive(data)

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
                ArtifactSpec(
                    kind=ArtifactKind.TEXT,
                    name="deterministic",
                    content=summary,
                )
            ],
            evidence=[
                AcceptanceEvidence(
                    criterion_id=criterion.id,
                    status=EvidenceStatus.PASS,
                    summary="deterministic worker completed the declared criterion",
                    refs=["deterministic"],
                )
                for criterion in ctx.node.acceptance_criteria
            ],
        )

    @staticmethod
    def _directive_payload(ctx: NodeExecutionContext) -> dict | None:
        generated_prompt = ctx.node.generated_prompt
        if not generated_prompt:
            return None
        try:
            data = json.loads(generated_prompt)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        sequence = data.get("sequence")
        if sequence is not None:
            if not isinstance(sequence, list) or not sequence:
                raise ValueError(
                    "Deterministic sequence must contain at least one directive"
                )
            selected = sequence[min(max(ctx.attempt - 1, 0), len(sequence) - 1)]
            if not isinstance(selected, dict):
                raise ValueError("Deterministic sequence entries must be objects")
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
            missing_inputs=[_as_input(i) for i in data.get("missing_inputs", [])],
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
            evidence=[
                AcceptanceEvidence.model_validate(item)
                for item in data.get("evidence", [])
            ],
            outputs={str(k): str(v) for k, v in (data.get("outputs") or {}).items()},
            route=data.get("route"),
        )


class DeterministicPlanner(Planner):
    """Planner for local unit-test fixtures stored in ``deterministic-plan``."""

    name = "deterministic-planner"

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        payload = None
        for resource in ctx.resources:
            if Path(resource.ref).name == "deterministic-plan.json" and resource.content:
                payload = json.loads(resource.content)
                break
        if payload is None:
            try:
                payload = json.loads(ctx.node.generated_prompt or "")
            except (json.JSONDecodeError, ValueError) as error:
                raise RuntimeError(
                    "Deterministic planner requires a deterministic-plan.json resource"
                ) from error
        if not isinstance(payload, dict):
            raise RuntimeError("Deterministic planner plan payload must be an object")
        return PlanResult.model_validate(payload)
