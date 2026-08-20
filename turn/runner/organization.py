"""Persistent manager-loop policy for planner boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from turn.contracts.organization import audit_materialized_boundary
from turn.domain.organization import (
    HandoffStatus,
    ManagerDecision,
    ManagerPhase,
    OrganizationContract,
    OrganizationPhase,
    OrganizationReview,
    WorkItemSpec,
    WorkItemStatus,
)
from turn.domain.schemas import (
    InputSpec,
    ManagerResult,
    Node,
    NodeStatus,
    PlanResult,
    VerificationDecision,
)
from turn.graph.logic import GraphWalker

if TYPE_CHECKING:
    from turn.db.store import Store


@dataclass(frozen=True)
class ManagerReviewDecision:
    node_id: object
    phase: OrganizationPhase
    replan: bool
    reason: str
    decision: ManagerDecision


    @property
    def manager_phase(self) -> ManagerPhase:
        return {
            OrganizationPhase.PLAN: ManagerPhase.PLANNING,
            OrganizationPhase.EXECUTE_FRONTIER: ManagerPhase.EXECUTING,
            OrganizationPhase.OBSERVE: ManagerPhase.REVIEWING,
            OrganizationPhase.REVIEW: ManagerPhase.REVIEWING,
            OrganizationPhase.ACCEPT_CHARTER: ManagerPhase.ACCEPTED,
            OrganizationPhase.BLOCKED: ManagerPhase.BLOCKED,
            OrganizationPhase.REPLAN: ManagerPhase.REVIEW_PENDING,
        }.get(self.phase, ManagerPhase.REVIEWING)


class OrganizationManager:
    """Review a boundary after meaningful frontier changes.

    This first manager loop is deliberately provider-neutral. It does not
    pretend that a static shape check can judge product quality; it decides
    whether the charter has enough structural and acceptance evidence to hand
    control upward, or whether the retained planner must receive another turn.
    """

    async def request_review(
        self, store: "Store", boundary_id, reason: str
    ) -> None:
        """Coalesce a meaningful management event for a safe-point review."""
        boundary = await store.get_node(boundary_id)
        if boundary is None or boundary.organization_contract is None:
            return
        reasons = list(dict.fromkeys([*boundary.manager_review_reasons, reason]))
        await store.set_manager_state(
            boundary.id,
            phase=ManagerPhase.REVIEW_PENDING,
            reasons=reasons,
        )

    async def snapshot(self, store: "Store", boundary_id) -> dict:
        """Build the compact management envelope for a retained planner turn."""
        boundary = await store.get_node(boundary_id)
        if boundary is None:
            raise ValueError(f"unknown organization boundary: {boundary_id}")
        nodes, edges, artifacts = await store.get_workgraph(boundary.project_id)
        graph = GraphWalker(nodes, edges)
        descendants = graph.descendants(boundary.id)
        work_items = await store.list_work_items(
            boundary.project_id, organization_id=boundary.id
        )
        runs = await store.get_project_runs(boundary.project_id)
        owned_ids = {node.id for node in descendants}
        return {
            "contract": boundary.organization_contract.model_dump(mode="json")
            if boundary.organization_contract
            else None,
            "boundary": boundary.model_dump(mode="json"),
            "descendants": [node.model_dump(mode="json") for node in descendants],
            "completed": [
                node.model_dump(mode="json")
                for node in descendants
                if node.status is NodeStatus.COMPLETE
            ],
            "failed_or_blocked": [
                node.model_dump(mode="json")
                for node in descendants
                if node.status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
            ],
            "artifacts": [
                artifact.model_dump(mode="json")
                for artifact in artifacts
                if artifact.node_id in owned_ids
            ],
            "work_items": [item.model_dump(mode="json") for item in work_items],
            "budget_consumption": {
                "input_tokens": sum(
                    run.usage.input_tokens
                    for run in runs
                    if run.node_id in owned_ids
                ),
                "output_tokens": sum(
                    run.usage.output_tokens
                    for run in runs
                    if run.node_id in owned_ids
                ),
                "cost_usd": sum(
                    run.usage.cost_usd or 0
                    for run in runs
                    if run.node_id in owned_ids
                ),
            },
            "review_reasons": list(boundary.manager_review_reasons),
        }

    async def apply_result(
        self, store: "Store", boundary_id, result: ManagerResult
    ) -> ManagerReviewDecision:
        """Apply a provider manager result through the ordinary Store paths."""
        boundary = await store.get_node(boundary_id)
        if boundary is None or boundary.organization_contract is None:
            raise ValueError("manager result requires an organization boundary")
        if result.decision is ManagerDecision.ACCEPT:
            decision = await self.review(store, boundary_id)
            if decision is None:
                raise ValueError("manager review could not be evaluated")
            return decision
        if result.decision is ManagerDecision.CONTINUE:
            if result.plan is not None:
                raise ValueError(
                    "manager CONTINUE accepts bounded work_items only; "
                    "graph changes require a retained planner turn"
                )
            if not result.work_items:
                raise ValueError("manager CONTINUE requires new work items")
            existing = await store.list_work_items(
                boundary.project_id, organization_id=boundary.id
            )
            by_key = {item.key: item.id for item in existing if item.key}
            duplicate_keys = [
                spec.key for spec in result.work_items if spec.key in by_key
            ]
            if len({spec.key for spec in result.work_items}) != len(result.work_items):
                raise ValueError("manager work-item keys must be unique")
            if duplicate_keys:
                raise ValueError(
                    "manager work-item keys already exist: "
                    + ", ".join(duplicate_keys)
                )
            batch_keys = {spec.key for spec in result.work_items}
            known_keys = set(by_key)
            unknown_dependencies = sorted(
                {
                    dependency
                    for spec in result.work_items
                    for dependency in spec.depends_on
                    if dependency not in known_keys and dependency not in batch_keys
                }
            )
            if unknown_dependencies:
                raise ValueError(
                    "manager work-item dependencies are unknown: "
                    + ", ".join(unknown_dependencies)
                )
            pending_specs = list(result.work_items)
            ordered_specs: list[WorkItemSpec] = []
            available_keys = set(by_key)
            while pending_specs:
                ready = [
                    spec
                    for spec in pending_specs
                    if all(dependency in available_keys for dependency in spec.depends_on)
                ]
                if not ready:
                    raise ValueError("manager work-item dependencies contain a cycle")
                for spec in ready:
                    pending_specs.remove(spec)
                    ordered_specs.append(spec)
                    available_keys.add(spec.key)
            created_specs: list[tuple[WorkItemSpec, object]] = []
            for spec in ordered_specs:
                dependency_ids = [by_key[key] for key in spec.depends_on]
                item = await store.create_work_item(
                    project_id=boundary.project_id,
                    organization_id=boundary.id,
                    key=spec.key,
                    title=spec.title,
                    objective=spec.instructions,
                    acceptance_criteria=spec.acceptance_criteria,
                    priority=spec.priority,
                    depends_on=dependency_ids,
                    agent_type=spec.agent_type,
                    organization_contract=spec.organization_contract,
                )
                by_key[spec.key] = item.id
                created_specs.append((spec, item.id))
            review = boundary.organization_review or OrganizationReview()
            review.review_count += 1
            review.phase = OrganizationPhase.EXECUTE_FRONTIER
            review.replan_requested = False
            review.last_reason = result.summary
            review.last_decision = ManagerDecision.CONTINUE
            review.continue_count += 1
            await store.set_organization_review(boundary.id, review)
            await store.set_status(boundary.id, NodeStatus.EXPANDED)
            await store.set_manager_state(
                boundary.id,
                phase=ManagerPhase.EXECUTING,
                iteration=boundary.manager_iteration + 1,
                reasons=[result.summary],
            )
            return ManagerReviewDecision(
                boundary.id,
                OrganizationPhase.EXECUTE_FRONTIER,
                False,
                result.summary,
                ManagerDecision.CONTINUE,
            )
        review = boundary.organization_review or OrganizationReview()
        review.review_count += 1
        if result.missing_inputs:
            await store.set_required_inputs(
                boundary.id, result.missing_inputs, merge=True
            )
        review.phase = OrganizationPhase.BLOCKED
        review.replan_requested = False
        review.last_reason = result.summary
        review.last_decision = ManagerDecision.BLOCK
        review.block_count += 1
        await store.set_organization_review(boundary.id, review)
        await store.set_manager_state(
            boundary.id,
            phase=ManagerPhase.BLOCKED,
            reasons=[result.summary],
        )
        await store.set_status(boundary.id, NodeStatus.BLOCKED)
        return ManagerReviewDecision(
            boundary.id,
            OrganizationPhase.BLOCKED,
            False,
            result.summary,
            ManagerDecision.BLOCK,
        )

    async def review(self, store: "Store", boundary_id) -> ManagerReviewDecision | None:
        boundary = await store.get_node(boundary_id)
        if boundary is None or boundary.organization_contract is None:
            return None
        await store.set_manager_state(
            boundary.id,
            phase=ManagerPhase.REVIEWING,
            iteration=boundary.manager_iteration + 1,
            reasons=boundary.manager_review_reasons,
        )
        boundary = await store.get_node(boundary.id) or boundary
        nodes, edges, _ = await store.get_workgraph(boundary.project_id)
        graph = GraphWalker(nodes, edges)
        descendants = graph.descendants(boundary.id)
        work_items = await store.list_work_items(
            boundary.project_id,
            organization_id=boundary.id,
        )
        if not descendants and not work_items:
            review = boundary.organization_review or OrganizationReview()
            review.review_count += 1
            review.block_count += 1
            review.last_decision = ManagerDecision.BLOCK
            review.phase = OrganizationPhase.BLOCKED
            review.last_reason = "organization has no executable frontier"
            await store.set_organization_review(boundary.id, review)
            await store.set_manager_state(
                boundary.id,
                phase=ManagerPhase.BLOCKED,
                reasons=["organization has no executable frontier"],
            )
            await store.set_status(boundary.id, NodeStatus.BLOCKED)
            return ManagerReviewDecision(
                boundary.id,
                OrganizationPhase.BLOCKED,
                False,
                "organization has no executable frontier",
                ManagerDecision.BLOCK,
            )
        leaves = [node for node in descendants if node.id not in graph.indexes.children]
        settled = all(
            node.status in {
                NodeStatus.COMPLETE,
                NodeStatus.FAILED,
                NodeStatus.BLOCKED,
                NodeStatus.CANCELLED,
            }
            for node in leaves
        )
        audit = audit_materialized_boundary(
            boundary.organization_contract,
            boundary,
            nodes,
            edges,
        )
        review = boundary.organization_review or OrganizationReview()
        review.review_count += 1
        review.audit = audit
        review.reviewed_at = datetime.now(timezone.utc)
        if not settled:
            review.phase = OrganizationPhase.EXECUTE_FRONTIER
            review.replan_requested = False
            review.last_reason = "frontier still has active or blocked work"
            review.continue_count += 1
            review.last_decision = ManagerDecision.CONTINUE
            await store.set_organization_review(boundary.id, review)
            await store.set_manager_state(
                boundary.id,
                phase=ManagerPhase.EXECUTING,
                reasons=boundary.manager_review_reasons,
            )
            return ManagerReviewDecision(
                boundary.id,
                review.phase,
                False,
                review.last_reason,
                ManagerDecision.CONTINUE,
            )

        direct_verifiers = [
            node
            for node in descendants
            if node.parent_id == boundary.id
            and node.agent is not None
            and node.agent.type_id.value == "verifier"
        ]
        verified = any(
            node.verification is not None
            and node.verification.decision is VerificationDecision.APPROVE
            for node in direct_verifiers
        )
        terminal_failures = [
            node for node in leaves
            if node.status in {NodeStatus.FAILED, NodeStatus.BLOCKED}
        ]
        reasons = list(audit.errors)
        unresolved_inputs = [
            item.id
            for node in [boundary, *descendants]
            for item in node.required_inputs
            if item.satisfied_by is None
        ]
        if unresolved_inputs:
            reasons.append(
                "unresolved required inputs: " + ", ".join(unresolved_inputs)
            )
        if terminal_failures:
            reasons.append(
                "terminal work items failed: "
                + ", ".join(node.objective for node in terminal_failures)
            )
        if boundary.organization_contract.require_independent_verification and not verified:
            reasons.append("independent acceptance evidence is missing")
        if boundary.organization_contract.require_independent_verification:
            approvals_without_evidence = [
                node.objective
                for node in direct_verifiers
                if node.verification is not None
                and node.verification.decision is VerificationDecision.APPROVE
                and not node.verification.evidence_refs
            ]
            if approvals_without_evidence:
                reasons.append(
                    "independent approvals must cite evidence: "
                    + ", ".join(approvals_without_evidence)
                )

        incomplete_items = [
            item for item in work_items
            if item.status not in {WorkItemStatus.COMPLETE, WorkItemStatus.CANCELLED}
        ]
        if incomplete_items:
            reasons.append(
                "unfinished backlog: "
                + ", ".join(item.title for item in incomplete_items)
            )
        required_integrators = [
            node
            for node in descendants
            if node.parent_id == boundary.id and node.executor == "integrator"
        ]
        if required_integrators and not any(
            node.status is NodeStatus.COMPLETE for node in required_integrators
        ):
            reasons.append("required integration has not completed")

        # Evidence artifacts are the durable bridge between a worker claim and
        # manager acceptance. An approved direct verifier with explicit refs is
        # the release-level authority for contract criteria. Descendant
        # evidence remains useful supporting material, but cannot satisfy the
        # organization's acceptance contract by itself.
        if boundary.organization_contract.acceptance_criteria:
            required_criterion_ids = {
                criterion.id
                for criterion in boundary.organization_contract.acceptance_criteria
            }
            passing_criterion_ids = {
                evidence.criterion_id
                for node in direct_verifiers
                if node.verification is not None
                for evidence in node.verification.evidence
                if evidence.status.value == "PASS"
            }
            missing_criterion_ids = required_criterion_ids - passing_criterion_ids
            if missing_criterion_ids:
                reasons.append(
                    "contract acceptance criteria have no passing evidence for: "
                    + ", ".join(sorted(missing_criterion_ids))
                )

        owned_ids = {node.id for node in descendants}
        owned_handoffs = [
            handoff
            for handoff in await store.list_handoffs(boundary.project_id)
            if handoff.producer_node_id in owned_ids
            and handoff.consumer_node_id in owned_ids
        ]
        for handoff in owned_handoffs:
            if handoff.status is not HandoffStatus.ACCEPTED:
                reasons.append(
                    f"handoff {handoff.contract.name} is {handoff.status.value.lower()}"
                )
            elif handoff.contract.evidence_required and not handoff.evidence_refs:
                reasons.append(
                    f"handoff {handoff.contract.name} has no acceptance evidence"
                )

        if reasons:
            if review.revision < boundary.organization_contract.max_replans:
                review.revision += 1
                review.phase = OrganizationPhase.REPLAN
                review.replan_requested = True
                decision = ManagerDecision.CONTINUE
                review.continue_count += 1
            else:
                review.phase = OrganizationPhase.BLOCKED
                review.replan_requested = False
                decision = ManagerDecision.BLOCK
                review.block_count += 1
            review.last_reason = "; ".join(reasons)
        else:
            review.phase = OrganizationPhase.ACCEPT_CHARTER
            review.replan_requested = False
            review.last_reason = "charter accepted with structural and verification evidence"
            decision = ManagerDecision.ACCEPT
            review.accept_count += 1
        review.last_decision = decision
        await store.set_organization_review(boundary.id, review)
        manager_phase = {
            OrganizationPhase.ACCEPT_CHARTER: ManagerPhase.ACCEPTED,
            OrganizationPhase.REPLAN: ManagerPhase.REVIEW_PENDING,
            OrganizationPhase.BLOCKED: ManagerPhase.BLOCKED,
        }[review.phase]
        await store.set_manager_state(
            boundary.id,
            phase=manager_phase,
            reasons=reasons,
        )
        if decision is ManagerDecision.ACCEPT:
            await store.set_status(boundary.id, NodeStatus.COMPLETE)
        elif decision is ManagerDecision.BLOCK:
            await store.set_status(boundary.id, NodeStatus.BLOCKED)
        return ManagerReviewDecision(
            boundary.id,
            review.phase,
            review.replan_requested,
            review.last_reason,
            decision,
        )
