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

    saved_repo = settings.repo_path
    settings.repo_path = repo
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    try:
        reg = WorkerRegistry()
        runner = Runner(store, registry=reg, events=EventBus(), settings=settings)
        root = await store.create_project("root objective")
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
        settings.repo_path = saved_repo
        await store.dispose()


async def test_intermediate_integration() -> None:
    """A nested broad planner should get an injected assembler that integrates
    its direct children (bottom-up composition), the project root should not,
    and we must not inject a second one when the planner already made one."""
    import tempfile
    from turn.runner.runner import Runner
    from turn.runner.events import EventBus
    from turn.domain.schemas import NodeSpec, PlanResult

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    reg = WorkerRegistry()
    Runner(store, registry=reg, events=EventBus(), settings=settings)
    root = await store.create_project("write a guide")
    created = await store.apply_plan(root, PlanResult(nodes=[
        NodeSpec(key="A", objective="chapter one", executor="planner", plan=True),
        NodeSpec(key="l1", objective="leaf one", executor="codex", parent_key="A"),
        NodeSpec(key="l2", objective="leaf two", executor="codex", parent_key="A"),
    ]))
    A = next(c for c in created if c.objective == "chapter one")
    created2 = await store.apply_plan(A, PlanResult(nodes=[
        NodeSpec(key="s1", objective="section one", executor="codex"),
        NodeSpec(key="s2", objective="section two", executor="codex"),
    ]))
    g = await store.get_workgraph(root.id)
    nodes = g[0]; edges = g[1]
    by_obj = {n.objective: n for n in nodes}
    asm = next((n for n in nodes if n.objective.startswith("Integrate:")), None)
    assert asm is not None, "no intermediate assembler injected for nested planner"
    assert asm.parent_id == A.id, "assembler should be a child of the nested planner A"
    dep_srcs = {e.src for e in edges if e.type == "DEPENDS_ON" and e.dst == asm.id}
    assert dep_srcs == {by_obj["section one"].id, by_obj["section two"].id}, dep_srcs
    # Root must not get an injected assembler.
    root_kids = [n for n in nodes if n.parent_id == root.id]
    assert not any(n.objective.startswith("Integrate:") for n in root_kids), "root got an assembler"
    # If the planner already made an assembler, do not add a duplicate.
    created3 = await store.apply_plan(A, PlanResult(nodes=[
        NodeSpec(key="s3", objective="section three", executor="codex"),
        NodeSpec(key="asm", objective="Assemble chapter one", executor="codex", depends_on=["s3"]),
    ]))
    assert not any(c.objective.startswith("Integrate:") for c in created3), "duplicate assembler injected"
    print("INTERMEDIATE INTEGRATION TEST PASSED")
    await store.dispose()


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
    asyncio.run(test_intermediate_integration())
