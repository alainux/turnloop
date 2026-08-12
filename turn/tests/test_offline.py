"""Offline tests — no LLM / network / Codex calls.

These exercise the kernel and the safety-critical worker isolation logic using
a temp SQLite store and deterministic inputs, so they can run in CI or during
development without burning API resources.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import uuid

from turn.config import Settings, settings
from turn.db.store import Store
from turn.domain.schemas import (
    EdgeSpec,
    EdgeType,
    Node,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
)
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.base import NodeExecutionContext, Planner
from turn.workers.codex_worker import CodexWorker
from turn.workers.echo_worker import EchoWorker
from turn.workers.registry import WorkerRegistry


async def test_auto_run_default() -> None:
    """New projects inherit the persisted auto-run preference."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()

    # No preference set yet -> default True.
    root = await store.create_project("first")
    assert root.auto_run is True

    # User flips to manual -> future projects default to manual.
    await store.set_setting("default_auto_run", "0")
    root2 = await store.create_project("second")
    assert root2.auto_run is False

    # And back to auto.
    await store.set_setting("default_auto_run", "1")
    root3 = await store.create_project("third")
    assert root3.auto_run is True

    # Setting round-trips.
    assert (await store.get_setting("default_auto_run", "1")) == "1"
    assert (await store.get_setting("missing_key", "fallback")) == "fallback"

    await store.dispose()
    print("AUTO-RUN DEFAULT TEST PASSED")


async def test_codex_worker_refuses_main_repo() -> None:
    """With no isolated worktree possible, the worker must REFUSE to run Codex
    in the main repository rather than falling back to it."""
    cfg = Settings(repo_path="/tmp/this-path-is-not-a-git-repo-xyz")
    worker = CodexWorker(cfg)
    node = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="do something dangerous",
        executor="codex",
        status=NodeStatus.RUNNABLE,
    )
    ctx = NodeExecutionContext(node=node, repo_path=None)
    # The guard returns before any subprocess is spawned, so this is offline.
    res = await worker.execute(ctx)
    assert res.outcome == Outcome.FAIL, res
    assert "main repository" in res.summary, res.summary
    print("CODEX WORKER SAFETY TEST PASSED")


async def test_worktree_isolation_happy_path() -> None:
    """A real git repo yields an isolated worktree, and a re-run cleans up and
    re-isolates instead of ever returning the main repo path."""
    repo = tempfile.mkdtemp(prefix="turn-wt-test-")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-q", "-m", "init"], check=True)

    cfg = Settings(repo_path=repo)
    worker = CodexWorker(cfg)
    nid = uuid.uuid4()

    wt1 = worker._prepare_worktree(nid)
    assert wt1 is not None, "should create a worktree"
    assert wt1 != repo, "must not fall back to the main repo"
    assert os.path.isdir(wt1), "worktree dir should exist"

    # Second call (a node re-run) must clean up + re-isolate, still not main repo.
    wt2 = worker._prepare_worktree(nid)
    assert wt2 is not None and wt2 != repo

    subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt2], check=True)
    print("WORKTREE ISOLATION TEST PASSED")


async def test_pause_respected() -> None:
    """In auto-run mode a paused runnable node must not be launched."""

    class P(Planner):
        name = "p"

        async def plan(self, ctx):
            return PlanResult(
                nodes=[
                    NodeSpec(key="a", objective="do a", executor="echo"),
                    NodeSpec(key="b", objective="do b", executor="echo", depends_on=["a"]),
                ],
                edges=[EdgeSpec(type=EdgeType.DEPENDS_ON, src="b", dst="a")],
            )

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    reg = WorkerRegistry()
    reg.register(EchoWorker())
    reg.register_planner(P())
    runner = Runner(store, registry=reg, events=EventBus(), settings=settings)
    root = await store.create_project("x")
    await store.set_auto_run(root.id, True)
    await runner.step(root.id)  # plan
    if runner._running:
        await asyncio.gather(*runner._running.values(), return_exceptions=True)
    nodes, _, _ = await store.get_workgraph(root.id)
    a = next(n for n in nodes if n.objective == "do a")
    await runner.pause(a.id)  # pause before any tick
    await runner.tick()  # auto mode would launch runnable nodes
    if runner._running:
        await asyncio.gather(*runner._running.values(), return_exceptions=True)
    a2 = await store.get_node(a.id)
    # A paused node must be held (not launched) — it shows as BLOCKED, never
    # RUNNING or COMPLETE, and is never in the running set.
    assert a2.status != NodeStatus.COMPLETE, a2.status
    assert a2.status != NodeStatus.RUNNING, a2.status
    assert a.id not in runner._running
    await store.dispose()
    print("PAUSE RESPECTED TEST PASSED")


async def test_cancel_then_rerun() -> None:
    """A cancelled node can be revived and re-run via run_node (no LLM)."""

    class P(Planner):
        name = "p"

        async def plan(self, ctx):
            return PlanResult(
                nodes=[NodeSpec(key="a", objective="do a", executor="echo")],
                edges=[],
            )

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    reg = WorkerRegistry()
    reg.register(EchoWorker())
    reg.register_planner(P())
    runner = Runner(store, registry=reg, events=EventBus(), settings=settings)
    root = await store.create_project("x")
    await store.set_auto_run(root.id, False)  # manual
    await runner.step(root.id)  # plan
    if runner._running:
        await asyncio.gather(*runner._running.values(), return_exceptions=True)
    nodes, _, _ = await store.get_workgraph(root.id)
    a = next(n for n in nodes if n.objective == "do a")
    await runner.cancel(a.id)  # not running -> CANCELLED
    a = await store.get_node(a.id)
    assert a.status == NodeStatus.CANCELLED, a.status
    nid = await runner.run_node(a.id)  # revive + re-run
    assert nid == a.id
    if runner._running:
        await asyncio.gather(*runner._running.values(), return_exceptions=True)
    a = await store.get_node(a.id)
    assert a.status == NodeStatus.COMPLETE, a.status  # re-ran to completion
    await store.dispose()
    print("CANCEL-RERUN TEST PASSED")


async def test_worker_prompt_points_at_worktree() -> None:
    """The worker must rewrite the main-repo path in a node's prompt to the
    isolated worktree, so Codex never operates on the source tree."""
    cfg = Settings(repo_path="/tmp/fake-repo")
    worker = CodexWorker(cfg)
    node = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="do it",
        executor="codex",
        generated_prompt="Edit the file at /tmp/fake-repo/README.md",
        status=NodeStatus.RUNNABLE,
    )
    ctx = NodeExecutionContext(node=node, repo_path=None)
    prompt = worker._build_prompt(ctx, cwd="/tmp/fake-repo/.turn/worktrees/abc")
    assert "/tmp/fake-repo/.turn/worktrees/abc/README.md" in prompt
    # No bare main-repo reference should remain.
    assert "/tmp/fake-repo/README.md" not in prompt
    print("WORKER PROMPT ISOLATION TEST PASSED")


if __name__ == "__main__":
    asyncio.run(test_auto_run_default())
    asyncio.run(test_codex_worker_refuses_main_repo())
    asyncio.run(test_worktree_isolation_happy_path())
    asyncio.run(test_pause_respected())
    asyncio.run(test_cancel_then_rerun())
    asyncio.run(test_worker_prompt_points_at_worktree())
