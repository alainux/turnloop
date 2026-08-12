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

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import Node, NodeStatus, Outcome
from turn.workers.base import NodeExecutionContext
from turn.workers.codex_worker import CodexWorker


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


if __name__ == "__main__":
    asyncio.run(test_auto_run_default())
    asyncio.run(test_codex_worker_refuses_main_repo())
    asyncio.run(test_worktree_isolation_happy_path())
