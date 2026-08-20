"""Three complete browser-driven offline acceptance runs with DB inspection."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

OBJECTIVES = (
    "Build a small modular command-line task tracker",
    "Create a branching mystery game with three endings",
    "Write a concise field guide with five independent chapters",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url: str, seconds: float = 12) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=.5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(.1)
    raise RuntimeError(f"server did not become ready: {url}")


def _wait_persisted_complete(state_file, seconds: float = 30) -> None:
    deadline, stable = time.time() + seconds, 0
    while time.time() < deadline:
        try:
            state = json.loads(state_file.read_text())
            statuses = [row["status"] for row in state.get("nodes", [])]
        except (FileNotFoundError, json.JSONDecodeError):
            statuses = []
        stable = stable + 1 if statuses and all(item == "COMPLETE" for item in statuses) else 0
        if stable >= 2:
            return
        time.sleep(.15)
    raise AssertionError(f"project {project_id} did not complete: {statuses}")


def _wait_for_graph(page, project_id: str, predicate, seconds: float = 30):
    """Poll the API response itself, rather than treating a Promise as truthy."""
    deadline = time.time() + seconds
    graph = None
    while time.time() < deadline:
        graph = page.evaluate(
            "async id => (await (await fetch(`/api/projects/${id}/graph?fresh=${Date.now()}`, {cache: 'no-store'})).json())",
            project_id,
        )
        if predicate(graph):
            return graph
        page.wait_for_timeout(100)
    raise AssertionError(f"project {project_id} never reached the expected graph state: {graph}")


def test_three_complete_ui_runs_persist_coherent_graphs_logs_and_results(tmp_path):
    if shutil.which("herdr") is None:
        pytest.skip("Herdr is required for process-harness acceptance runs")
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        port = _free_port()
    except PermissionError:
        pytest.skip("this sandbox does not permit local listener sockets")
    data_dir = tmp_path / "turn"
    server_log = tmp_path / "server.log"
    project_ids: list[str] = []
    env = os.environ.copy()
    env.update({
        "TURN_DATA_DIR": str(data_dir),
        "TURN_PROJECTS_DIR": str(tmp_path / "projects"),
        "TURN_PLANNER": "heuristic",
        "TURN_DEFAULT_EXECUTOR": "mock",
        "TURN_TEST_MODE": "1",
        "TURN_RUNNER_TICK_SECONDS": "0.02",
    })
    with server_log.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "turn.server.app:app", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait(f"http://127.0.0.1:{port}/")
            with playwright.sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as error:
                    pytest.skip(f"Playwright Chromium is not installed: {error}")
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page.set_default_timeout(12000)
                console_errors: list[str] = []
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                state_files: list[Path] = []

                for index, objective in enumerate(OBJECTIVES):
                    page.get_by_role("textbox", name="Project objective").fill(objective)
                    page.get_by_label("Harness").click()
                    page.get_by_role("option", name="Mock harness").click()
                    # Step is the product default. This acceptance flow
                    # intentionally verifies the separate Auto behavior.
                    page.get_by_role("button", name="Project and run configuration").click()
                    page.get_by_label("Auto-run", exact=True).check()
                    page.get_by_role("button", name="Create workgraph").click()
                    page.locator(".gnode").first.wait_for(timeout=15000)
                    project_id = page.evaluate(
                        "async objective => (await (await fetch('/api/projects', {cache: 'no-store'})).json()).projects.find(p => p.generated_prompt === objective).id",
                        objective,
                    )
                    project_ids.append(project_id)
                    graph = _wait_for_graph(
                        page, project_id, lambda value: len(value["nodes"]) == 5,
                    )
                    state_files.append(Path(graph["nodes"][0]["repo_path"]) / ".turn" / "state.json")
                    assert len(graph["nodes"]) == 5
                    assert sum(node["ui_state"] == "waiting_input" for node in graph["nodes"]) == 0
                    assert sum(edge["type"] == "FOLLOWS" for edge in graph["edges"]) == 3
                    assert all(
                        node["agent"]["harness"] == "mock" and node["agent"]["model"] == "deterministic"
                        for node in graph["nodes"] if node["parent_id"]
                    )

                    _wait_for_graph(
                        page,
                        project_id,
                        lambda value: all(node["status"] == "COMPLETE" for node in value["nodes"]),
                    )
                    _wait_persisted_complete(state_files[-1])
                    page.locator(f'[data-node-id="{project_id}"] .node-main').click()
                    assert page.get_by_role("tab", name="History").count() == 0
                    page.get_by_role("button", name="Turn").click()
                    page.get_by_role("heading", name="What should the workgraph build?").wait_for()

                page.get_by_role("button", name="Toggle projects").click()
                assert page.locator(".project-item").count() == 3
                assert not console_errors
                browser.close()
        finally:
            # Delete projects while the test server is still alive. This
            # closes their Herdr workspaces through the normal API lifecycle;
            # Runner.stop below is a second boundary-level cleanup.
            for project_id in project_ids:
                try:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/projects/{project_id}",
                        method="DELETE",
                    )
                    with urllib.request.urlopen(request, timeout=5):
                        pass
                except Exception:
                    pass
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    states = [json.loads(state_file.read_text()) for state_file in state_files]
    assert [next(node for node in state["nodes"] if node["parent_id"] is None)["generated_prompt"] for state in states] == list(OBJECTIVES)
    for state in states:
        roots = [node for node in state["nodes"] if node["parent_id"] is None]
        assert len(roots) == 1 and len(roots[0]["objective"]) <= 72 and roots[0]["status"] == "COMPLETE"
        nodes = state["nodes"]
        assert len(nodes) == 5 and all(node["status"] == "COMPLETE" for node in nodes)
        supplied = [item for node in nodes for item in node["required_inputs"]]
        assert supplied == []
        runs = state["runs"]
        assert len(runs) == 5
        assert all(run["status"] == "COMPLETE" and run["summary"] and run["logs"] for run in runs)
        artifacts = state["artifacts"]
        assert len([item for item in artifacts if item["kind"] == "user_input"]) == 0
        assert len([item for item in artifacts if item["kind"] == "text"]) == 4

    log_text = server_log.read_text()
    assert log_text.count("POST /api/projects") == 3
    assert "Traceback" not in log_text and " 500 " not in log_text
