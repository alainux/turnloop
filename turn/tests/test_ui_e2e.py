"""Browser acceptance for the strict React workbench."""
from __future__ import annotations

import os
import shutil
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
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=.5) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(.1)
    raise RuntimeError(f"server did not become ready: {url}")


def test_react_authoring_manual_graph_inspector_terminal_and_visuals(tmp_path):
    if shutil.which("herdr") is None:
        pytest.skip("Herdr is required for Turn terminal sessions")
    playwright = pytest.importorskip("playwright.sync_api")
    try:
        port = _free_port()
    except PermissionError:
        pytest.skip("this sandbox does not permit local listener sockets")
    env = os.environ.copy()
    env.update({
        # Herdr owns the project workspace; the project directory remains
        # pytest-managed and can use the long path.
        "TURN_DATA_DIR": f"/private/tmp/turn-ui-{port}",
        "TURN_PROJECTS_DIR": str(tmp_path / "projects"),
        "TURN_PLANNER": "heuristic",
        "TURN_DEFAULT_EXECUTOR": "mock",
        "TURN_TEST_MODE": "1",
        "TURN_RUNNER_TICK_SECONDS": "0.02",
    })
    server_log = tmp_path / "turn-server.log"
    server_log_handle = server_log.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "turn.server.app:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=server_log_handle,
        stderr=subprocess.STDOUT,
    )
    project_id_for_cleanup: str | None = None
    try:
        _wait(f"http://127.0.0.1:{port}/")
        with playwright.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as error:
                pytest.skip(f"Playwright Chromium is not installed: {error}")
            page = browser.new_page(viewport={"width": 1440, "height": 960})
            page.set_default_timeout(9000)
            console_errors: list[str] = []
            websocket_frames: list[str] = []
            page.add_init_script(
                """(() => {
                    const NativeWebSocket = window.WebSocket;
                    const sockets = [];
                    Object.defineProperty(window, "__turnSockets", { value: sockets });
                    window.WebSocket = new Proxy(NativeWebSocket, {
                        construct(target, args) {
                            const socket = Reflect.construct(target, args);
                            sockets.push(socket);
                            return socket;
                        },
                    });
                })()"""
            )

            def capture_websocket(websocket) -> None:
                # Playwright wraps Python callbacks; passing the bound list
                # method directly is rejected by that wrapper.
                websocket.on("framesent", lambda payload: websocket_frames.append(payload))

            page.on("websocket", capture_websocket)
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error" and message.text != "[Turn] Settings saved"
                else None,
            )
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")

            page.get_by_role("heading", name="What should the workgraph build?").wait_for()
            assert page.locator("img.hero-asset,.welcome-visual,.activitybar").count() == 0
            assert page.locator("aside.sidebar").evaluate("node => getComputedStyle(node).visibility === 'hidden'")
            onboarding = tmp_path / "onboarding.png"
            page.screenshot(path=str(onboarding))

            page.get_by_role("button", name="Workspace settings").click()
            settings = page.locator(".side-panel")
            settings.wait_for()
            assert page.get_by_role("button", name="Save settings").is_disabled()
            page.get_by_label("Theme").select_option("light")
            assert page.get_by_role("button", name="Save settings").is_enabled()
            page.get_by_role("button", name="Save settings").click()
            page.wait_for_function("document.documentElement.dataset.theme === 'light'")
            page.get_by_role("button", name="Close settings").click()

            objective = "Build a compact offline incident-response CLI with parser, policy, renderer, and integration checks."
            page.get_by_role("textbox", name="Project objective").fill(objective)
            page.get_by_label("Harness").click()
            page.get_by_role("option", name="Mock harness").click()
            assert page.get_by_label("Model").input_value() == "Deterministic"
            attachment = tmp_path / "brief.txt"
            attachment.write_text("All commands must be deterministic and offline.")
            second_attachment = tmp_path / "constraints.md"
            second_attachment.write_text("The public launch command must remain stable.")
            page.get_by_role("button", name="Attach files").click()
            page.locator('input[type="file"]').set_input_files([
                str(attachment),
                str(second_attachment),
            ])
            page.get_by_text("brief.txt", exact=False).wait_for()
            page.get_by_text("constraints.md", exact=False).wait_for()
            page.get_by_role("button", name="Remove brief.txt").wait_for()
            page.get_by_role("button", name="Remove brief.txt").click()
            assert page.get_by_role("button", name="Remove brief.txt").count() == 0
            page.get_by_role("button", name="Remove constraints.md").wait_for()
            selector = page.get_by_label("Harness")
            selector_width = selector.evaluate("node => node.getBoundingClientRect().width")
            selector.click()
            page.get_by_role("option", name="Mock harness").wait_for()
            assert abs(selector.evaluate("node => node.getBoundingClientRect().width") - selector_width) < 1
            page.keyboard.press("Escape")
            composer = page.locator("form.composer")
            composer_height = composer.evaluate("node => node.getBoundingClientRect().height")
            page.get_by_role("button", name="Project and run configuration").click()
            assert abs(composer.evaluate("node => node.getBoundingClientRect().height") - composer_height) < 1
            page.get_by_role("button", name="Project and run configuration").click()
            page.route(
                "**/api/system/pick-directory",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"path":null}',
                ),
            )
            page.get_by_role("button", name="Choose project directory").click()
            assert page.get_by_role("button", name="Use current directory").count() == 0
            page.unroute("**/api/system/pick-directory")
            page.route(
                "**/api/system/pick-directory",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"path":"/workspace/incident-response"}',
                ),
            )
            page.get_by_role("button", name="Choose project directory").click()
            page.get_by_text("incident-response", exact=True).wait_for()
            page.get_by_role("button", name="Use current directory").click()
            page.get_by_role("button", name="Project and run configuration").click()
            page.get_by_label("Auto-run", exact=True).uncheck()
            page.get_by_role("button", name="Create workgraph").click()
            page.locator(".gnode").first.wait_for()
            assert page.get_by_role("button", name="New project").count() == 1

            root = page.evaluate(
                "async objective => (await (await fetch('/api/projects')).json()).projects.find(p => p.generated_prompt === objective)",
                objective,
            )
            project_id_for_cleanup = root["id"]
            assert len(root["objective"]) <= 72 and root["generated_prompt"] == objective
            root_play = page.get_by_role("button", name=f"Run {root['objective']}")
            root_play.wait_for()
            root_play.click()
            page.wait_for_function("document.querySelectorAll('.gnode').length === 5")
            assert page.locator(".edge-active").count() == 0
            assert page.locator(".gnode").evaluate_all("nodes => nodes.every(node => getComputedStyle(node, '::after').animationName === 'none')")
            page.locator(f'[data-node-id="{root["id"]}"]').click(button="right")
            page.get_by_role("menuitem", name="Inspect node").wait_for()
            page.keyboard.press("Escape")

            graph = page.evaluate("async () => { const p = await (await fetch('/api/projects')).json(); return (await (await fetch(`/api/projects/${p.projects[0].id}/graph`)).json()); }")
            children = [node for node in graph["nodes"] if node["parent_id"]]
            assert children and all(node["agent"]["harness"] == "mock" for node in children)
            runnable = next(node for node in children if "run" in node["allowed_actions"])
            page.get_by_role("button", name=f"Run {runnable['objective']}").click()
            page.wait_for_function(
                "async id => (await (await fetch(`/api/nodes/${id}`)).json()).node.status === 'COMPLETE'",
                arg=runnable["id"],
            )

            page.locator(f'[data-node-id="{runnable["id"]}"] .node-main').click()
            page.locator("#inspector").get_by_role(
                "heading", name=runnable["objective"]
            ).wait_for()
            page.locator(".instructions-section").wait_for()
            assert page.locator(".instructions-section").count() == 1
            assert page.get_by_role("button", name="Save agent").is_disabled()
            page.get_by_role("tab", name="Terminal").click()
            page.locator(".terminal-shadow-host").wait_for(state="visible")
            page.locator(".terminal-shadow-host .xterm").wait_for()
            page.wait_for_function("document.querySelector('.terminal-mode')?.textContent?.includes('LIVE')")
            terminal_text = page.locator(".terminal-shadow-host").text_content() or ""
            assert '"type"' not in terminal_text
            helper = page.locator(".terminal-shadow-host .xterm-helper-textarea")
            assert helper.is_enabled() and helper.get_attribute("tabindex") != "-1"

            # Scrolling is a Herdr pane operation. Exercise the same browser
            # websocket used by the xterm presenter and assert that the server
            # returns the canonical pane snapshot after forwarding the request
            # to Herdr, rather than maintaining a second local transcript.
            try:
                scroll_snapshot = page.evaluate(
                    """() => new Promise((resolve, reject) => {
                    const socket = window.__turnSockets?.find(
                        candidate => candidate.url.includes('/shell') && candidate.readyState === WebSocket.OPEN
                    );
                    if (!socket) {
                        reject(new Error(`no open Turn terminal websocket: ${JSON.stringify(
                            window.__turnSockets?.map(candidate => ({ url: candidate.url, readyState: candidate.readyState }))
                        )}`));
                        return;
                    }
                    const onMessage = event => {
                        try {
                            const message = JSON.parse(event.data);
                            if (message.type === 'snapshot') {
                                socket.removeEventListener('message', onMessage);
                                resolve(message);
                            }
                        } catch {}
                    };
                    socket.addEventListener('message', onMessage);
                    socket.send(JSON.stringify({ type: 'scroll', direction: 'up', amount: 1 }));
                    window.setTimeout(() => reject(new Error('scroll snapshot timeout')), 10000);
                    })"""
                )
            except Exception as error:
                server_log_handle.flush()
                raise AssertionError(
                    f"browser scroll exchange failed: {error}\n{server_log.read_text()}"
                ) from error
            assert scroll_snapshot["type"] == "snapshot"
            sent_messages = [
                frame for frame in websocket_frames
                if isinstance(frame, str) and '"type":"scroll"' in frame
            ]
            assert any('"direction":"up"' in frame for frame in sent_messages)

            # The visible xterm viewport must pass user wheel input to the
            # node's Herdr pane. It must not scroll an independent browser
            # transcript or rely on a synthetic page-level scroll position.
            wheel_snapshot = page.evaluate(
                """() => new Promise((resolve, reject) => {
                const viewport = document.querySelector('.terminal-shadow-host .xterm-viewport');
                const socket = window.__turnSockets?.find(
                    candidate => candidate.url.includes('/shell') && candidate.readyState === WebSocket.OPEN
                );
                if (!viewport || !socket) {
                    reject(new Error('terminal viewport or socket is unavailable'));
                    return;
                }
                const localScrollTop = viewport.scrollTop;
                const onMessage = event => {
                    try {
                        const message = JSON.parse(event.data);
                        if (message.type === 'snapshot') {
                            socket.removeEventListener('message', onMessage);
                            resolve({ message, localScrollTop, afterScrollTop: viewport.scrollTop });
                        }
                    } catch {}
                };
                socket.addEventListener('message', onMessage);
                viewport.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: -120,
                    bubbles: true,
                    cancelable: true,
                }));
                window.setTimeout(() => reject(new Error('wheel scroll snapshot timeout')), 10000);
                })"""
            )
            assert wheel_snapshot["message"]["type"] == "snapshot"
            assert wheel_snapshot["localScrollTop"] == 0
            assert wheel_snapshot["afterScrollTop"] == 0
            sent_messages = [
                frame for frame in websocket_frames
                if isinstance(frame, str) and '"type":"scroll"' in frame
            ]
            assert any('"direction":"up"' in frame for frame in sent_messages)

            workspace = tmp_path / "workspace.png"
            page.screenshot(path=str(workspace))
            page.set_viewport_size({"width": 700, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth === document.documentElement.clientWidth")
            page.get_by_role("button", name="Toggle projects").click()
            assert page.locator("aside.sidebar").is_visible()
            page.locator(".project-menu").first.click()
            page.get_by_role("menuitem", name="Rename").wait_for()
            page.keyboard.press("Escape")
            compact = tmp_path / "compact.png"
            page.screenshot(path=str(compact))
            for path, size in ((onboarding, (1440, 960)), (workspace, (1440, 960)), (compact, (700, 900))):
                with Image.open(path) as image:
                    assert image.size == size
                    assert max(ImageStat.Stat(image.convert("RGB")).var) > 20
            assert not console_errors
            browser.close()
    finally:
        if project_id_for_cleanup is not None:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/projects/{project_id_for_cleanup}",
                    method="DELETE",
                )
                with urllib.request.urlopen(request, timeout=3):
                    pass
            except Exception:
                pass
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        server_log_handle.close()
