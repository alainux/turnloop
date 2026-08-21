from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from turn.contracts.organization import audit_plan, organization_metrics
from turn.db.store import Store
from turn.domain.organization import (
    AcceptanceCriterion,
    AcceptanceEvidence,
    EvidenceStatus,
    HandoffContract,
    ManagerDecision,
    ManagerPhase,
    OrganizationContract,
    OrganizationPhase,
    OrganizationScale,
    WorkspaceIsolation,
    WorkItemSpec,
    WorkItemStatus,
)
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    ArtifactKind,
    ArtifactSpec,
    Edge,
    EdgeType,
    InputSpec,
    ManagerResult,
    Node,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
    RunPolicy,
    RunStatus,
    WorkerResult,
)
from turn.config import Settings
from turn.runner.scheduler import Scheduler
from turn.runner.organization import OrganizationManager
from turn.runner.runner import Runner
from turn.runner.workspaces import WorkspaceError, WorkspaceManager


def test_large_multi_product_request_gets_an_organization_charter():
    contract = OrganizationContract.from_objective(
        "Operate a large multi-product organization"
    )
    assert contract.scale is OrganizationScale.ORGANIZATION
    assert contract.require_independent_verification is True
    assert contract.min_first_level_production_owners == 2


def test_small_production_ready_service_stays_a_focused_charter():
    contract = OrganizationContract.from_objective(
        "Build a small production-ready command-line service"
    )

    assert contract.scale is OrganizationScale.FOCUSED
    assert contract.require_independent_verification is False
    assert contract.min_first_level_production_owners == 1


def test_independent_audit_rejects_department_named_executor_leaves():
    contract = OrganizationContract(
        charter="produce a coherent multi-part outcome",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=["the outcome is usable"],
        min_first_level_production_owners=2,
        require_independent_verification=True,
    )
    plan = PlanResult(
        nodes=[
            NodeSpec(key="shape", objective="Own the entire result", agent_type=AgentType.EXECUTOR),
            NodeSpec(key="detail", objective="Own the complete system", agent_type=AgentType.EXECUTOR),
            NodeSpec(key="compose", objective="Compose the result", agent_type=AgentType.INTEGRATOR, follows=["shape", "detail"]),
            NodeSpec(key="evaluate", objective="Evaluate the result", agent_type=AgentType.VERIFIER, follows=["compose"]),
        ]
    )
    audit = audit_plan(contract, plan)
    assert audit.accepted is False
    assert audit.has_convergence is True
    assert any("compressed" in error for error in audit.errors)


def test_audit_accepts_real_recursive_ownership_and_qa():
    contract = OrganizationContract(
        charter="produce a coherent multi-part outcome",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=["the outcome is usable"],
        min_first_level_production_owners=2,
        require_independent_verification=True,
    )
    plan = PlanResult(
        nodes=[
            NodeSpec(key="product", objective="Own product definition", agent_type=AgentType.PLANNER, organization_contract=OrganizationContract.from_objective("Own product definition")),
            NodeSpec(key="engineering", objective="Run the engineering organization", agent_type=AgentType.PLANNER, organization_contract=OrganizationContract.from_objective("Run the engineering organization")),
            NodeSpec(key="integrate", objective="Integrate the release", agent_type=AgentType.INTEGRATOR, follows=["product", "engineering"]),
            NodeSpec(key="verify", objective="Independently verify the release", agent_type=AgentType.VERIFIER, follows=["integrate"]),
        ]
    )
    audit = audit_plan(contract, plan)
    assert audit.accepted is True
    assert audit.has_convergence is True
    assert audit.has_independent_verification is True


def test_organization_metrics_make_flat_ownership_visible():
    contract = OrganizationContract(
        charter="produce a coherent multi-part outcome",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=["the outcome is usable"],
        min_first_level_production_owners=2,
        require_independent_verification=True,
    )
    plan = PlanResult(
        nodes=[
            NodeSpec(key="product", objective="Product", agent_type=AgentType.PLANNER),
            NodeSpec(key="engineering", objective="Run engineering", agent_type=AgentType.PLANNER),
            NodeSpec(key="integrate", objective="Integrate", agent_type=AgentType.INTEGRATOR, follows=["product", "engineering"]),
            NodeSpec(key="verify", objective="Verify", agent_type=AgentType.VERIFIER, follows=["integrate"]),
        ],
        organization_contract=contract,
    )
    # Plan-level metrics are intentionally not accepted as evidence by
    # themselves; this test exercises the same signals after materialization.
    project_id = uuid.UUID(int=1)
    nodes = [
        Node(
            project_id=project_id,
            objective=spec.objective,
            executor=("planner" if spec.agent_type is AgentType.PLANNER else spec.agent_type.value),
            agent=AgentConfig(type_id=spec.agent_type or AgentType.EXECUTOR, harness="mock"),
            organization_contract=contract if spec.agent_type is AgentType.PLANNER else None,
        )
        for spec in plan.nodes
    ]
    by_key = dict(zip((spec.key for spec in plan.nodes), nodes, strict=True))

    edges = [
        Edge(src=by_key["product"].id, dst=by_key["integrate"].id, type=EdgeType.FOLLOWS),
        Edge(src=by_key["engineering"].id, dst=by_key["integrate"].id, type=EdgeType.FOLLOWS),
        Edge(src=by_key["integrate"].id, dst=by_key["verify"].id, type=EdgeType.FOLLOWS),
    ]
    metrics = organization_metrics(nodes, edges)
    assert metrics.boundary_count == 2
    assert metrics.planner_count == 2
    assert metrics.converged_boundary_count == 0


@pytest.mark.asyncio
async def test_organization_plan_materializes_tickets_and_typed_handoffs(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("Build a game", repo_path=str(tmp_path / "projects" / "game"))
    plan = PlanResult(
        nodes=[
            NodeSpec(key="product", objective="Run product", agent_type=AgentType.PLANNER, organization_contract=OrganizationContract.from_objective("Run product")),
            NodeSpec(key="engineering", objective="Run engineering", agent_type=AgentType.PLANNER, organization_contract=OrganizationContract.from_objective("Run engineering"), priority=10),
            NodeSpec(key="integrate", objective="Integrate", agent_type=AgentType.INTEGRATOR, follows=["product", "engineering"]),
            NodeSpec(
                key="verify",
                objective="Verify",
                agent_type=AgentType.VERIFIER,
                follows=["integrate"],
                required_handoffs=[HandoffContract(name="release", schema_name="release.v1")],
            ),
        ]
    )
    # Direct storage can preserve a plan for inspection; the runner's enforced
    # path is covered by the audit tests above.
    created = await store.apply_plan(root, plan)
    items = await store.list_work_items(root.id)
    assert len(items) == 4
    assert items[0].priority == 10
    handoffs = await store.list_handoffs(root.id)
    assert len(handoffs) == 1
    assert handoffs[0].contract.schema_name == "release.v1"
    integrator = next(node for node in created if node.objective == "Integrate")
    artifact = (
        await store.add_artifacts(
            integrator.id,
            [
                ArtifactSpec(
                    kind=ArtifactKind.JSON,
                    name="release",
                    content={"ready": True},
                    schema_name="release.v1",
                    schema_version="1",
                    evidence_refs=["release.json"],
                )
            ],
        )
    )[0]
    available = (await store.list_handoffs(root.id))[0]
    assert available.status.value == "AVAILABLE"
    assert available.artifact_id == artifact.id
    consumer = next(node for node in created if node.objective == "Verify")
    runner = Runner(
        store,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
    )
    await runner._accept_consumed_handoffs(
        consumer,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="consumed release",
            evidence=[
                AcceptanceEvidence(
                    criterion_id="release-consumed",
                    status=EvidenceStatus.PASS,
                    summary="The release artifact was consumed.",
                    refs=["qa/release-consumed.json"],
                )
            ],
        ),
    )
    assert (await store.list_handoffs(root.id))[0].status.value == "ACCEPTED"

    work = next(node for node in created if node.objective == "Run engineering")
    assert (await store.get_work_item(work.work_item_id)).status is WorkItemStatus.BACKLOG
    await store.set_status(work.id, NodeStatus.RUNNABLE)
    assert (await store.get_work_item(work.work_item_id)).status is WorkItemStatus.READY
    await store.update_handoff(handoffs[0].id, status="ACCEPTED", evidence_refs=["release.json"])
    assert (await store.list_handoffs(root.id))[0].evidence_refs == ["release.json"]
    refreshed = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await refreshed.init()
    assert len(await refreshed.list_work_items(root.id)) == 4
    assert (await refreshed.get_node(root.id)).organization_review.phase is OrganizationPhase.EXECUTE_FRONTIER
    await store.dispose()
    await refreshed.dispose()


@pytest.mark.asyncio
async def test_scheduler_respects_project_and_global_capacity(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("parallel fixture", repo_path=str(tmp_path / "projects" / "parallel"))
    root.auto_run = True
    root.run_policy.auto_run = True
    root.run_policy.max_parallel_agents = 2
    await store._save_node(root)
    await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key=f"work-{index}", objective=f"Work {index}", executor="deterministic")
            for index in range(4)
        ]),
    )
    started: list[uuid.UUID] = []

    async def execute(node, _project_id):
        started.append(node.id)
        await store.set_status(node.id, NodeStatus.RUNNING)
        await asyncio.sleep(0.2)
        await store.set_status(node.id, NodeStatus.COMPLETE)

    scheduler = Scheduler(
        store,
        Settings(max_parallel_agents=2),
        execute,
        lambda *_args: asyncio.sleep(0),
        lambda _root: asyncio.sleep(0),
        lambda: None,
    )
    await scheduler.schedule_once(root.id)
    await asyncio.sleep(0)
    assert len(started) == 2
    await scheduler.wait_for_idle(root.id)
    await store.dispose()


@pytest.mark.asyncio
async def test_manager_loop_accepts_only_verified_charters(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("Build a game", repo_path=str(tmp_path / "projects" / "game"))
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="product", objective="Product", agent_type=AgentType.PLANNER, organization_contract=OrganizationContract.from_objective("Product")),
            NodeSpec(key="engineering", objective="Engineering", agent_type=AgentType.PLANNER, organization_contract=OrganizationContract.from_objective("Engineering")),
            NodeSpec(key="integrate", objective="Integrate", agent_type=AgentType.INTEGRATOR, follows=["product", "engineering"]),
            NodeSpec(key="verify", objective="Verify", agent_type=AgentType.VERIFIER, follows=["integrate"]),
        ]),
    )
    for node in created:
        await store.set_status(node.id, NodeStatus.COMPLETE)
    verifier = next(node for node in created if node.agent.type_id is AgentType.VERIFIER)
    verifier = await store.get_node(verifier.id)
    criterion_id = root.organization_contract.acceptance_criteria[0].id
    verifier.verification = {
        "decision": "APPROVE",
        "summary": "accepted",
        "evidence_refs": ["qa/report.json"],
    }
    await store._save_node(verifier)
    incomplete = await OrganizationManager().review(store, root.id)
    assert incomplete.decision is not ManagerDecision.ACCEPT

    verifier = await store.get_node(verifier.id)
    verifier.verification = {
        "decision": "APPROVE",
        "summary": "accepted",
        "evidence_refs": ["qa/report.json"],
        "evidence": [
            {
                "criterion_id": criterion_id,
                "status": "PASS",
                "summary": "The release satisfies the charter.",
                "refs": ["qa/report.json"],
            }
        ],
    }
    await store._save_node(verifier)
    decision = await OrganizationManager().review(store, root.id)
    assert decision.phase is OrganizationPhase.ACCEPT_CHARTER
    assert decision.replan is False
    await store.dispose()


@pytest.mark.asyncio
async def test_worktree_isolation_commits_and_merges_explicitly(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "turn@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Turn Test"], cwd=root, check=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=root, check=True)

    node_id = uuid.uuid4()
    manager = WorkspaceManager(tmp_path / "turn-state")
    project_id = "production-project"
    workspace = await manager.ensure(root, node_id, project_id=project_id)
    assert Path(workspace).is_relative_to(tmp_path / "turn-state" / "worktrees" / project_id)
    assert manager.branch_name(root, node_id, project_id).startswith("turn/production-project/")
    (tmp_path / "marker").write_text(workspace, encoding="utf-8")
    Path(workspace, "README.md").write_text("worker\n", encoding="utf-8")
    commit = await manager.commit(workspace, node_id)
    assert commit
    assert Path(root, "README.md").read_text(encoding="utf-8") == "base\n"
    await manager.merge(root, commit, node_id)
    assert Path(root, "README.md").read_text(encoding="utf-8") == "worker\n"
    await manager.remove(root, node_id, project_id=project_id)
    assert not Path(workspace).exists()


@pytest.mark.asyncio
async def test_merge_conflict_isolated_to_composer_and_aborted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "turn@example.test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Turn Test"], cwd=root, check=True)
    (root / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=root, check=True)

    manager = WorkspaceManager(tmp_path / "turn-state")
    project_id = "conflict-project"
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    composer = uuid.uuid4()
    workspace_a = await manager.ensure(root, worker_a, project_id=project_id)
    workspace_b = await manager.ensure(root, worker_b, project_id=project_id)
    workspace_composer = await manager.ensure(root, composer, project_id=project_id)

    Path(workspace_a, "README.md").write_text("worker-a\n", encoding="utf-8")
    commit_a = await manager.commit(workspace_a, worker_a)
    Path(workspace_b, "README.md").write_text("worker-b\n", encoding="utf-8")
    commit_b = await manager.commit(workspace_b, worker_b)
    assert commit_a and commit_b

    with pytest.raises(WorkspaceError, match="git merge"):
        await manager.merge_into_workspace(
            workspace_composer,
            [commit_a, commit_b],
            composer,
        )

    assert Path(root, "README.md").read_text(encoding="utf-8") == "base\n"
    assert Path(workspace_composer, "README.md").read_text(encoding="utf-8") == "worker-a\n"
    for checkout in (root, Path(workspace_composer)):
        merge_head = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        assert merge_head.returncode != 0

    await manager.remove(root, worker_a, project_id=project_id)
    await manager.remove(root, worker_b, project_id=project_id)
    await manager.remove(root, composer, project_id=project_id)


@pytest.mark.asyncio
async def test_worker_commit_stays_on_branch_until_root_acceptance(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "turn@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Turn Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=repo, check=True)

    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "small request",
        repo_path=str(repo),
        run_policy=RunPolicy(
            auto_run=False,
            workspace_isolation=WorkspaceIsolation.WORKTREE,
        ),
    )
    node = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="worker output",
        executor="deterministic",
    )
    runner = Runner(
        store,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
    )
    workspace = await runner.workspaces.ensure(repo, node.id, root.id)
    await store.set_workspace_ref(
        node.id,
        path=workspace,
        branch=runner.workspaces.branch_name(repo, node.id, root.id),
    )
    Path(workspace, "worker.txt").write_text("worker\n", encoding="utf-8")

    await runner._commit_workspace_result(node)

    persisted = await store.get_node(node.id)
    assert persisted.workspace_commit
    assert not (repo / "worker.txt").exists()
    assert Path(workspace, "worker.txt").read_text(encoding="utf-8") == "worker\n"
    await store.dispose()


@pytest.mark.asyncio
async def test_dirty_repo_context_uses_canonical_path_without_allocating_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    (repo / "user-change.txt").write_text("uncommitted\n", encoding="utf-8")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "small request",
        repo_path=str(repo),
        run_policy=RunPolicy(
            auto_run=False,
            workspace_isolation=WorkspaceIsolation.WORKTREE,
        ),
    )
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="serial worker",
        executor="deterministic",
    )
    runner = Runner(
        store,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
    )

    context = await runner._build_context(child)

    assert context.repo_path == str(repo.resolve())
    assert (await store.get_node(child.id)).workspace_path is None
    assert not runner.workspaces.target(repo, child.id, root.id).exists()
    await store.dispose()


@pytest.mark.asyncio
async def test_runner_reviews_a_settled_material_boundary_after_execution(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "Build a game",
        repo_path=str(tmp_path / "projects" / "game"),
        run_policy=RunPolicy(auto_run=False),
    )
    root.organization_contract = OrganizationContract(
        charter="produce a coherent multi-part outcome",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=["the outcome is usable"],
        min_first_level_production_owners=2,
        require_independent_verification=True,
    )
    await store._save_node(root)
    created = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(
                    key="product",
                    objective="Product",
                    agent_type=AgentType.PLANNER,
                    organization_contract=OrganizationContract.from_objective("Product"),
                ),
                NodeSpec(
                    key="engineering",
                    objective="Engineering",
                    agent_type=AgentType.PLANNER,
                    organization_contract=OrganizationContract.from_objective("Engineering"),
                ),
                NodeSpec(
                    key="integrate",
                    objective="Integrate",
                    agent_type=AgentType.INTEGRATOR,
                    follows=["product", "engineering"],
                ),
                NodeSpec(
                    key="verify",
                    objective="Verify",
                    agent_type=AgentType.VERIFIER,
                    follows=["integrate"],
                ),
            ],
        ),
    )
    for node in created:
        await store.set_status(node.id, NodeStatus.COMPLETE)
    verifier = next(node for node in created if node.agent.type_id is AgentType.VERIFIER)
    verifier = await store.get_node(verifier.id)
    criterion_id = root.organization_contract.acceptance_criteria[0].id
    verifier.verification = {
        "decision": "APPROVE",
        "summary": "release verified",
        "evidence_refs": ["qa/release.json"],
        "evidence": [
            {
                "criterion_id": criterion_id,
                "status": "PASS",
                "summary": "The release scenario passed.",
                "refs": ["qa/release.json"],
            }
        ],
    }
    await store._save_node(verifier)
    await store.set_status(root.id, NodeStatus.EXPANDED)
    await store.set_manager_state(
        root.id,
        phase=ManagerPhase.EXECUTING,
        reasons=["frontier settled"],
    )
    runner = Runner(
        store,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
    )

    await runner._review_safe_organizations(root.id)

    assert (await store.get_node(root.id)).manager_phase is ManagerPhase.ACCEPTED
    await store.dispose()


@pytest.mark.asyncio
async def test_backlog_dependencies_materialize_as_normal_nodes(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("small request", repo_path=str(tmp_path / "projects" / "small"))
    first = await store.create_work_item(
        project_id=root.id,
        organization_id=root.id,
        key="first",
        title="First",
        objective="Do first",
    )
    second = await store.create_work_item(
        project_id=root.id,
        organization_id=root.id,
        key="second",
        title="Second",
        objective="Do second",
        depends_on=[first.id],
    )
    materialized = await store.materialize_ready_work_items(root.id, limit=10)
    assert [node.objective for node in materialized] == ["First"]
    assert (await store.get_work_item(second.id)).node_id is None
    await store.set_status(materialized[0].id, NodeStatus.COMPLETE)
    assert (await store.get_work_item(first.id)).status is WorkItemStatus.COMPLETE
    materialized = await store.materialize_ready_work_items(root.id, limit=10)
    assert [node.objective for node in materialized] == ["Second"]
    assert (await store.get_work_item(second.id)).status is WorkItemStatus.ACTIVE
    await store.dispose()


@pytest.mark.asyncio
async def test_manager_continue_and_block_are_structured_and_durable(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("small request", repo_path=str(tmp_path / "projects" / "small"))
    manager = OrganizationManager()
    continued = await manager.apply_result(
        store,
        root.id,
        ManagerResult(
            decision=ManagerDecision.CONTINUE,
            summary="add the missing unit",
            work_items=[
                WorkItemSpec(
                    key="unit",
                    title="Add unit",
                    instructions="Implement the missing unit",
                )
            ],
        ),
    )
    assert continued.decision is ManagerDecision.CONTINUE
    assert (await store.get_node(root.id)).manager_phase is ManagerPhase.EXECUTING
    assert len(await store.list_work_items(root.id)) == 1
    blocked = await manager.apply_result(
        store,
        root.id,
        ManagerResult(
            decision=ManagerDecision.BLOCK,
            summary="need a product choice",
            missing_inputs=[InputSpec(id="choice", label="Product choice")],
        ),
    )
    assert blocked.decision is ManagerDecision.BLOCK
    refreshed = await store.get_node(root.id)
    assert refreshed.manager_phase is ManagerPhase.BLOCKED
    assert refreshed.required_inputs[0].id == "choice"
    await store.dispose()


@pytest.mark.asyncio
async def test_work_backlog_keeps_thirty_items_persisted_and_capacity_bounded(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "backlog fixture",
        repo_path=str(tmp_path / "projects" / "backlog"),
        run_policy=RunPolicy(auto_run=True, max_parallel_agents=4),
    )

    for index in range(30):
        await store.create_work_item(
            project_id=root.id,
            organization_id=root.id,
            key=f"item-{index:02d}",
            title=f"Known work {index:02d}",
            objective=f"Complete known backlog item {index:02d}",
        )

    persisted = await store.list_work_items(root.id)
    assert len(persisted) == 30
    materialized = await store.materialize_ready_work_items(root.id, limit=4)
    assert len(materialized) == 4

    persisted = await store.list_work_items(root.id)
    assert len(persisted) == 30
    assert len([item for item in persisted if item.status is WorkItemStatus.ACTIVE]) == 4
    assert len([item for item in persisted if item.node_id is None]) == 26
    nodes, _, _ = await store.get_workgraph(root.id)
    assert len(nodes) == 5  # root plus the capacity-sized active frontier
    await store.dispose()


@pytest.mark.asyncio
async def test_nested_evidence_with_duplicate_criterion_id_does_not_accept_parent(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "scoped evidence fixture",
        repo_path=str(tmp_path / "projects" / "scoped-evidence"),
    )
    root.organization_contract = OrganizationContract(
        charter="accept an accurate result",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=[AcceptanceCriterion(id="accuracy", description="The result is accurate.")],
        require_independent_verification=False,
    )
    await store._save_node(root)
    child_contract = OrganizationContract(
        charter="produce an accurate child result",
        scale=OrganizationScale.FOCUSED,
        acceptance_criteria=[AcceptanceCriterion(id="accuracy", description="The child result is accurate.")],
    )
    child = (
        await store.apply_plan(
            root,
            PlanResult(
                nodes=[
                    NodeSpec(
                        key="child",
                        objective="Child organization",
                        agent_type=AgentType.PLANNER,
                        organization_contract=child_contract,
                    )
                ]
            ),
        )
    )[0]
    descendants = await store.apply_plan(
        child,
        PlanResult(
            nodes=[
                NodeSpec(key="child-work", objective="Child work", executor="deterministic"),
                NodeSpec(
                    key="child-review",
                    objective="Review child accuracy",
                    agent_type=AgentType.VERIFIER,
                    follows=["child-work"],
                ),
            ],
        ),
    )
    for node in [child, *descendants]:
        await store.set_status(node.id, NodeStatus.COMPLETE)
    child_verifier = next(
        node for node in descendants if node.agent and node.agent.type_id is AgentType.VERIFIER
    )
    child_verifier = await store.get_node(child_verifier.id)
    child_verifier.verification = {
        "decision": "APPROVE",
        "summary": "child accuracy passed",
        "evidence_refs": ["qa/child-accuracy.json"],
        "evidence": [
            {
                "criterion_id": "accuracy",
                "status": "PASS",
                "summary": "The child result is accurate.",
                "refs": ["qa/child-accuracy.json"],
            }
        ],
    }
    await store._save_node(child_verifier)

    decision = await OrganizationManager().review(store, root.id)

    assert decision.decision is ManagerDecision.CONTINUE
    assert "contract acceptance criteria have no passing evidence for: accuracy" in decision.reason
    await store.dispose()


@pytest.mark.asyncio
async def test_nested_organization_capacity_limits_frontier(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("small request", repo_path=str(tmp_path / "projects" / "small"))
    nested_contract = OrganizationContract(
        charter="engineering",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=["engineering works"],
    )
    created = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(
                    key="engineering",
                    objective="Engineering organization",
                    agent_type=AgentType.PLANNER,
                    organization_contract=nested_contract,
                )
            ]
        ),
    )
    engineering = created[0]
    engineering = await store.get_node(engineering.id)
    engineering.organization_contract.budget.max_active_workers = 1
    await store._save_node(engineering)
    await store.apply_plan(
        engineering,
        PlanResult(
            nodes=[
                NodeSpec(key="one", objective="One", executor="deterministic"),
                NodeSpec(key="two", objective="Two", executor="deterministic"),
            ]
        ),
    )
    root = await store.get_node(root.id)
    root.auto_run = True
    root.run_policy = RunPolicy(auto_run=True, max_parallel_agents=4)
    await store._save_node(root)
    started: list[uuid.UUID] = []

    async def execute(node, _project_id):
        started.append(node.id)
        await store.set_status(node.id, NodeStatus.RUNNING)
        await asyncio.sleep(0.15)
        await store.set_status(node.id, NodeStatus.COMPLETE)

    scheduler = Scheduler(
        store,
        Settings(max_parallel_agents=4),
        execute,
        lambda *_args: asyncio.sleep(0),
        lambda _root: asyncio.sleep(0),
        lambda: None,
    )
    await scheduler.schedule_once(root.id)
    await asyncio.sleep(0)
    assert len(started) == 1
    await scheduler.wait_for_idle(root.id)
    await store.dispose()


@pytest.mark.asyncio
async def test_budget_exhaustion_requests_manager_review(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("small request", repo_path=str(tmp_path / "projects" / "small"))
    root.auto_run = True
    root.run_policy = RunPolicy(auto_run=True, max_parallel_agents=1, max_total_runs=1)
    root.organization_contract.budget.max_total_runs = 1
    await store._save_node(root)
    await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="one", objective="One", executor="deterministic")]),
    )
    requested: list[tuple[uuid.UUID, str]] = []

    async def request_review(boundary_id, reason):
        requested.append((boundary_id, reason))

    async def execute(node, _project_id):
        run = await store.create_run(node, "deterministic")
        await store.set_status(node.id, NodeStatus.RUNNING)
        await store.set_status(node.id, NodeStatus.COMPLETE)
        await store.update_run(
            run.id,
            status=RunStatus.COMPLETE,
            outcome=Outcome.COMPLETE,
        )

    scheduler = Scheduler(
        store,
        Settings(max_parallel_agents=1),
        execute,
        lambda *_args: asyncio.sleep(0),
        lambda _root: asyncio.sleep(0),
        lambda: None,
        request_review=request_review,
    )
    await scheduler.schedule_once(root.id)
    await scheduler.wait_for_idle(root.id)
    await scheduler.schedule_once(root.id)
    assert requested and requested[0][0] == root.id
    await store.dispose()


@pytest.mark.asyncio
async def test_dirty_repo_serializes_worktree_policy_without_touching_user_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "turn@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Turn Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("user change\n", encoding="utf-8")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("small request", repo_path=str(repo))
    root.auto_run = True
    root.run_policy = RunPolicy(
        auto_run=True,
        max_parallel_agents=4,
        workspace_isolation=WorkspaceIsolation.WORKTREE,
    )
    await store._save_node(root)
    await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(key="one", objective="One", executor="deterministic"),
                NodeSpec(key="two", objective="Two", executor="deterministic"),
            ]
        ),
    )
    manager = WorkspaceManager(tmp_path / "turn")
    assert await manager.isolation_available(repo) is False
    started: list[uuid.UUID] = []

    async def execute(node, _project_id):
        started.append(node.id)
        await store.set_status(node.id, NodeStatus.RUNNING)
        await asyncio.sleep(0.1)
        await store.set_status(node.id, NodeStatus.COMPLETE)

    scheduler = Scheduler(
        store,
        Settings(max_parallel_agents=4),
        execute,
        lambda *_args: asyncio.sleep(0),
        lambda _root: asyncio.sleep(0),
        lambda: None,
        isolation_available=lambda _project_id: manager.isolation_available(repo),
    )
    await scheduler.schedule_once(root.id)
    await asyncio.sleep(0)
    assert len(started) == 1
    await scheduler.wait_for_idle(root.id)
    assert (repo / "README.md").read_text(encoding="utf-8") == "user change\n"
    await store.dispose()


@pytest.mark.asyncio
async def test_scheduler_stops_live_tasks_of_already_cancelled_nodes(tmp_path):
    """A CANCELLED node's live task must be reaped, not left occupying slots."""
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("Cancel race", repo_path=str(tmp_path / "projects" / "cancel-race"))
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="a", objective="Task A", executor="deterministic"),
            NodeSpec(key="b", objective="Task B", executor="deterministic"),
        ]),
    )
    node_a, node_b = created
    cancelled_via_api = await store.set_status(node_a.id, NodeStatus.CANCELLED)
    assert cancelled_via_api is not None

    started: list[uuid.UUID] = []
    finished: list[uuid.UUID] = []
    cancel_calls: list[uuid.UUID] = []

    async def execute(node, _project_id):
        started.append(node.id)
        # The provider process is live even though the durable status already
        # says CANCELLED: the flip won the race against the launch boundary.
        await asyncio.sleep(5)
        finished.append(node.id)

    async def cancel_node(node_id):
        cancel_calls.append(node_id)
        task = scheduler.running.get(node_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await store.set_status(node_id, NodeStatus.CANCELLED)

    scheduler = Scheduler(
        store,
        Settings(max_parallel_agents=2),
        execute,
        lambda *_args: asyncio.sleep(0),
        lambda _root: asyncio.sleep(0),
        lambda: None,
        cancel_node=cancel_node,
    )

    # Simulate the cancel/launch race: the store says CANCELLED but a live
    # execution task was reserved before the status flip became visible.
    scheduler.reserve(await store.get_node(node_a.id), root.id)
    await asyncio.sleep(0.05)
    assert scheduler.active_node_ids()

    await scheduler.schedule_once(root.id)

    # The live task of the already-CANCELLED node was stopped through the one
    # cancellation path and no longer occupies a concurrency slot.
    assert cancel_calls == [node_a.id]
    assert node_a.id not in scheduler.active_node_ids()
    assert node_a.id not in finished
    await store.dispose()


@pytest.mark.asyncio
async def test_concurrent_claims_admit_exactly_one_winner(tmp_path):
    """claim_work_item is the assignment boundary: it must be atomic."""
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project("claim race", repo_path=str(tmp_path / "projects" / "claim-race"))
    item = await store.create_work_item(
        project_id=root.id,
        organization_id=root.id,
        key="contested",
        title="Contested ticket",
        objective="Do contested work",
    )

    results = await asyncio.gather(
        *(store.claim_work_item(item.id, node_id=root.id) for _ in range(8)),
        return_exceptions=True,
    )
    winners = [result for result in results if not isinstance(result, BaseException)]
    losers = [result for result in results if isinstance(result, BaseException)]
    assert len(winners) == 1, "exactly one concurrent claim may win"
    assert all(isinstance(loser, ValueError) for loser in losers)
    assert winners[0].status is WorkItemStatus.CLAIMED
    stored = (await store.list_work_items(root.id))[0]
    assert stored.status is WorkItemStatus.CLAIMED
    await store.dispose()
