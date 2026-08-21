"""Process-level mock harness used only by test-mode E2E runs.

Unlike Echo, this adapter launches a real repository-owned shell process
through the configured terminal transport. The process emits terminal output
and completes by writing the same atomic handoff files that native harnesses
use. It is deliberately registered only when the server is in test mode.
"""
from __future__ import annotations

import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Any

from turn.config import Settings, settings
from turn.contracts.dag import parse_result, parse_verification
from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, PlanResult, Usage, WorkerResult
from turn.workers.base import InvalidSubmission, NodeExecutionContext, Planner, Worker, render_context_block
from turn.workers.interactive import (
    agent_environment,
    read_result_file,
    read_submission_file,
    run_until_result,
)
from turn.capabilities.catalog import CapabilityCatalog
from turn.workers.terminal import LocalPtyTransport


def mock_harness_script() -> str:
    return str(Path(__file__).resolve().parents[1] / "demo" / "mock_turn_harness.sh")


def _write_launcher(
    path: Path,
    *,
    environment: dict[str, str],
    command: str,
    process_start_path: Path,
) -> None:
    """Write a short executable wrapper for injectable terminal transports."""
    lines = ["#!/bin/sh", "set -eu"]
    lines.extend(
        f"export {key}={shlex.quote(str(value))}"
        for key, value in environment.items()
        if value is not None
    )
    lines.append(f"printf '%s\\n' \"$$\" > {shlex.quote(str(process_start_path))}")
    lines.append(f"exec {shlex.quote(command)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o700)


def _active_result_path(repo_path: str, node_id: uuid.UUID, kind: str, attempt: int) -> Path:
    """Allocate isolated protocol files for one process attempt.

    A durable Herdr shell can finish a command after Turn has detached from
    it. Fixed node-level paths would let that late process overwrite the next
    attempt's handoff or exit marker, making the runner observe the wrong
    lifecycle. Active mock runs are consumed directly by their owning worker,
    so they can safely use attempt-scoped protocol paths.
    """
    directory = Path(repo_path) / ".turn" / "interactive"
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    return directory / f"{node_id}.run-{attempt}-{token}.{kind}.json"


async def _launch(
    ctx: NodeExecutionContext,
    *,
    kind: str,
    prompt: str,
    runtime: Settings,
) -> dict[str, Any]:
    if not ctx.repo_path or not Path(ctx.repo_path).is_dir():
        raise RuntimeError("assigned project directory is unavailable")
    control_root = ctx.project_repo_path or ctx.repo_path
    result_path = _active_result_path(control_root, ctx.node.id, kind, ctx.attempt)
    process_exit_path = result_path.with_suffix(".exit")
    process_exit_path.unlink(missing_ok=True)
    process_start_path = result_path.with_suffix(".started")
    process_start_path.unlink(missing_ok=True)
    prompt_path = result_path.with_suffix(".prompt")
    launcher_path = result_path.with_suffix(".launch.sh")
    prompt_path.write_text(prompt, encoding="utf-8")
    control_environment = agent_environment(
        ctx.repo_path,
        ctx.node.id,
        kind,
        result_path,
        ctx.node.agent,
        data_dir=runtime.data_dir,
        project_repo_path=ctx.project_repo_path,
        run_id=ctx.run_id,
    )
    session_id = _session_for(ctx)
    # This fixture deliberately keeps the injected shell command short. The
    # real harness adapters need the full capability environment, while this
    # process only consumes the control-plane handoff and prompt metadata.
    environment = dict(control_environment)
    environment.update({
        "TURN_PROJECT_ID": str(ctx.node.project_id),
        "TURN_AGENT_SESSION_ID": session_id,
        "TURN_INITIAL_PROMPT_FILE": str(prompt_path),
        "TURN_MOCK_ATTEMPT": str(ctx.attempt),
        "TURN_MOCK_EXIT_FILE": str(process_exit_path),
    })
    for resource in ctx.resources:
        if Path(resource.ref).name == "mock-plan.json":
            environment["TURN_MOCK_PLAN_FILE"] = resource.ref
            break
    _write_launcher(
        launcher_path,
        environment=environment,
        command=mock_harness_script(),
        process_start_path=process_start_path,
    )
    transport = ctx.terminal or LocalPtyTransport()
    try:
        terminal = await run_until_result(
            transport,
            ctx.node.id,
            [str(launcher_path)],
            cwd=ctx.repo_path,
            result_path=result_path,
            stream=ctx.stream,
            timeout=ctx.timeout_seconds or runtime.default_run_timeout_seconds,
            idle_warning=runtime.terminal_idle_warning_seconds,
            idle_reap=runtime.terminal_idle_reap_seconds,
            harness_name=mock_harness_script(),
            # The launcher carries the full process environment. Herdr still
            # needs the project identity on its attachment so reconciliation
            # can associate the durable workspace with the Turn project;
            # passing only this small routing value avoids exporting the full
            # capability environment alongside the short command.
            environment={
                "TURN_PROJECT_ID": str(ctx.node.project_id),
                "TURN_HANDOFF_FILE": str(result_path),
            },
            process_start_path=process_start_path,
            process_exit_path=process_exit_path,
            keep_attached=False,
        )
        submitted_present, submitted = read_submission_file(result_path)
        if submitted_present and submitted is None:
            raise InvalidSubmission("mock harness returned an invalid JSON submission")
        if submitted is None and terminal.returncode != 0:
            raise RuntimeError(f"mock harness exited with status {terminal.returncode}")
        if submitted is None:
            raise RuntimeError("mock harness exited without a handoff")
        return submitted
    finally:
        result_path.unlink(missing_ok=True)
        prompt_path.unlink(missing_ok=True)
        process_start_path.unlink(missing_ok=True)
        process_exit_path.unlink(missing_ok=True)
        launcher_path.unlink(missing_ok=True)


async def _remember_session(ctx: NodeExecutionContext, session_id: str) -> None:
    """Expose the fixture's retained conversation through Turn's normal port."""
    if ctx.session_callback is not None:
        await ctx.session_callback(session_id)


def _session_for(ctx: NodeExecutionContext) -> str:
    """Return the current mock conversation, or allocate a fresh one."""
    agent = ctx.node.agent
    if agent is None:
        agent = AgentConfig(harness=HarnessKind.MOCK)
        ctx.node.agent = agent
    session_id = agent.session_id
    if not session_id or session_id == ctx.forbidden_session_id:
        session_id = f"mock-{uuid.uuid4().hex}"
        agent.session_id = session_id
    return session_id


class MockHarnessPlanner(Planner):
    name = "mock-planner"

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
        try:
            plan = PlanResult.model_validate(payload)
        except (TypeError, ValueError) as error:
            raise InvalidSubmission(f"mock planner returned an invalid plan: {error}") from error
        plan.session_id = session_id
        return plan

    async def call_structured(
        self,
        ctx: NodeExecutionContext,
        prompt: str,
        *,
        handoff_kind: str = "result",
    ) -> tuple[dict[str, Any], Usage, str | None]:
        """Run a structured control-plane turn through the mock process.

        Lead Chat and other test-mode control turns must use the same
        process-backed mock harness as ordinary mock nodes. Keeping this on
        the planner adapter preserves the normal terminal/session/run path;
        it does not create an in-process chat shortcut.
        """
        session_id = _session_for(ctx)
        await _remember_session(ctx, session_id)
        payload = await _launch(
            ctx,
            kind=handoff_kind,
            prompt=prompt,
            runtime=self.runtime,
        )
        return payload, Usage(), session_id


class MockHarnessWorker(Worker):
    name = "mock"

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
            try:
                decision = parse_verification(payload)
            except (TypeError, ValueError) as error:
                raise InvalidSubmission(f"mock verifier returned an invalid verification: {error}") from error
            return WorkerResult(
                outcome="COMPLETE",
                summary=decision.summary,
                verification=decision,
                session_id=session_id,
            )
        try:
            result = parse_result(json.dumps(payload))
        except (TypeError, ValueError) as error:
            raise InvalidSubmission(f"mock worker returned an invalid result: {error}") from error
        result.session_id = session_id
        return result
