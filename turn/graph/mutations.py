"""Pure graph-state mutations for applying a validated plan."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    Artifact,
    ArtifactSpec,
    Edge,
    EdgeType,
    Node,
    NodeStatus,
    PlanResult,
)

PLANNER_EXECUTOR = "planner"


def merge_document_refs(existing, incoming):
    merged = []
    seen: set[str] = set()
    for ref in [*existing, *incoming]:
        if ref.ref in seen:
            continue
        seen.add(ref.ref)
        merged.append(ref)
    return merged


def append_artifacts(state: Any, node_id: uuid.UUID, specs: list[ArtifactSpec]) -> list[Artifact]:
    """Append non-duplicate artifacts to a ProjectState-like aggregate."""
    existing = {
        ("ref", artifact.ref) if artifact.ref else
        ("value", artifact.kind, artifact.name, json.dumps(artifact.content, sort_keys=True, default=str))
        for artifact in state.artifacts.values()
        if artifact.node_id == node_id
    }
    artifacts: list[Artifact] = []
    for spec in specs:
        key = (
            ("ref", spec.ref) if spec.ref else
            ("value", spec.kind, spec.name, json.dumps(spec.content, sort_keys=True, default=str))
        )
        if key in existing:
            continue
        artifact = Artifact(
            id=uuid.uuid4(),
            node_id=node_id,
            kind=spec.kind,
            name=spec.name,
            content=spec.content,
            ref=spec.ref,
        )
        state.artifacts[artifact.id] = artifact
        existing.add(key)
        artifacts.append(artifact)
    node = state.nodes.get(node_id)
    if node is not None and artifacts:
        node.artifact_refs = [*node.artifact_refs, *(artifact.id for artifact in artifacts)]
    return artifacts


def apply_plan(state: Any, parent: Node, plan: PlanResult) -> list[Node]:
    """Apply a validated PlanResult to a ProjectState-like aggregate."""
    parent.document_refs = merge_document_refs(parent.document_refs, plan.document_refs)
    if parent.parent_id is None and parent.project_name is None and plan.project_name:
        candidate_name = plan.project_name.strip()
        if candidate_name:
            parent.project_name = candidate_name
            parent.objective = candidate_name
    if not plan.nodes:
        parent.status = NodeStatus.COMPLETE
        state.nodes[parent.id] = parent.model_copy(deep=True)
        append_artifacts(state, parent.id, plan.artifacts)
        return []

    keys_to_ids = {spec.key: uuid.uuid4() for spec in plan.nodes}
    edge_keys = {(edge.src, edge.dst, edge.type) for edge in state.edges.values()}

    def add_edge(src: uuid.UUID, dst: uuid.UUID, edge_type: EdgeType) -> None:
        key = (src, dst, edge_type)
        if key in edge_keys:
            return
        edge = Edge(src=src, dst=dst, type=edge_type)
        state.edges[edge.id] = edge
        edge_keys.add(key)

    created: list[Node] = []
    for spec in plan.nodes:
        node_id = keys_to_ids[spec.key]
        node_parent_id = keys_to_ids[spec.parent_key] if spec.parent_key else parent.id
        executor = (
            PLANNER_EXECUTOR
            if (spec.plan or spec.executor == PLANNER_EXECUTOR)
            else (spec.executor or "codex")
        )
        inherited_agent = parent.agent.model_copy(deep=True) if parent.agent else None
        if inherited_agent:
            inherited_agent.capabilities = [
                capability_id
                for capability_id in inherited_agent.capabilities
                if capability_id != "turn-setup"
            ]
        generic_leaf = executor != PLANNER_EXECUTOR and executor == "codex"
        if generic_leaf and inherited_agent:
            executor = inherited_agent.harness.value
        if spec.agent:
            node_agent = spec.agent.model_copy(deep=True)
        elif executor == PLANNER_EXECUTOR:
            node_agent = inherited_agent or AgentConfig(type_id="planner")
        elif generic_leaf and inherited_agent:
            node_agent = inherited_agent
        elif inherited_agent and executor == inherited_agent.harness.value:
            node_agent = inherited_agent
        elif executor in {"codex", "claude", "opencode", "pi", "echo", "shell"}:
            node_agent = AgentConfig(harness=executor)
        else:
            node_agent = inherited_agent or AgentConfig()
        node_agent.session_id = None
        requested_agent_type = spec.agent_type or (
            spec.agent.type_id if spec.agent is not None else None
        )
        if requested_agent_type is not None:
            node_agent = node_agent.as_type(requested_agent_type)
        elif not spec.agent:
            node_agent = node_agent.as_type(
                AgentType.PLANNER if executor == PLANNER_EXECUTOR else AgentType.EXECUTOR
            )
        node_agent.capabilities = list(dict.fromkeys([
            *node_agent.capabilities,
            *spec.capabilities,
        ]))
        node = Node(
            id=node_id,
            project_id=parent.project_id,
            parent_id=node_parent_id,
            objective=spec.objective,
            generated_prompt=spec.generated_prompt,
            executor=executor,
            agent=node_agent,
            status=NodeStatus.PENDING,
            required_inputs=spec.required_inputs,
            resource_refs=spec.resource_refs,
            document_refs=spec.document_refs,
        )
        state.nodes[node.id] = node
        append_artifacts(state, node.id, spec.artifacts)
        created.append(node)
        if not spec.parent_key:
            add_edge(parent.id, node.id, EdgeType.CONTAINS)
    for spec in plan.nodes:
        if spec.parent_key:
            add_edge(keys_to_ids[spec.parent_key], keys_to_ids[spec.key], EdgeType.CONTAINS)
        for dep in spec.depends_on:
            add_edge(keys_to_ids[dep], keys_to_ids[spec.key], EdgeType.DEPENDS_ON)
    for item in plan.edges:
        add_edge(keys_to_ids[item.src], keys_to_ids[item.dst], item.type)
    parent.status = NodeStatus.EXPANDED
    state.nodes[parent.id] = parent.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
    append_artifacts(state, parent.id, plan.artifacts)
    return [node.model_copy(deep=True) for node in created]
