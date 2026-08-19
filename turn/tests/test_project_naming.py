from __future__ import annotations

from turn.db.store import Store
from turn.domain.schemas import NodeSpec, PlanResult
import json


async def test_setup_plan_name_becomes_the_persisted_root_objective(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    prompt = "An app factory request with the complete user-authored scope"
    root = await store.create_project(prompt, repo_path=str(tmp_path / "repo"))

    await store.apply_plan(
        root,
        PlanResult(
            project_name="First App Factory",
            nodes=[NodeSpec(key="research", objective="Research the first app", executor="deterministic")],
        ),
    )

    saved = await store.get_node(root.id)
    assert saved is not None
    assert saved.objective == "First App Factory"
    assert saved.project_name == "First App Factory"
    assert saved.generated_prompt == prompt


async def test_state_load_migrates_prompt_sized_node_labels(tmp_path):
    data_dir = tmp_path / "turn"
    projects_dir = tmp_path / "projects"
    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project("Migration project")
    state_path = data_dir / "projects" / f"proj-{root.id.hex[:8]}" / ".turn" / "state.json"
    payload = json.loads(state_path.read_text())
    long_label = "Establish the implementation-ready foundation and contracts for later workstreams."
    payload["nodes"][0]["objective"] = long_label
    payload["nodes"][0]["generated_prompt"] = None
    state_path.write_text(json.dumps(payload))

    reloaded = Store(data_dir, projects_dir=projects_dir)
    await reloaded.init()
    migrated = await reloaded.get_node(root.id)

    assert migrated is not None
    assert len(migrated.objective) <= 72
    assert migrated.objective.endswith("…")
    assert migrated.generated_prompt == long_label


async def test_node_edit_rejects_prompt_sized_labels(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    root = await store.create_project("Editable project")

    try:
        await store.edit_node(root.id, objective="x" * 73)
    except ValueError as error:
        assert "at most 72 characters" in str(error)
    else:
        raise AssertionError("prompt-sized node labels must be rejected")
