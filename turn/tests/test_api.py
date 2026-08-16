from __future__ import annotations

import uuid
import base64
import json
from pathlib import Path

import httpx
from fastapi import FastAPI

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import Outcome, RunStatus, Usage
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.server.api import router
from turn.workers.echo_worker import EchoWorker
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry
from turn.tests.fakes import FakeHerdrAdapter


async def test_api_exposes_state_actions_policy_capabilities_and_usage(tmp_path):
    cfg = Settings()
    cfg.projects_dir = str(tmp_path / "projects")
    cfg.default_executor = "echo"
    store = Store(tmp_path / "turn")
    await store.init()
    registry = WorkerRegistry()
    registry.register(EchoWorker())
    registry.register_planner(HeuristicPlanner("echo"))
    runner = Runner(store, registry, EventBus(), cfg, herdr_adapter=FakeHerdrAdapter())
    app = FastAPI()
    app.include_router(router)
    app.state.store = store
    app.state.runner = runner
    app.state.events = runner.events
    app.state.test_mode = True
    from turn.workers.harnesses import harness_capabilities
    app.state.capabilities = harness_capabilities()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        caps = (await client.get("/api/capabilities")).json()
        assert {h["id"] for h in caps["harnesses"]} == {"codex", "claude", "opencode", "pi"}
        assert all(h["reasoning_profiles"] for h in caps["harnesses"])
        assert all(item["future"] for item in caps["output_types"])
        settings_payload = (await client.get("/api/settings")).json()
        assert set(settings_payload["agent_defaults"]) == {"planner", "executor", "integrator", "verifier"}
        original_defaults = json.loads(json.dumps(settings_payload["agent_defaults"]))
        changed_defaults = json.loads(json.dumps(original_defaults))
        changed_defaults["planner"]["model"] = "planner-test-model"
        changed_defaults["executor"]["model"] = "executor-test-model"
        changed_defaults["integrator"]["model"] = "integrator-test-model"
        changed_defaults["verifier"]["model"] = "verifier-test-model"
        updated = await client.post("/api/settings", json={"agent_defaults": changed_defaults})
        assert updated.status_code == 200, updated.text
        stored_defaults = (await client.get("/api/settings")).json()["agent_defaults"]
        assert stored_defaults["planner"]["model"] == "planner-test-model"
        assert stored_defaults["executor"]["model"] == "executor-test-model"
        assert stored_defaults["integrator"]["model"] == "integrator-test-model"
        assert stored_defaults["verifier"]["model"] == "verifier-test-model"
        invalid = await client.post("/api/projects", json={
            "prompt": "Must not create a repository",
            "agent": {"harness": "codex", "model": "fast-mini", "reasoning": "xhigh"},
        })
        assert invalid.status_code == 422
        assert (await client.get("/api/projects")).json()["projects"] == []
        created = await client.post("/api/projects", json={
            "name": "Inspectable demo",
            "prompt": "Build an inspectable demo",
            "agent": {"harness": "echo", "type_id": "executor"},
            "run_policy": {"auto_run": False, "delay_between_jobs_ms": 25},
            "attachments": [
                {"name": "brief.txt", "mime": "text/plain", "content_base64": base64.b64encode(b"immutable project context").decode()},
                {"name": "brief.txt", "mime": "text/plain", "content_base64": base64.b64encode(b"second copy").decode()},
            ],
        })
        assert created.status_code == 200, created.text
        pid = created.json()["project_id"]
        graph = (await client.get(f"/api/projects/{pid}/graph")).json()
        root = graph["nodes"][0]
        assert root["project_name"] == "Inspectable demo"
        assert root["objective"] == "Inspectable demo"
        assert root["generated_prompt"] == "Build an inspectable demo"
        assert root["ui_state"] == "ready"
        assert "run" in root["allowed_actions"]
        assert root["agent"]["harness"] == "echo"
        assert root["agent"]["type_id"] == "planner"
        assert len(root["resource_refs"]) == 2
        assert {Path(ref).name for ref in root["resource_refs"]} == {"brief.txt", "brief-2.txt"}
        assert all(Path(ref).is_file() for ref in root["resource_refs"])
        policy = root["run_policy"]
        assert "force_sequential" not in policy and policy["delay_between_jobs_ms"] == 25
        renamed = await client.patch(f"/api/projects/{pid}", json={"name": "Renamed demo"})
        assert renamed.status_code == 200
        renamed_root = renamed.json()["project"]
        assert renamed_root["project_name"] == "Renamed demo"
        assert renamed_root["objective"] == "Inspectable demo"
        assert renamed_root["generated_prompt"] == "Build an inspectable demo"
        derived = await store.create_project(
            "Build a **scoped** project",
            repo_path=str(tmp_path / "projects" / "derived"),
        )
        assert derived.project_name is None
        assert derived.objective == "Build a **scoped** project"
        await store.delete_project(derived.id)
        root_node = await store.get_node(uuid.UUID(pid))
        run = await store.create_run(root_node, "echo")
        await store.update_run(run.id, session_id="live-session")
        live_run = (await store.get_runs(root_node.id))[0]
        assert live_run.session_id == "live-session" and live_run.ended_at is None
        await store.update_run(
            run.id,
            status=RunStatus.COMPLETE,
            outcome=Outcome.COMPLETE,
            usage=Usage(input_tokens=8, cached_input_tokens=2, output_tokens=3, cost_usd=0.004),
        )
        usage = (await client.get(f"/api/projects/{pid}/usage")).json()
        assert usage["totals"]["input_tokens"] == 8
        assert usage["by_node"][pid]["output_tokens"] == 3
        assert usage["by_branch"][pid]["cost_usd"] == 0.004
        assert usage["node_count"] == 1
        child = await store.create_node(
            project_id=root_node.project_id,
            parent_id=root_node.id,
            objective="A second measured agent",
            executor="echo",
            agent=root_node.agent,
        )
        child_run = await store.create_run(child, "echo")
        await store.update_run(
            child_run.id,
            status=RunStatus.COMPLETE,
            outcome=Outcome.COMPLETE,
            usage=Usage(input_tokens=5, cached_input_tokens=7, output_tokens=2, cost_usd=0.001),
        )
        usage = (await client.get(f"/api/projects/{pid}/usage")).json()
        assert usage["by_node"][pid]["input_tokens"] == 8
        assert usage["by_node"][str(child.id)]["input_tokens"] == 5
        assert usage["by_node"][str(child.id)]["cached_input_tokens"] == 7
        assert usage["by_branch"][pid]["input_tokens"] == 13
        branch = await client.post(f"/api/nodes/{pid}/branch", json={"action": "pause"})
        assert branch.status_code == 200
        node = (await client.get(f"/api/nodes/{pid}")).json()["node"]
        assert node["ui_state"] == "paused"
        await runner.close_project_workspace(root_node.id)
        await store.delete_project(root_node.id)
        await client.post("/api/settings", json={"agent_defaults": original_defaults})
    await runner.stop()
    await store.dispose()
