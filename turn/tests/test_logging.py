import json
import uuid

import pytest

from turn.db.store import Store
from turn.logging import EventLog


def test_event_log_rotates_and_stitches_jsonl(tmp_path):
    project_id = uuid.uuid4()
    log = EventLog(tmp_path, max_records=2)
    for index in range(5):
        log.emit_sync(project_id, kind="test.event", message=f"record {index}", data={"index": index})

    files = sorted((tmp_path / "logs").glob(f"project-{project_id}-*.jsonl"))
    assert len(files) == 3
    assert [item["data"]["index"] for item in log.read(project_id)] == list(range(5))
    assert all(json.loads(line)["project_id"] == str(project_id) for path in files for line in path.read_text().splitlines())


def test_project_events_use_the_project_dot_turn_log_directory(tmp_path):
    project_id = uuid.uuid4()
    project_root = tmp_path / "project"
    log = EventLog(tmp_path / "workspace", max_records=2)
    log.bind_project(project_id, project_root)

    log.emit_sync(project_id, kind="project.created")

    assert list((project_root / ".turn" / "logs").glob("*.jsonl"))
    assert not (tmp_path / "workspace" / "logs").exists()
    assert len(log.read(project_id)) == 1


@pytest.mark.asyncio
async def test_store_records_project_state_and_session_decisions(tmp_path):
    log = EventLog(tmp_path / "turn", max_records=100)
    store = Store(tmp_path / "turn", logs=log)
    await store.init()
    root = await store.create_project("instrument me", repo_path=str(tmp_path / "project"), id=uuid.uuid4())
    await store.set_agent_session(root.id, "session-1")
    await store.clear_agent_session(root.id)
    await store.set_setting("example", "value")

    records = log.read(root.project_id)
    kinds = {item["kind"] for item in records}
    assert "project.created" in kinds
    assert "decision.session" in kinds
    assert "configuration.changed" in {item["kind"] for item in log.read(None)}
