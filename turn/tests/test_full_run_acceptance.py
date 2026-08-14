"""Three complete browser-driven offline acceptance runs with DB inspection."""
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
    deadline, stable = time.time() + seconds, 0
    while time.time() < deadline:
        with sqlite3.connect(database) as connection:
            statuses = [row[0] for row in connection.execute(
                "SELECT status FROM nodes WHERE project_id = ?", (project_id.replace("-", ""),)
            )]
        stable = stable + 1 if statuses and all(item == "COMPLETE" for item in statuses) else 0
        if stable >= 2:
            return
        time.sleep(.15)
    raise AssertionError(f"project {project_id} did not complete: {statuses}")


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
                project_ids: list[str] = []

                for index, objective in enumerate(OBJECTIVES):
                    page.get_by_role("textbox", name="Project objective").fill(objective)
                    page.get_by_label("Harness").select_option("echo")
                    page.get_by_role("button", name="Create workgraph").click()
                    page.locator(".gnode.waiting_input").wait_for(timeout=15000)
                    project_id = page.evaluate(
                        "async objective => (await (await fetch('/api/projects')).json()).projects.find(p => p.generated_prompt === objective).id",
                        objective,
                    )
                    project_ids.append(project_id)
                    graph = page.evaluate("async id => (await (await fetch(`/api/projects/${id}/graph`)).json())", project_id)
                    assert len(graph["nodes"]) == 5
                    assert sum(node["ui_state"] == "waiting_input" for node in graph["nodes"]) == 1
                    assert sum(edge["type"] == "DEPENDS_ON" for edge in graph["edges"]) == 3
                    assert all(
                        node["agent"]["harness"] == "echo" and node["agent"]["model"] == "deterministic"
                        for node in graph["nodes"] if node["parent_id"]
                    )

                    clarification = next(node for node in graph["nodes"] if node["ui_state"] == "waiting_input")
                    page.locator(f'[data-node-id="{clarification["id"]}"] .node-main').click()
                    input_label = next(item["label"] for item in clarification["required_inputs"] if not item.get("satisfied_by"))
                    page.get_by_role("textbox", name=input_label).fill(
                        f"Keep run {index + 1} concise, modular, and independently verifiable."
                    )
                    page.get_by_role("button", name="Provide input").click()
                    page.wait_for_function(
                        "async id => { const g = await (await fetch(`/api/projects/${id}/graph`)).json(); return g.nodes.every(n => n.status === 'COMPLETE'); }",
                        arg=project_id,
                        timeout=15000,
                    )
                    _wait_persisted_complete(database, project_id)
                    page.locator(f'[data-node-id="{project_id}"] .node-main').click()
                    page.get_by_role("tab", name="History").click()
                    page.locator(".history-item").wait_for()
                    page.get_by_role("button", name="Turn").click()
                    page.get_by_role("heading", name="What should the workgraph build?").wait_for()

                page.get_by_role("button", name="Toggle projects").click()
                assert page.locator(".project-item").count() == 3
                assert not console_errors
                browser.close()
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        roots = connection.execute(
            "SELECT id, objective, generated_prompt, status FROM nodes WHERE parent_id IS NULL ORDER BY created_at"
        ).fetchall()
        assert [row["generated_prompt"] for row in roots] == list(OBJECTIVES)
        assert all(len(row["objective"]) <= 72 and row["status"] == "COMPLETE" for row in roots)
        for root in roots:
            nodes = connection.execute(
                "SELECT id, objective, status, required_inputs FROM nodes WHERE project_id = ?", (root["id"],)
            ).fetchall()
            assert len(nodes) == 5 and all(row["status"] == "COMPLETE" for row in nodes)
            supplied = [item for row in nodes for item in json.loads(row["required_inputs"] or "[]")]
            assert len(supplied) == 1 and supplied[0]["satisfied_by"]
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
            assert len([row for row in artifacts if row["kind"] == "user_input"]) == 1
            assert len([row for row in artifacts if row["kind"] == "text"]) == 4

    log_text = server_log.read_text()
    assert log_text.count("POST /api/projects") == 3
    assert "Traceback" not in log_text and " 500 " not in log_text
