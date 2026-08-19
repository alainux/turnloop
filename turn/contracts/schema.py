"""Public generated-schema source for the web client contract."""
from __future__ import annotations

from turn.domain.schemas import (
    Agent,
    Artifact,
    ArtifactSpec,
    CapabilityStatus,
    Edge,
    EdgeSpec,
    FlowEdge,
    Executor,
    Graph,
    GraphNodeView,
    GraphView,
    Trigger,
    TriggerContext,
    TriggerSpec,
    Integrator,
    InputSpec,
    Node,
    NodeSpec,
    Planner,
    Verifier,
    VerificationResult,
    PlanResult,
    Run,
    RunPolicy,
    WorkerResult,
)


PUBLIC_DOMAIN_MODELS = {
    "Agent": Agent,
    "CapabilityStatus": CapabilityStatus,
    "Planner": Planner,
    "Executor": Executor,
    "Integrator": Integrator,
    "Verifier": Verifier,
    "VerificationResult": VerificationResult,
    "Node": Node,
    "NodeSpec": NodeSpec,
    "Edge": Edge,
    "EdgeSpec": EdgeSpec,
    "FlowEdge": FlowEdge,
    "InputSpec": InputSpec,
    "RunPolicy": RunPolicy,
    "Artifact": Artifact,
    "ArtifactSpec": ArtifactSpec,
    "Graph": Graph,
    "GraphNodeView": GraphNodeView,
    "GraphView": GraphView,
    "Trigger": Trigger,
    "TriggerContext": TriggerContext,
    "TriggerSpec": TriggerSpec,
    "Run": Run,
    "PlanResult": PlanResult,
    "WorkerResult": WorkerResult,
}


def public_schema() -> dict[str, object]:
    """Return the authoritative JSON Schema document served to clients."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Turn domain contract",
        "models": {
            name: model.model_json_schema(ref_template="#/$defs/{model}")
            for name, model in PUBLIC_DOMAIN_MODELS.items()
        },
    }
