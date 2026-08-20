"""Domain-agnostic organization fitness checks.

The planner may propose a graph, but it does not get to be the sole judge of
whether the graph preserves the charter.  This module is intentionally pure so
it can run before persistence, in tests, in the CLI, and in the API.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from turn.domain.organization import (
    Handoff,
    HandoffStatus,
    OrganizationContract,
    OrganizationMetrics,
    OrganizationPhase,
    OrganizationScale,
    PlanAudit,
    WorkItem,
    WorkItemStatus,
)
from turn.domain.schemas import AgentType, Edge, EdgeType, Node, NodeSpec, PlanResult, Run
from turn.graph.logic import GraphWalker


_BROAD_LEAF_MARKERS = (
    # These are deliberately scope/ownership phrases, not industry or team
    # names.  A planner is allowed to name work in any domain; the audit only
    # flags a single vague owner claiming the whole material outcome.
    "entire",
    "everything",
    "all aspects",
    "full responsibility",
    "complete system",
    "whole result",
    "end to end",
    "end-to-end",
)


def _role(spec: NodeSpec) -> AgentType | None:
    if spec.agent_type is not None:
        return spec.agent_type
    if spec.agent is not None:
        return spec.agent.type_id
    if spec.plan or spec.executor == "planner":
        return AgentType.PLANNER
    return None


def _reaches(sequence: dict[str, set[str]], source: str, target: str) -> bool:
    frontier = [source]
    seen: set[str] = set()
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(sequence.get(current, set()) - seen)
    return False


def audit_plan(contract: OrganizationContract, plan: PlanResult) -> PlanAudit:
    """Audit one proposed composition against its organization's charter."""
    direct = [spec for spec in plan.nodes if spec.parent_key is None]
    roles = {spec.key: _role(spec) for spec in plan.nodes}
    planners = [spec for spec in direct if roles[spec.key] is AgentType.PLANNER]
    integrators = [spec for spec in direct if roles[spec.key] is AgentType.INTEGRATOR]
    verifiers = [spec for spec in direct if roles[spec.key] is AgentType.VERIFIER]
    production = [
        spec for spec in direct
        if roles[spec.key] not in {AgentType.INTEGRATOR, AgentType.VERIFIER}
    ]

    sequence: dict[str, set[str]] = {spec.key: set() for spec in direct}
    direct_keys = set(sequence)
    for spec in plan.nodes:
        if spec.key not in direct_keys:
            continue
        for predecessor in spec.follows:
            if predecessor in direct_keys:
                sequence[predecessor].add(spec.key)
    for edge in plan.edges:
        if edge.type is EdgeType.FOLLOWS and edge.src in direct_keys and edge.dst in direct_keys:
            sequence[edge.src].add(edge.dst)

    convergence_owner = next(
        (
            integrator
            for integrator in integrators
            if all(_reaches(sequence, owner.key, integrator.key) for owner in production)
        ),
        None,
    )
    # Composition is a property of the proposed ownership graph, not of a
    # domain label or of OrganizationScale.  A sequential chain can pass its
    # result forward without a synthetic integrator; independent branches
    # need an explicit convergence owner.
    has_parallel_production = any(
        not _reaches(sequence, left.key, right.key)
        and not _reaches(sequence, right.key, left.key)
        for index, left in enumerate(production)
        for right in production[index + 1 :]
    )
    composition_required = has_parallel_production
    terminal_production = [
        candidate
        for candidate in production
        if not any(
            _reaches(sequence, candidate.key, other.key)
            for other in production
            if other.key != candidate.key
        )
    ]
    verification_source = (
        convergence_owner.key
        if convergence_owner is not None
        else terminal_production[0].key
        if len(terminal_production) == 1
        else None
    )
    independent_verifier = next(
        (
            verifier
            for verifier in verifiers
            if verification_source is not None
            and _reaches(sequence, verification_source, verifier.key)
        ),
        None,
    )
    broad_executors = [
        spec for spec in production
        if roles[spec.key] is not AgentType.PLANNER
        and any(marker in spec.objective.casefold() for marker in _BROAD_LEAF_MARKERS)
    ]
    compression = len(broad_executors) / len(production) if production else 0.0
    errors: list[str] = []
    warnings: list[str] = []

    if contract.scale is not OrganizationScale.FOCUSED and not contract.acceptance_criteria:
        errors.append("material organization contract has no acceptance criteria")
    missing_nested_contracts = [
        spec.key for spec in planners if spec.organization_contract is None
    ]
    if missing_nested_contracts and contract.scale is not OrganizationScale.FOCUSED:
        # Store.apply_plan materializes a planner contract from its objective
        # when the provider omits one. This is a deterministic Turn guarantee,
        # so absence from the wire plan is advisory rather than a semantic
        # organization failure.
        warnings.append(
            "nested planner contracts will be materialized from their objectives: "
            + ", ".join(missing_nested_contracts)
        )

    if contract.scale is not OrganizationScale.FOCUSED:
        if not direct:
            errors.append("material contract has no first-level ownership")
        if len(production) < contract.min_first_level_production_owners:
            errors.append(
                "material contract exposes too few independently accountable "
                f"production owners ({len(production)} < {contract.min_first_level_production_owners})"
            )
    if composition_required and not integrators:
        errors.append(
            "independent first-level branches require an explicit convergence owner"
        )
    if composition_required and integrators and production and convergence_owner is None:
        errors.append(
            "all independent first-level branches must converge into one composition owner"
        )
    if contract.require_independent_verification and not verifiers:
        errors.append("the contract requires an independent evaluator")
    if contract.require_independent_verification and independent_verifier is None:
        errors.append("the independent evaluator must be downstream of the assembled result")
    if broad_executors and contract.scale is OrganizationScale.ORGANIZATION:
        errors.append(
            "broad first-level responsibilities are compressed into vague worker leaves: "
            + ", ".join(spec.key for spec in broad_executors)
        )

    if not direct and plan.nodes:
        errors.append("plan has no first-level nodes")
    checks = [
        bool(direct),
        len(production) >= contract.min_first_level_production_owners
        if contract.scale is not OrganizationScale.FOCUSED
        else True,
        (convergence_owner is not None and composition_required)
        if composition_required
        else True,
        bool(independent_verifier) if contract.require_independent_verification else True,
        not broad_executors
        if contract.scale is OrganizationScale.ORGANIZATION
        else True,
    ]
    score = max(0.0, min(1.0, sum(checks) / len(checks) - compression * 0.25))
    return PlanAudit(
        accepted=not errors,
        score=score,
        errors=errors,
        warnings=warnings,
        direct_node_count=len(direct),
        planner_count=len(planners),
        integrator_count=len(integrators),
        verifier_count=len(verifiers),
        production_owner_count=len(production),
        has_convergence=convergence_owner is not None,
        has_independent_verification=independent_verifier is not None,
        ownership_compression=compression,
    )


def audit_plan_structure(parent: Node | None, plan: PlanResult) -> PlanAudit:
    """Compatibility-shaped entry point for callers that have a parent node."""
    contract = (
        parent.organization_contract
        if parent is not None and parent.organization_contract is not None
        else plan.organization_contract
    )
    if contract is None:
        contract = OrganizationContract.from_objective(
            parent.objective if parent is not None else "focused work"
        )
    return audit_plan(contract, plan)


def audit_materialized_boundary(
    contract: OrganizationContract,
    boundary: Node,
    nodes: list[Node],
    edges,
) -> PlanAudit:
    """Audit a persisted boundary without manufacturing a new plan payload."""
    # Backlog tickets are ordinary execution nodes, but their presence should
    # not make a healthy architectural boundary fail the first-level
    # composition audit. Planner/integrator/verifier tickets remain visible to
    # the audit because they carry structural accountability of their own.
    owned = {
        node.id: node
        for node in nodes
        if node.parent_id == boundary.id
        and (
            node.work_item_id is None
            or (
                node.agent is not None
                and node.agent.type_id.value
                in {"planner", "integrator", "verifier"}
            )
        )
    }
    direct_specs = [
        NodeSpec(
            key=str(node.id),
            objective=node.objective,
            executor=node.executor,
            agent=node.agent,
            agent_type=node.agent.type_id if node.agent else None,
            plan=node.executor == "planner",
            organization_contract=node.organization_contract,
            acceptance_criteria=list(node.acceptance_criteria),
            exported_handoffs=list(node.exported_handoffs),
            required_handoffs=list(node.required_handoffs),
            priority=node.priority,
        )
        for node in owned.values()
    ]
    by_id = {node.id: node for node in nodes}
    for spec in direct_specs:
        node = by_id.get(uuid.UUID(spec.key))
        if node is None:
            continue
        spec.follows = [
            str(edge.src)
            for edge in edges
            if edge.type is EdgeType.FOLLOWS and edge.dst == node.id and edge.src in owned
        ]
    return audit_plan(contract, PlanResult(nodes=direct_specs))


def organization_metrics(
    nodes: list[Node],
    edges: list[Edge],
    *,
    work_items: list[WorkItem] | None = None,
    handoffs: list[Handoff] | None = None,
    runs: list[Run] | None = None,
) -> OrganizationMetrics:
    """Compute domain-neutral organization fitness signals from real state.

    These metrics are intentionally shape and evidence signals, not a product
    quality score.  They make the failure mode visible early: a large charter
    with depth one and broad executor ownership is structurally suspect even
    before a verifier rejects the shipped artifact.
    """
    walker = GraphWalker(nodes, edges)
    boundaries = [
        node for node in nodes
        if node.executor == "planner" and node.organization_contract is not None
    ]
    planners = [node for node in nodes if node.executor == "planner"]
    leaves = [node for node in nodes if node.id not in walker.indexes.children]
    production_leaves = [
        node for node in leaves
        if node.executor not in {"planner", "integrator", "verifier"}
    ]

    audits: list[PlanAudit] = []
    orphan_branches = 0
    for boundary in boundaries:
        audit = audit_materialized_boundary(boundary.organization_contract, boundary, nodes, edges)
        audits.append(audit)
        direct = [node for node in nodes if node.parent_id == boundary.id]
        direct_production = [
            node for node in direct
            if node.executor not in {"planner", "integrator", "verifier"}
        ]
        direct_integrators = [node for node in direct if node.executor == "integrator"]
        for owner in direct_production:
            if not any(
                _node_reaches(edges, owner.id, integrator.id)
                for integrator in direct_integrators
            ):
                orphan_branches += 1

    follows = [edge for edge in edges if edge.type is EdgeType.FOLLOWS]
    outgoing: dict[object, set[object]] = {}
    incoming: dict[object, set[object]] = {}
    for edge in follows:
        outgoing.setdefault(edge.src, set()).add(edge.dst)
        incoming.setdefault(edge.dst, set()).add(edge.src)
    fanout_nodes = {node_id for node_id, targets in outgoing.items() if len(targets) >= 2}
    convergence_nodes = {node_id for node_id, sources in incoming.items() if len(sources) >= 2}
    fanout_count = len(fanout_nodes)
    convergence_count = len(convergence_nodes)
    ratio = (
        min(fanout_count, convergence_count) / fanout_count
        if fanout_count
        else 0.0
    )
    compressions = [audit.ownership_compression for audit in audits]
    reviews = [node.organization_review for node in boundaries if node.organization_review]
    work = work_items or []
    links = handoffs or []
    observed_runs = runs or []
    spent = sum(run.usage.cost_usd or 0 for run in observed_runs)
    timeline: list[tuple[datetime, int]] = []
    now = datetime.now(timezone.utc)
    for run in observed_runs:
        started = run.started_at
        ended = run.ended_at or now
        timeline.append((started, 1))
        timeline.append((ended, -1))
    concurrency = 0
    peak_concurrency = 0
    for _, delta in sorted(timeline, key=lambda item: (item[0], -item[1])):
        concurrency += delta
        peak_concurrency = max(peak_concurrency, concurrency)
    return OrganizationMetrics(
        boundary_count=len(boundaries),
        planner_count=len(planners),
        max_depth=max((walker.depth(node.id) for node in nodes), default=0),
        production_leaf_count=len(production_leaves),
        planner_to_leaf_ratio=len(planners) / max(1, len(production_leaves)),
        max_ownership_compression=max(compressions, default=0.0),
        average_ownership_compression=sum(compressions) / len(compressions) if compressions else 0.0,
        converged_boundary_count=sum(audit.has_convergence for audit in audits),
        verified_boundary_count=sum(
            review.phase is OrganizationPhase.ACCEPT_CHARTER for review in reviews
        ),
        orphan_production_branches=orphan_branches,
        fanout_boundary_count=fanout_count,
        convergence_boundary_count=convergence_count,
        fanout_to_fanin_ratio=ratio,
        replan_count=sum(review.revision for review in reviews),
        work_item_count=len(work),
        completed_work_item_count=sum(item.status is WorkItemStatus.COMPLETE for item in work),
        handoff_count=len(links),
        accepted_handoff_count=sum(item.status is HandoffStatus.ACCEPTED for item in links),
        budget_spent_usd=spent,
        manager_iteration_count=sum(
            max(node.manager_iteration, review.review_count)
            for node, review in zip(
                [boundary for boundary in boundaries if boundary.organization_review],
                reviews,
                strict=True,
            )
        ),
        manager_accept_count=sum(review.accept_count for review in reviews),
        manager_continue_count=sum(review.continue_count for review in reviews),
        manager_block_count=sum(review.block_count for review in reviews),
        verifier_rejection_count=sum(
            node.verification is not None
            and node.verification.decision.value == "REJECT"
            for node in nodes
        ),
        open_work_item_count=sum(
            item.status not in {WorkItemStatus.COMPLETE, WorkItemStatus.CANCELLED}
            for item in work
        ),
        active_work_item_count=sum(
            item.status in {
                WorkItemStatus.ACTIVE,
                WorkItemStatus.CLAIMED,
                WorkItemStatus.RUNNING,
            }
            for item in work
        ),
        peak_concurrency=peak_concurrency,
    )


def _node_reaches(edges: list[Edge], source, target) -> bool:
    """Return whether a local FOLLOWS path connects two materialized nodes."""
    adjacency: dict[object, set[object]] = {}
    for edge in edges:
        if edge.type is EdgeType.FOLLOWS:
            adjacency.setdefault(edge.src, set()).add(edge.dst)
    frontier = [source]
    seen: set[object] = set()
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(adjacency.get(current, set()) - seen)
    return False
