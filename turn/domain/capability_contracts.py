"""Stable capability identifiers owned by the domain layer."""
from __future__ import annotations

import re


SETUP_CAPABILITY_ID = "turn-setup"
AUTHORING_CAPABILITY_ID = "turn-authoring-capabilities"
BASICS_CAPABILITY_ID = "turn-basics"

ROLE_CAPABILITY_IDS: dict[str, tuple[str, ...]] = {
    "planner": (
        BASICS_CAPABILITY_ID,
        "turn-planning",
        AUTHORING_CAPABILITY_ID,
    ),
    "executor": (BASICS_CAPABILITY_ID, "turn-executing"),
    "integrator": (BASICS_CAPABILITY_ID, "turn-integrating"),
    "verifier": (BASICS_CAPABILITY_ID, "turn-verifying"),
    # The lead oversees the hierarchy: it reuses planning foundations so it
    # can read organization contracts and propose corrections, without a
    # separate capability product.
    "lead": (
        BASICS_CAPABILITY_ID,
        "turn-planning",
        AUTHORING_CAPABILITY_ID,
    ),
}

# ``turn-setup`` is attached only to the root setup planner, but it is still a
# Turn-owned role capability. It must not become a custom capability when an
# agent is converted to another role or when a parent configuration cascades
# to descendants.
BUILTIN_CAPABILITY_IDS = frozenset({
    BASICS_CAPABILITY_ID,
    SETUP_CAPABILITY_ID,
    *(capability_id for ids in ROLE_CAPABILITY_IDS.values() for capability_id in ids),
})

_CAPABILITY_ID = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")


def _agent_type_key(agent_type: object) -> str:
    return str(getattr(agent_type, "value", agent_type))


def capability_ids_for_agent_type(agent_type: object) -> list[str]:
    return list(ROLE_CAPABILITY_IDS.get(_agent_type_key(agent_type), ()))


def validate_capability_id(capability_id: str) -> str:
    if not isinstance(capability_id, str) or not _CAPABILITY_ID.fullmatch(capability_id):
        raise ValueError(f"invalid capability plugin id: {capability_id!r}")
    return capability_id
