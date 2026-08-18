from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI

from turn.contracts.dag import parse_plan, parse_result
from turn.db.store import Store
from turn.domain.schemas import ArtifactKind, ArtifactSpec, DocumentRef, NodeSpec, PlanResult
from turn.server.api import router
from turn.tools.graph_explorer import _query


def test_document_refs_are_uri_like_and_composable():
    ref = DocumentRef(
        ref="docs/ARCHITECTURE.md#runtime",
        title="Runtime architecture",
        imports=[DocumentRef(ref="docs/contracts.md")],
    )
    assert ref.imports[0].ref == "docs/contracts.md"
    assert DocumentRef(ref="https://example.com/architecture")

    for invalid in ("/tmp/architecture.md", "../architecture.md", "file:///tmp/a.md", "ftp://example.com/a"):
        with pytest.raises(ValueError):
            DocumentRef(ref=invalid)


def test_plan_and_result_accept_file_reference_shorthand():
    plan = parse_plan({
        "nodes": [{
            "key": "build",
            "objective": "Build the feature",
            "document_refs": ["docs/prompt.md"],
            "artifacts": ["src/feature.py"],
        }],
        "document_refs": [{
            "ref": "ARCHITECTURE.md",
            "imports": [{"ref": "docs/runtime.md", "imports": ["docs/contracts.md"]}],
        }],
        "artifacts": ["ARCHITECTURE.md"],
    })
    assert plan.nodes[0].document_refs[0].ref == "docs/prompt.md"
    assert plan.nodes[0].artifacts[0].ref == "src/feature.py"
    assert plan.document_refs[0].ref == "ARCHITECTURE.md"
    assert plan.document_refs[0].imports[0].imports[0].ref == "docs/contracts.md"
    assert parse_result({
        "outcome": "COMPLETE",
        "document_refs": ["reports/verification.md"],
    }).document_refs[0].ref == "reports/verification.md"

    handoff = parse_plan({
        "nodes": [],
        "document_refs": ["ARCHITECTURE.md"],
        "artifacts": ["ARCHITECTURE.md"],
    })
    assert handoff.document_refs[0].ref == "ARCHITECTURE.md"
    assert handoff.artifacts[0].name == "ARCHITECTURE.md"
    assert handoff.artifacts[0].ref == "ARCHITECTURE.md"


@pytest.mark.asyncio
async def test_store_keeps_references_dynamic_until_a_worker_submits_the_artifact(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a documented feature")
    plan = PlanResult(
        document_refs=[
            DocumentRef(
                ref="ARCHITECTURE.md",
                imports=[DocumentRef(ref="docs/runtime.md", imports=[DocumentRef(ref="docs/contracts.md")])],
            ),
            DocumentRef(ref="docs/plan.md"),
            DocumentRef(ref="https://example.com/reference"),
        ],
        artifacts=[{"kind": "file", "name": "ARCHITECTURE.md", "ref": "ARCHITECTURE.md"}],
        nodes=[NodeSpec(
            key="worker",
            objective="Build feature",
            document_refs=[DocumentRef(ref="docs/prompt.md")],
        )],
    )
    created = await store.apply_plan(root, plan)
    graph = await store.get_graph(root.id)
    child = next(node for node in graph.nodes if node.id == created[0].id)
    assert [ref.ref for ref in graph.nodes[0].document_refs] == [
        "ARCHITECTURE.md",
        "docs/plan.md",
        "https://example.com/reference",
    ]
    assert [ref.ref for ref in child.document_refs] == ["docs/prompt.md"]
    refs = {artifact.ref for artifact in graph.artifacts if artifact.ref}
    assert refs == {"ARCHITECTURE.md"}
    assert not any(artifact.ref == "docs/plan.md" for artifact in graph.artifacts)
    assert not any(artifact.ref == "https://example.com/reference" for artifact in graph.artifacts)

    await store.add_artifacts(
        child.id,
        [ArtifactSpec(kind=ArtifactKind.FILE, name="prompt.md", ref="docs/prompt.md")],
    )
    graph = await store.get_graph(root.id)
    refs = {artifact.ref for artifact in graph.artifacts if artifact.ref}
    assert refs == {"ARCHITECTURE.md", "docs/prompt.md"}
    state_path = store.project_path(root.id) / ".turn" / "state.json"
    raw = json.loads(state_path.read_text())
    assert "contents" not in json.dumps(raw)

    nodes, _ = await _query(str(state_path), str(root.id))
    inspected = next(item for item in nodes if item["id"] == str(child.id))
    assert inspected["document_refs"][0]["ref"] == "docs/prompt.md"


@pytest.mark.asyncio
async def test_adding_document_refs_never_creates_placeholder_artifacts(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Keep planned documents optional")

    assert await store.add_document_refs(root.id, [DocumentRef(ref="future.md")]) == []
    graph = await store.get_graph(root.id)

    assert [ref.ref for ref in graph.nodes[0].document_refs] == ["future.md"]
    assert graph.artifacts == []


@pytest.mark.asyncio
async def test_artifact_identity_deduplicates_same_reference_even_when_labels_differ(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Deduplicate document artifacts")
    plan = PlanResult(
        document_refs=[DocumentRef(ref="ARCHITECTURE.md", title="Architecture")],
        artifacts=[
            {"kind": "file", "name": "ARCHITECTURE.md", "ref": "ARCHITECTURE.md"},
            {"kind": "file", "name": "Architecture", "ref": "ARCHITECTURE.md"},
        ],
        nodes=[],
    )
    await store.apply_plan(root, plan)
    graph = await store.get_graph(root.id)
    matches = [artifact for artifact in graph.artifacts if artifact.ref == "ARCHITECTURE.md"]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_store_migrates_duplicate_reference_artifacts_when_reopened(tmp_path):
    data_dir = tmp_path / "state"
    store = Store(data_dir)
    await store.init()
    root = await store.create_project("Migrate duplicate document artifacts")
    state_path = store.project_path(root.id) / ".turn" / "state.json"
    raw = json.loads(state_path.read_text())
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    raw["artifacts"] = [
        {
            "id": first_id,
            "node_id": str(root.id),
            "kind": "file",
            "name": "ARCHITECTURE.md",
            "content": None,
            "ref": "ARCHITECTURE.md",
        },
        {
            "id": second_id,
            "node_id": str(root.id),
            "kind": "file",
            "name": "Branchlight Architecture",
            "content": None,
            "ref": "ARCHITECTURE.md",
        },
    ]
    raw["nodes"][0]["artifact_refs"] = [first_id, second_id]
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = Store(data_dir)
    await reopened.init()
    graph = await reopened.get_graph(root.id)
    matches = [artifact for artifact in graph.artifacts if artifact.ref == "ARCHITECTURE.md"]
    assert len(matches) == 1
    assert graph.nodes[0].artifact_refs == [matches[0].id]


@pytest.mark.asyncio
async def test_store_discovers_project_local_state_without_top_level_project_id(tmp_path):
    data_dir = tmp_path / "state"
    projects_dir = tmp_path / "projects"
    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project(
        "Discover an existing project",
        repo_path=str(projects_dir / "existing"),
    )
    state_path = store.project_path(root.id) / ".turn" / "state.json"
    raw = json.loads(state_path.read_text())
    raw.pop("project_id")
    state_path.write_text(json.dumps(raw), encoding="utf-8")
    (data_dir / "config.json").write_text(
        json.dumps({"projects": {}, "settings": {}}),
        encoding="utf-8",
    )

    reopened = Store(data_dir, projects_dir=projects_dir)
    await reopened.init()

    projects = await reopened.list_projects()
    assert [project.id for project in projects] == [root.id]
    assert reopened.project_path(root.id) == store.project_path(root.id)


@pytest.mark.asyncio
async def test_project_document_endpoint_is_scoped_and_serves_markdown(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Serve documents")
    project_root = store.project_path(root.id)
    (project_root / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")

    app = FastAPI()
    app.include_router(router)
    app.state.store = store
    app.state.runner = None
    app.state.events = None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        served = await client.get(f"/api/projects/{root.id}/documents/ARCHITECTURE.md")
        assert served.status_code == 200
        assert served.text.startswith("# Architecture")
        traversal = await client.get(
            f"/api/projects/{root.id}/documents/%2E%2E/outside.md"
        )
        assert traversal.status_code == 404


@pytest.mark.asyncio
async def test_capability_catalog_serves_the_actual_skill_file():
    app = FastAPI()
    app.include_router(router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        catalog = await client.get("/api/capability-catalog/turn-executing")
        assert catalog.status_code == 200
        source = catalog.json()["skills"][0]["source"]
        served = await client.get(source)
        assert served.status_code == 200
        assert "# Turn executing skill" in served.text
        missing = await client.get("/api/capability-catalog/not-a-capability")
        assert missing.status_code == 404
