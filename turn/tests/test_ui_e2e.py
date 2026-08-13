from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from PIL import Image, ImageStat


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url: str, seconds: float = 12) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=.5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(.1)
    raise RuntimeError(f"server did not become ready: {url}")


def test_authoring_graph_terminal_editing_settings_and_visual_generation(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        port = _free_port()
    except PermissionError:
        pytest.skip("this sandbox does not permit local listener sockets")
    env = os.environ.copy()
    env.update({
        "TURN_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'ui.db'}",
        "TURN_PROJECTS_DIR": str(tmp_path / "projects"),
        "TURN_PLANNER": "heuristic",
        "TURN_DEFAULT_EXECUTOR": "echo",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "turn.server.app:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait(f"http://127.0.0.1:{port}/")
        with playwright.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as exc:
                pytest.skip(f"Playwright Chromium is not installed: {exc}")
            page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
            page.set_default_timeout(7000)
            console_errors: list[str] = []
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("button", name="Help and shortcuts").click()
            assert page.locator("#popover").is_visible()
            assert "Toggle projects" in page.locator("#popover").inner_text()
            page.keyboard.press("Escape")

            # Prompt-first onboarding: sober, single-purpose, and collapsed.
            page.get_by_role("heading", name="What should the workgraph build?").wait_for()
            assert page.locator("img,.brand-mark,.welcome-visual,.activitybar:visible").count() == 0
            assert page.locator("#project-sidebar").evaluate("element => getComputedStyle(element).visibility === 'hidden'")
            assert page.get_by_role("button", name="Attach files").count() == 1
            assert page.get_by_role("button", name="Choose project directory").count() == 1
            onboarding = tmp_path / "onboarding.png"
            page.screenshot(path=str(onboarding), full_page=True)

            # Help is centralized and settings are a panel with pristine saves.
            page.locator("#help-btn").click()
            assert "Toggle projects" in page.locator("#popover").inner_text()
            page.keyboard.press("Escape")
            page.keyboard.press("Control+,")
            page.locator("#settings-panel:not([hidden])").wait_for()
            settings_save = page.get_by_role("button", name="Save settings")
            assert settings_save.is_disabled()
            page.locator("#setting-theme").select_option("light")
            assert settings_save.is_enabled()
            settings_save.click()
            page.locator("#settings-panel").wait_for(state="hidden")

            # Compact configuration remains available without repeating the prompt.
            page.locator("#author-config-btn").click()
            page.locator("#author-config-panel:not([hidden])").wait_for()
            page.locator("#author-prompt").fill("Create a compact visual test project")
            assert page.locator("#new-name").input_value() == "compact visual test project"
            page.locator("#new-name").fill("Visual test")
            page.locator("#new-model").fill("fast-mini")
            assert page.locator("#new-reasoning option").all_text_contents() == ["Default", "Low", "Medium", "High"]
            page.locator("#new-model").fill("smalltalk-pro")
            assert "Xhigh" in page.locator("#new-reasoning option").all_text_contents()
            page.locator("#new-auto").uncheck()
            attachment = tmp_path / "brief.txt"
            attachment.write_text("attached acceptance criteria")
            page.locator("#attachment-input").set_input_files(str(attachment))
            page.get_by_text("brief.txt", exact=True).wait_for()
            page.get_by_role("button", name="Create workgraph").click()
            page.locator(".gnode").first.wait_for()
            resource_refs = page.evaluate("async () => (await (await fetch('/api/projects')).json()).projects[0].resource_refs")
            assert len(resource_refs) == 1 and resource_refs[0].endswith("brief.txt")

            # Manual step, graph liveness contracts, and stable keyed refresh.
            page.get_by_role("button", name="Run next").click()
            page.wait_for_function("document.querySelectorAll('.gnode').length >= 4")
            edge = page.locator(".edge-contains").first
            assert "H" in edge.get_attribute("d") and "V" in edge.get_attribute("d")
            live_node = page.locator(".gnode").first
            live_node.evaluate("element => element.classList.add('running')")
            edge.evaluate("element => element.classList.add('edge-active')")
            assert live_node.evaluate("element => getComputedStyle(element, '::after').animationName === 'node-breathe'")
            assert edge.evaluate("element => getComputedStyle(element).animationName === 'flow'")
            page.emulate_media(reduced_motion="reduce")
            assert live_node.evaluate("element => parseFloat(getComputedStyle(element, '::after').animationDuration) <= .001")
            page.emulate_media(reduced_motion="no-preference")
            page.evaluate("window.__stableNode = document.querySelector('.gnode')")
            page.evaluate("async () => fetch('/api/projects/' + (await (await fetch('/api/projects')).json()).projects[0].id + '/mode', {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({auto_run:false})})")
            page.wait_for_timeout(400)
            assert page.evaluate("window.__stableNode === document.querySelector('.gnode')")

            # Inspector inputs are the presentation, not duplicated editors.
            page.locator(".gnode").first.click()
            page.locator("#inspector").wait_for()
            page.get_by_role("textbox", name="Node objective").wait_for()
            assert page.get_by_role("button", name="Save agent").is_disabled()
            assert page.get_by_role("button", name="Save revision").is_disabled()
            objective = page.get_by_role("textbox", name="Node objective")
            objective.fill(objective.input_value() + " revised")
            assert page.get_by_role("button", name="Save revision").is_enabled()
            dirty_value = objective.input_value()
            selected_id = page.locator(".gnode.selected").get_attribute("data-node-id")
            page.evaluate("""async ([id]) => fetch(`/api/nodes/${id}/edit`, {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({generated_prompt:'Concurrent persisted update'})})""", [selected_id])
            page.wait_for_timeout(500)
            assert objective.input_value() == dirty_value
            assert page.get_by_role("button", name="Save revision").is_enabled()
            page.get_by_role("button", name="Regenerate branch").click()
            page.get_by_role("dialog").wait_for()
            assert "Active descendants" in page.get_by_role("dialog").inner_text()
            page.get_by_role("dialog").get_by_role("button", name="Cancel", exact=True).click()

            # Terminal is xterm over the PTY socket, with truthful session mode.
            page.get_by_role("tab", name="Terminal").click()
            page.locator(".xterm").wait_for()
            assert page.locator(".terminal-note").count() == 0
            assert page.locator(".terminal-mode").inner_text() in {"LIVE", "TRANSCRIPT", "CONNECTING"}
            page.wait_for_function("document.querySelector('.terminal-mode')?.textContent === 'TRANSCRIPT'")
            terminal_input = page.locator(".xterm-helper-textarea")
            assert terminal_input.is_disabled()
            assert terminal_input.get_attribute("tabindex") == "-1"
            assert terminal_input.get_attribute("aria-label") == "Terminal transcript"

            # Policy is a side panel and its save is also pristine-aware.
            page.locator("#project-options-btn").click()
            page.locator("#policy-panel:not([hidden])").wait_for()
            policy_save = page.get_by_role("button", name="Apply policy")
            assert policy_save.is_disabled()
            page.locator("#policy-sequential").check()
            assert policy_save.is_enabled()
            page.keyboard.press("Escape")
            page.locator("#policy-panel").wait_for(state="hidden")

            workspace = tmp_path / "workspace.png"
            page.screenshot(path=str(workspace), full_page=True)
            page.set_viewport_size({"width": 700, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth === document.documentElement.clientWidth")
            compact = tmp_path / "workspace-compact.png"
            page.screenshot(path=str(compact), full_page=True)
            for path, size in ((onboarding, (1440, 960)), (workspace, (1440, 960)), (compact, (700, 900))):
                with Image.open(path) as image:
                    assert image.size == size
                    assert max(ImageStat.Stat(image.convert("RGB")).var) > 20
            assert not console_errors
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
