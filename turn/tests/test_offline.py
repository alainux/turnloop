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
from turn.workers.artifacts import capture_worktree
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


async def test_long_prompt_keeps_full_intent_and_derives_concise_title(tmp_path) -> None:
    store = Store(f"sqlite+aiosqlite:///{tmp_path / 'titles.db'}")
    await store.init()
    prompt = (
        "Build a compact offline release-notes generator with parsing, "
        "formatting, validation, integration, and deterministic regression checks."
    )
    root = await store.create_project(prompt)
    assert root.generated_prompt == prompt
    assert root.project_name == root.objective
    assert len(root.objective) <= 72 and root.objective.endswith("…")
    await store.dispose()


def test_capture_worktree_includes_untracked_file_as_reviewable_diff(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "turn@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Turn"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".turn/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "PLAN.md").write_text("# Plan\n\nVerify the graph.\n")
    captured = capture_worktree(str(tmp_path))
    patch = next(item.content for item in captured if item.kind.value == "code_diff")
    assert "diff --git" in patch and "+++ b/PLAN.md" in patch
    assert "+# Plan" in patch


async def test_codex_worker_refuses_main_repo() -> None:
    """With no project repository configured, the worker must REFUSE to run
    Codex rather than falling back to an undefined location (e.g. the Turn app
    directory)."""
    cfg = Settings()
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
    assert "project repository" in res.summary, res.summary
    print("CODEX WORKER SAFETY TEST PASSED")


async def test_worktree_isolation_happy_path() -> None:
    """A real git repo yields an isolated worktree for child nodes, and a
    re-run cleans up and re-isolates instead of ever returning the main repo.
    The root node's worktree IS the project repo root itself."""
    import os as _os
    from turn.workers import worktree as wtmod

    repo = tempfile.mkdtemp(prefix="turn-wt-test-")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-q", "-m", "init"], check=True)

    cfg = Settings()
    worker = CodexWorker(cfg)
    root_id = uuid.uuid4()
    child_id = uuid.uuid4()

    # Root node's worktree IS the project repo root (not a separate worktree).
    root_wt = wtmod.get_or_create_worktree(root_id, None, force=True, repo_path=repo)
    assert root_wt == repo, "root worktree should be the project repo root"

    # A child node gets a real isolated worktree, never the main repo.
    wt1 = wtmod.get_or_create_worktree(child_id, root_id, force=True, repo_path=repo)
    assert wt1 is not None, "should create a worktree"
    assert wt1 != repo, "must not fall back to the main repo"
    assert _os.path.isdir(wt1), "worktree dir should exist"

    # Second call (a node re-run) must clean up + re-isolate, still not main repo.
    wt2 = wtmod.get_or_create_worktree(child_id, root_id, force=True, repo_path=repo)
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
    cfg = Settings()
    worker = CodexWorker(cfg)
    node = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="do it",
        executor="codex",
        generated_prompt="Edit the file at /tmp/fake-repo/README.md",
        status=NodeStatus.RUNNABLE,
    )
    ctx = NodeExecutionContext(node=node, repo_path="/tmp/fake-repo")
    prompt = worker._build_prompt(ctx, cwd="/tmp/fake-repo/.turn/worktrees/abc")
    assert "/tmp/fake-repo/.turn/worktrees/abc/README.md" in prompt
    # No bare main-repo reference should remain.
    assert "/tmp/fake-repo/README.md" not in prompt
    print("WORKER PROMPT ISOLATION TEST PASSED")


async def test_planner_topology() -> None:
    """apply_plan must build sequential, parallel, and nested-planner graphs.

    A broad objective decomposes into a leaf (engine), two sub-planners
    (chapters), and a join node (assemble) that depends on all of them.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    root = await store.create_project("Build a choose-your-own-adventure game")

    plan = PlanResult(
        nodes=[
            NodeSpec(key="engine", objective="Build the game engine", executor="codex"),
            NodeSpec(key="ch1", objective="Write chapter 1", executor="planner", plan=True),
            NodeSpec(key="ch2", objective="Write chapter 2", executor="planner", plan=True),
            NodeSpec(
                key="assemble",
                objective="Assemble the game",
                executor="codex",
                depends_on=["engine", "ch1", "ch2"],
            ),
        ]
    )
    created = await store.apply_plan(root, plan)
    assert len(created) == 4, created

    by_obj = {n.objective: n for n in created}
    engine = by_obj["Build the game engine"]
    ch1 = by_obj["Write chapter 1"]
    ch2 = by_obj["Write chapter 2"]
    assemble = by_obj["Assemble the game"]

    # nested planners are flagged as planner executors
    assert ch1.executor == "planner"
    assert ch2.executor == "planner"
    assert engine.executor == "codex"
    # the parent becomes a container
    assert (await store.get_node(root.id)).status == NodeStatus.EXPANDED

    # edges: ch1/ch2 are PARALLEL (no deps); assemble DEPENDS_ON all three
    _, edges, _ = await store.get_workgraph(root.id)
    deps = [e for e in edges if e.type == EdgeType.DEPENDS_ON]
    assert any(e.src == engine.id and e.dst == assemble.id for e in deps)
    assert any(e.src == ch1.id and e.dst == assemble.id for e in deps)
    assert any(e.src == ch2.id and e.dst == assemble.id for e in deps)
    assert not any(e.dst == ch1.id for e in deps)  # chapters have no deps -> parallel
    print("PLANNER TOPOLOGY TEST PASSED")


async def test_worktree_merge_up() -> None:
    """A leaf's files must merge up into its parent, and a nested planner's
    files must propagate through it to the root -- so a downstream assembler
    finds real files on disk instead of regenerating from context."""
    import tempfile
    import uuid as _uuid

    from turn.workers import worktree as wtmod

    repo = tempfile.mkdtemp(prefix="turn-merge-test-")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-q", "-m", "init"], check=True)

    root = _uuid.uuid4()
    leaf = _uuid.uuid4()
    chapter = _uuid.uuid4()
    scene = _uuid.uuid4()
    rp = repo

    root_wt = wtmod.get_or_create_worktree(root, None, force=True, repo_path=rp)
    leaf_wt = wtmod.get_or_create_worktree(leaf, root, force=True, repo_path=rp)
    chapter_wt = wtmod.get_or_create_worktree(chapter, root, force=True, repo_path=rp)
    scene_wt = wtmod.get_or_create_worktree(scene, chapter, force=True, repo_path=rp)
    assert all([root_wt, leaf_wt, chapter_wt, scene_wt])

    # leaf -> root
    open(os.path.join(leaf_wt, "engine.py"), "w").write("ENGINE")
    wtmod.commit_worktree(leaf, rp)
    wtmod.merge_into_parent(leaf, root, rp)

    # scene -> chapter -> root (nested planner chain)
    open(os.path.join(scene_wt, "scene1.py"), "w").write("SCENE")
    wtmod.commit_worktree(scene, rp)
    wtmod.merge_into_parent(scene, chapter, rp)
    wtmod.merge_into_parent(chapter, root, rp)

    files = sorted(os.listdir(root_wt))
    assert "engine.py" in files, "leaf file did not merge into root"
    assert "scene1.py" in files, "nested planner file did not reach root"
    print("WORKTREE MERGE-UP TEST PASSED")


async def test_deep_merge_up_ordering() -> None:
    """Regression for the 'missing chapters' bug: a planner container must not
    be merged into its parent until ALL of its own children have merged into it.
    Build root -> A -> B -> leaves (3-level chain) and let the runner's
    _schedule_project propagate completion bottom-up; every leaf file must
    reach the root worktree. With the old shallow-first ordering, B could be
    merged after A, so B's leaf files were dropped from the root."""
    import tempfile
    import subprocess
    import os
    from turn.workers import worktree as wtmod
    from turn.runner.runner import Runner
    from turn.runner.events import EventBus
    from turn.domain.schemas import NodeSpec, PlanResult

    repo = tempfile.mkdtemp(prefix="turn-order-test-")
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-q", "-m", "init"], check=True)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    try:
        reg = WorkerRegistry()
        runner = Runner(store, registry=reg, events=EventBus(), settings=settings)
        root = await store.create_project("root objective", repo_path=repo)
        await store.set_auto_run(root.id, False)

        created = await store.apply_plan(root, PlanResult(nodes=[
            NodeSpec(key="A", objective="alpha container", executor="planner", plan=True),
            NodeSpec(key="l1", objective="leaf one", executor="codex", parent_key="A"),
            NodeSpec(key="l2", objective="leaf two", executor="codex", parent_key="A"),
        ]))
        A = next(c for c in created if c.objective == "alpha container")
        created2 = await store.apply_plan(A, PlanResult(nodes=[
            NodeSpec(key="B", objective="beta container", executor="planner", plan=True),
            NodeSpec(key="l3", objective="leaf three", executor="codex", parent_key="B"),
            NodeSpec(key="l4", objective="leaf four", executor="codex", parent_key="B"),
        ]))
        B = next(c for c in created2 if c.objective == "beta container")
        leaf_ids = {"l1": None, "l2": None, "l3": None, "l4": None}
        obj_to_key = {"leaf one": "l1", "leaf two": "l2", "leaf three": "l3", "leaf four": "l4"}
        for c in created + created2:
            if c.objective in obj_to_key:
                leaf_ids[obj_to_key[c.objective]] = c.id

        # Worktrees + leaf files, then simulate the worker merging leaves up.
        root_wt = wtmod.get_or_create_worktree(root.id, None, force=True, repo_path=repo)
        A_wt = wtmod.get_or_create_worktree(A.id, root.id, force=True, repo_path=repo)
        B_wt = wtmod.get_or_create_worktree(B.id, A.id, force=True, repo_path=repo)
        for k, nid in leaf_ids.items():
            wt = wtmod.get_or_create_worktree(nid, A.id if k in ("l1", "l2") else B.id,
                                             force=True, repo_path=repo)
            open(os.path.join(wt, f"{k}.md"), "w").write(f"CONTENT-{k}")
            wtmod.commit_worktree(nid, repo_path=repo)
        wtmod.merge_into_parent(leaf_ids["l1"], A.id, repo_path=repo)
        wtmod.merge_into_parent(leaf_ids["l2"], A.id, repo_path=repo)
        wtmod.merge_into_parent(leaf_ids["l3"], B.id, repo_path=repo)
        wtmod.merge_into_parent(leaf_ids["l4"], B.id, repo_path=repo)

        # Simulate every worker/assembler having run: leaves and injected
        # assemblers are COMPLETE; planner containers are EXPANDED so the runner
        # will propagate them upward. (The merge-ordering logic is what we test.)
        gwk = await store.get_workgraph(root.id)
        for n in gwk[0]:
            if n.executor == "planner":
                await store.set_status(n.id, NodeStatus.EXPANDED)
            else:
                await store.set_status(n.id, NodeStatus.COMPLETE)

        await runner._schedule_project(root.id)

        files = sorted(os.listdir(root_wt))
        for k in ("l1", "l2", "l3", "l4"):
            assert f"{k}.md" in files, f"leaf {k} missing from root (merge-order bug): {files}"
        print("DEEP MERGE-UP ORDERING TEST PASSED")
    finally:
        await store.dispose()


async def test_plan_application_is_semantically_agnostic() -> None:
    """Persistence preserves planner intent without caps, title rewriting, or
    hidden domain-specific assembler nodes."""
    import tempfile
    from turn.domain.schemas import AgentConfig, HarnessKind, NodeSpec, PlanResult

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    root = await store.create_project(
        "write a guide", agent=AgentConfig(harness=HarnessKind.CLAUDE)
    )
    long_title = "Preserve this complete objective " + ("with detail " * 12)
    specs = [
        NodeSpec(key=f"leaf_{i}", objective="same valid scope", executor="echo")
        for i in range(5)
    ] + [
        NodeSpec(key="long", objective=long_title, executor="echo"),
        NodeSpec(
            key="integrate",
            objective="Integrate exactly when the planner requests it",
            executor="echo",
            depends_on=["leaf_0", "leaf_1"],
        ),
    ]
    created = await store.apply_plan(root, PlanResult(nodes=specs))

    assert len(created) == len(specs)
    assert sum(node.objective == "same valid scope" for node in created) == 5
    assert next(node.objective for node in created if node.objective == long_title) == long_title
    assert sum(node.objective.startswith("Integrate") for node in created) == 1
    assert all(node.executor == "echo" for node in created)
    assert all(node.agent and node.agent.harness == HarnessKind.ECHO for node in created)
    print("SEMANTICALLY AGNOSTIC PLAN APPLICATION TEST PASSED")
    await store.dispose()


async def test_accept_cleans_subtree() -> None:
    """Accepting a merged container deletes its redundant subtree worktrees
    and marks the container + descendants as accepted; the root is never
    touched. Rejecting (feedback) is exercised live, but the FS-cleaning path
    is what risks data, so it is covered here deterministically."""
    import tempfile, subprocess
    from pathlib import Path as _Path

    from turn.workers import worktree as wtmod
    from turn.domain.schemas import NodeSpec, PlanResult

    tmp = tempfile.mkdtemp()
    repo = _Path(tmp) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo))
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo))
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=str(repo))
    (repo / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "-A"], cwd=str(repo))
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=str(repo))
    settings.auto_accept_merges = False
    try:
        store = Store(f"sqlite+aiosqlite:///{tempfile.NamedTemporaryFile(suffix='.db', delete=False).name}")
        await store.init()
        reg = WorkerRegistry()
        runner = Runner(store, registry=reg, events=EventBus(), settings=settings)

        root = await store.create_project("write a guide", repo_path=str(repo))
        created = await store.apply_plan(root, PlanResult(nodes=[
            NodeSpec(key="A", objective="chapter one", executor="planner", plan=True),
            NodeSpec(key="l1", objective="leaf one", executor="codex", parent_key="A"),
            NodeSpec(key="l2", objective="leaf two", executor="codex", parent_key="A"),
        ]))
        A = next(c for c in created if c.objective == "chapter one")
        l1 = next(c for c in created if c.objective == "leaf one")
        l2 = next(c for c in created if c.objective == "leaf two")

        # Materialise the worktrees and merge the leaves up into A, then A up
        # into the root (exactly what the real flow does).
        for nid, pid in ((root.id, None), (A.id, root.id), (l1.id, A.id), (l2.id, A.id)):
            wtmod.get_or_create_worktree(nid, pid, repo_path=str(repo))
        wtmod.merge_into_parent(l1.id, A.id, repo_path=str(repo))
        wtmod.merge_into_parent(l2.id, A.id, repo_path=str(repo))
        wtmod.merge_into_parent(A.id, root.id, repo_path=str(repo))
        for nid in (l1.id, l2.id, A.id):
            await store.set_status(nid, NodeStatus.COMPLETE)
        await store.set_status(A.id, NodeStatus.EXPANDED)

        # Marking merged flags the container for review (no auto-accept).
        await runner._mark_merged(A)
        a2 = await store.get_node(A.id)
        assert a2.needs_review is True, "container not flagged for review"
        assert a2.merge_accepted is False

        # Accept: subtree worktrees removed, container + descendants accepted.
        await runner.accept_merge(A.id)
        for nid in (A.id, l1.id, l2.id):
            assert not wtmod.worktree_path(nid, str(repo)).exists(), f"worktree not cleaned for {nid}"
        a3 = await store.get_node(A.id)
        assert a3.merge_accepted is True, "container not marked accepted"
        assert a3.needs_review is False
        for nid in (l1.id, l2.id):
            assert (await store.get_node(nid)).merge_accepted is True, "descendant not accepted"
        # Root is the accumulation point and must never be cleaned: its repo
        # still holds the seed file and its working branch is intact.
        assert (repo / "seed.txt").exists(), "project repo was damaged"
        assert wtmod._branch_exists(
            wtmod.branch_name(root.id), str(repo)
        ), "root working branch was deleted"
        print("ACCEPT CLEANS SUBTREE TEST PASSED")
        await store.dispose()
    finally:
        settings.auto_accept_merges = False


if __name__ == "__main__":
    asyncio.run(test_auto_run_default())
    asyncio.run(test_codex_worker_refuses_main_repo())
    asyncio.run(test_worktree_isolation_happy_path())
    asyncio.run(test_pause_respected())
    asyncio.run(test_cancel_then_rerun())
    asyncio.run(test_worker_prompt_points_at_worktree())
    asyncio.run(test_planner_topology())
    asyncio.run(test_worktree_merge_up())
    asyncio.run(test_deep_merge_up_ordering())
    asyncio.run(test_plan_application_is_semantically_agnostic())
    asyncio.run(test_accept_cleans_subtree())
