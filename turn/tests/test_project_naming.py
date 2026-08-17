from __future__ import annotations

from turn.db.store import Store
from turn.domain.schemas import NodeSpec, PlanResult


async def test_setup_plan_name_becomes_the_persisted_root_objective(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    prompt = "An app factory request with the complete user-authored scope"
    root = await store.create_project(prompt, repo_path=str(tmp_path / "repo"))

    await store.apply_plan(
        root,
        PlanResult(
            project_name="First App Factory",
            nodes=[NodeSpec(key="research", objective="Research the first app", executor="echo")],
        ),
    )

    saved = await store.get_node(root.id)
    assert saved is not None
    assert saved.objective == "First App Factory"
    assert saved.project_name == "First App Factory"
    assert saved.generated_prompt == prompt
