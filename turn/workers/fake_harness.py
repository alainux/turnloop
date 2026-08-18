"""Process-level fake harness used only by test-mode E2E runs.

Unlike Echo, this adapter launches a real repository-owned shell process
through the configured terminal transport. The process emits terminal output
and completes by writing the same atomic handoff files that native harnesses
use. It is deliberately registered only when the server is in test mode.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from turn.config import Settings, settings
from turn.contracts.dag import parse_result, parse_verification
from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, PlanResult, WorkerResult
from turn.workers.base import NodeExecutionContext, Planner, Worker, render_context_block
from turn.workers.interactive import (
    agent_environment,
    prepare_result_file,
    read_result_file,
    run_until_result,
)
from turn.capabilities.catalog import CapabilityCatalog
from turn.workers.terminal import LocalPtyTransport


def fake_harness_script() -> str:
    return str(Path(__file__).resolve().parents[1] / "demo" / "fake_turn_harness.sh")


async def _launch(
    ctx: NodeExecutionContext,
    *,
    kind: str,
    prompt: str,
    runtime: Settings,
) -> dict[str, Any]:
    if not ctx.repo_path or not Path(ctx.repo_path).is_dir():
        raise RuntimeError("assigned project directory is unavailable")
    result_path = prepare_result_file(ctx.repo_path, ctx.node.id, kind)
    prompt_path = result_path.with_suffix(".prompt")
    prompt_path.write_text(prompt, encoding="utf-8")
    control_environment = agent_environment(
        ctx.repo_path,
        ctx.node.id,
        kind,
        result_path,
        ctx.node.agent,
        data_dir=runtime.data_dir,
    )
    session_id = _session_for(ctx)
    # This fixture deliberately keeps the injected shell command short. The
    # real harness adapters need the full capability environment, while this
    # process only consumes the control-plane handoff and prompt metadata.
    environment = {
        key: control_environment[key]
        for key in (
            "TURN_HANDOFF_KIND",
            "TURN_HANDOFF_FILE",
        )
    }
    environment.update({
        "TURN_PROJECT_ID": str(ctx.node.project_id),
        "TURN_AGENT_SESSION_ID": session_id,
        "TURN_INITIAL_PROMPT_FILE": str(prompt_path),
        "TURN_FAKE_ATTEMPT": str(ctx.attempt),
    })
    for resource in ctx.resources:
        if Path(resource.ref).name == "fake-plan.json":
            environment["TURN_FAKE_PLAN_FILE"] = resource.ref
            break
    transport = ctx.terminal or LocalPtyTransport()
    try:
        await run_until_result(
            transport,
            ctx.node.id,
            [fake_harness_script()],
            cwd=ctx.repo_path,
            result_path=result_path,
            stream=ctx.stream,
            timeout=ctx.timeout_seconds or runtime.default_run_timeout_seconds,
            idle_warning=runtime.terminal_idle_warning_seconds,
            idle_reap=runtime.terminal_idle_reap_seconds,
            harness_name=fake_harness_script(),
            environment=environment,
        )
        submitted = read_result_file(result_path)
        if submitted is None:
            raise RuntimeError("fake harness exited without a handoff")
        return submitted
    finally:
        result_path.unlink(missing_ok=True)
        prompt_path.unlink(missing_ok=True)


async def _remember_session(ctx: NodeExecutionContext, session_id: str) -> None:
    """Expose the fixture's retained conversation through Turn's normal port."""
    if ctx.session_callback is not None:
        await ctx.session_callback(session_id)


def _session_for(ctx: NodeExecutionContext) -> str:
    """Return the current fake conversation, or allocate a fresh one."""
    agent = ctx.node.agent
    if agent is None:
        agent = AgentConfig(harness=HarnessKind.FAKE)
        ctx.node.agent = agent
    session_id = agent.session_id
    if not session_id or session_id == ctx.forbidden_session_id:
        session_id = f"fake-{uuid.uuid4().hex}"
        agent.session_id = session_id
    return session_id


class FakeHarnessPlanner(Planner):
    name = "fake-planner"

    def __init__(self, runtime: Settings = settings):
        self.runtime = runtime

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        # Mirror the real planner's project-local capability loading. The
        # served test-mode authoring flow creates a fresh project directory,
        # so validation must see the same loaded contract as a seeded lab
        # project before accepting the submitted plan.
        if ctx.repo_path:
            catalog = CapabilityCatalog(Path(self.runtime.data_dir) / "capabilities")
            for entry in catalog.list():
                catalog.load_into_project(entry.id, ctx.repo_path)
        session_id = _session_for(ctx)
        await _remember_session(ctx, session_id)
        payload = await _launch(
            ctx,
            kind="plan",
            prompt=f"{ctx.node.generated_prompt or ctx.node.objective}\n{render_context_block(ctx)}",
            runtime=self.runtime,
        )
        plan = PlanResult.model_validate(payload)
        plan.session_id = session_id
        return plan


class FakeHarnessWorker(Worker):
    name = "fake"

    def __init__(self, runtime: Settings = settings):
        self.runtime = runtime

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        session_id = _session_for(ctx)
        await _remember_session(ctx, session_id)
        verification = bool(
            ctx.node.agent and ctx.node.agent.type_id is AgentType.VERIFIER
        )
        kind = "verification" if verification else "result"
        payload = await _launch(
            ctx,
            kind=kind,
            prompt=f"{ctx.node.generated_prompt or ctx.node.objective}\n{render_context_block(ctx)}",
            runtime=self.runtime,
        )
        if verification:
            decision = parse_verification(payload)
            return WorkerResult(
                outcome="COMPLETE",
                summary=decision.summary,
                verification=decision,
                session_id=session_id,
            )
        result = parse_result(json.dumps(payload))
        result.session_id = session_id
        return result
