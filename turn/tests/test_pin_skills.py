from __future__ import annotations

import pytest

from turn.domain.schemas import AgentConfig, AgentType
from turn.domain.skill_contracts import BUILTIN_SKILL_IDS
from turn.skills.library import SKILLS, validate_skill_reference


def test_pin_agent_role_skill_contract():
    assert AgentConfig(type_id=AgentType.PLANNER).skill_ids == [
        "turn-planning", "imagegen", "find-skills", "find-mcps",
    ]
    assert AgentConfig(type_id=AgentType.EXECUTOR).skill_ids == ["turn-executing"]
    assert AgentConfig(type_id=AgentType.INTEGRATOR).skill_ids == ["turn-integrating"]
    assert AgentConfig(type_id=AgentType.VERIFIER).skill_ids == ["turn-verifying"]


def test_pin_agent_as_type_replaces_role_defaults_but_preserves_custom_skills():
    agent = AgentConfig(
        type_id=AgentType.EXECUTOR,
        skill_ids=["project:custom-work"],
        session_id="session-1",
    )

    verifier = agent.as_type(AgentType.VERIFIER)

    assert verifier.type_id is AgentType.VERIFIER
    assert verifier.skill_ids == ["turn-verifying", "project:custom-work"]
    assert verifier.session_id == "session-1"


@pytest.mark.parametrize(
    "reference",
    ["turn-executing", "project:visual-qa", "https://example.test/skill/SKILL.md"],
)
def test_pin_skill_reference_validation_contract(reference: str):
    assert validate_skill_reference(reference) == reference


def test_pin_invalid_skill_reference_is_rejected():
    with pytest.raises(ValueError, match=r"built-in id, project:<slug>, or an http\(s\) URL"):
        validate_skill_reference("not-a-skill")


def test_pin_catalog_contains_every_builtin_role_skill():
    for skill_id in (
        "turn-planning", "imagegen", "find-skills", "find-mcps",
        "turn-executing", "turn-integrating", "turn-verifying",
    ):
        assert skill_id in SKILLS


def test_pin_skill_catalog_and_stable_ids_cannot_drift():
    assert set(SKILLS) == set(BUILTIN_SKILL_IDS)
