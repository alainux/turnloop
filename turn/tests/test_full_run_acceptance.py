"""Browser-driven acceptance runs with database and log inspection.

The deterministic heuristic/echo harness proves the entire local product path
without network calls or token spend. The three objectives deliberately cover
the MVP's primary domains: software, story games, and books.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

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


def _wait_persisted_complete(database, project_id: str, seconds: float = 15) -> None:
    """Require two stable DB observations before the QA server is stopped."""
    deadline = time.time() + seconds
    stable = 0
    compact = project_id.replace("-", "")
    while time.time() < deadline:
        with sqlite3.connect(database) as connection:
            statuses = [
                row[0] for row in connection.execute(
                    "SELECT status FROM nodes WHERE project_id = ?", (compact,)
                )
            ]
        if statuses and all(status == "COMPLETE" for status in statuses):
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        time.sleep(.15)
    raise AssertionError(f"project {project_id} did not reach stable persisted completion: {statuses}")


def test_three_complete_ui_runs_persist_coherent_graphs_logs_and_results(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        port = _free_port()
    except PermissionError:
        pytest.skip("this sandbox does not permit local listener sockets")
    database = tmp_path / "acceptance.db"
    server_log = tmp_path / "server.log"
    env = os.environ.copy()
    env.update({
        "TURN_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "TURN_PROJECTS_DIR": str(tmp_path / "projects"),
        "TURN_PLANNER": "heuristic",
        "TURN_DEFAULT_EXECUTOR": "echo",
        "TURN_RUNNER_TICK_SECONDS": "0.02",
    })
    with server_log.open("w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "turn.server.app:app", "--host", "127.0.0.1", "--port", str(port)],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait(f"http://127.0.0.1:{port}/")
            with playwright.sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch()
                except Exception as exc:
                    pytest.skip(f"Playwright Chromium is not installed: {exc}")
                page = browser.new_page(viewport={"width": 1440, "height": 960})
                page.set_default_timeout(8000)
                console_errors: list[str] = []
                page.on("console", lambda message: console_errors.append(message.text) if message.type in {"error", "warning"} else None)
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                project_ids: list[str] = []

                for index, objective in enumerate(OBJECTIVES):
                    page.locator("#author-prompt").fill(objective)
                    page.locator("#author-form").evaluate("form => form.requestSubmit()")
                    page.locator("#new-auto").check()
                    page.get_by_role("button", name="Create workgraph").click()
                    page.locator(".gnode.waiting_input").wait_for(timeout=15000)
                    project_id = page.evaluate("localStorage.getItem('turn.project')")
                    project_ids.append(project_id)

                    graph = page.evaluate("async id => (await fetch(`/api/projects/${id}/graph`)).json()", project_id)
                    assert len(graph["nodes"]) == 5
                    assert len({node["objective"] for node in graph["nodes"]}) == 5
                    assert sum(node["ui_state"] == "waiting_input" for node in graph["nodes"]) == 1
                    assert sum(edge["type"] == "DEPENDS_ON" for edge in graph["edges"]) == 3

                    clarification = next(node for node in graph["nodes"] if node["ui_state"] == "waiting_input")
                    page.locator(f'[data-node-id="{clarification["id"]}"]').click()
                    page.locator("#detail .input-card textarea").fill(f"Keep run {index + 1} concise, modular, and independently verifiable.")
                    page.get_by_role("button", name="Provide input").click()
                    page.wait_for_function(
                        """async id => {
                          const graph = await (await fetch(`/api/projects/${id}/graph`)).json();
                          const root = graph.nodes.find(node => node.id === id);
                              // Completion acceptance is a persisted-state
                              // contract, not merely a transient graph
                              // projection. Wait for durable facts before
                              // closing the project/server.
                              return root?.status === 'COMPLETE' && graph.nodes.every(node => node.status === 'COMPLETE');
                        }""",
                        arg=project_id,
                        timeout=15000,
                    )
                    _wait_persisted_complete(database, project_id)

                    page.locator(f'[data-node-id="{project_id}"]').click(button="right")
                    page.get_by_role("menuitem", name="View run history").click()
                    page.locator(".history-item").wait_for()
                    assert "attempt 1" in page.locator("#detail").inner_text()
                    page.locator("#home-btn").click()
                    page.get_by_role("heading", name="What should the workgraph build?").wait_for()

                page.get_by_role("button", name="Toggle projects").click()
                assert page.locator(".project-item").count() == 3
                assert console_errors == []
                browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        roots = connection.execute("SELECT id, objective, generated_prompt, status FROM nodes WHERE parent_id IS NULL ORDER BY created_at").fetchall()
        assert [row["generated_prompt"] for row in roots] == list(OBJECTIVES)
        assert all(len(row["objective"]) <= 72 for row in roots)
        assert len({row["objective"] for row in roots}) == len(OBJECTIVES)
        assert all(row["status"] == "COMPLETE" for row in roots)
        for root in roots:
            nodes = connection.execute("SELECT id, objective, generated_prompt, status, required_inputs FROM nodes WHERE project_id = ?", (root["id"],)).fetchall()
            assert len(nodes) == 5
            assert len({row["objective"] for row in nodes}) == 5
            assert all(row["status"] == "COMPLETE" for row in nodes)
            assert all(root["generated_prompt"] in row["generated_prompt"] for row in nodes if row["id"] != root["id"])
            gated = [json.loads(row["required_inputs"] or "[]") for row in nodes]
            supplied = [item for items in gated for item in items]
            assert len(supplied) == 1 and supplied[0]["satisfied_by"]
            edges = connection.execute(
                "SELECT type FROM edges WHERE src IN (SELECT id FROM nodes WHERE project_id = ?)",
                (root["id"],),
            ).fetchall()
            assert sum(row["type"] == "CONTAINS" for row in edges) == 4
            assert sum(row["type"] == "DEPENDS_ON" for row in edges) == 3
            runs = connection.execute(
                "SELECT status, summary, logs FROM runs WHERE node_id IN (SELECT id FROM nodes WHERE project_id = ?)",
                (root["id"],),
            ).fetchall()
            assert len(runs) == 5
            assert all(row["status"] == "COMPLETE" and row["summary"] and row["logs"] for row in runs)
            artifacts = connection.execute(
                "SELECT kind, name, content FROM artifacts WHERE node_id IN (SELECT id FROM nodes WHERE project_id = ?)",
                (root["id"],),
            ).fetchall()
            assert len(artifacts) == 5
            user_inputs = [row for row in artifacts if row["kind"] == "user_input"]
            results = [row for row in artifacts if row["kind"] == "text"]
            assert len(user_inputs) == 1 and user_inputs[0]["name"] == "input:scope"
            assert "independently verifiable" in json.loads(user_inputs[0]["content"])
            assert len(results) == 4
            child_objectives = {row["objective"] for row in nodes if row["id"] != root["id"]}
            assert {json.loads(row["content"]).split("\n", 1)[0] for row in results} == child_objectives

    log_text = server_log.read_text()
    assert log_text.count("POST /api/projects") == 3
    assert "Traceback" not in log_text
    assert " 500 " not in log_text
