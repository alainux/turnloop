from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"


class InventoryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_icon_system_is_complete_and_replaces_prototype_glyphs():
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    parser = InventoryParser()
    parser.feed(html)
    icon_names = {attrs["data-icon"] for _, attrs in parser.tags if attrs.get("data-icon")}
    assert len(icon_names) >= 18
    assert all((UI / "icons" / f"{name}.svg").is_file() for name in icon_names)
    for dynamic in ("activity", "alert-triangle", "circle-play", "git-fork", "pause", "pencil", "rotate-cw", "square-stop"):
        assert (UI / "icons" / f"{dynamic}.svg").is_file()
    assert (UI / "icons" / "LICENSE-lucide.txt").is_file()
    assert not re.search(r"[⚙◇▤⌁◐⌕⌄↑▶＋×❯]", html + js)
    assert "title=" not in html


def test_icon_only_controls_have_accessible_names_and_context_help():
    parser = InventoryParser()
    parser.feed((UI / "index.html").read_text())
    icon_buttons = [attrs for tag, attrs in parser.tags if tag == "button" and "icon-button" in (attrs.get("class") or "")]
    assert icon_buttons
    assert all(attrs.get("aria-label") and attrs.get("data-tooltip") for attrs in icon_buttons)
    assert sum(1 for _, attrs in parser.tags if attrs.get("data-tooltip")) >= 20
    assert not any(attrs.get("data-view") == "agents" for _, attrs in parser.tags)


def test_help_inventory_has_no_duplicate_global_commands_or_top_layer_gap():
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    parser = InventoryParser()
    parser.feed(html)
    contextual_classes = {"icon-button", "quiet-icon", "activity", "send-button"}
    contextual_controls = [
        attrs for tag, attrs in parser.tags
        if tag == "button" and contextual_classes.intersection((attrs.get("class") or "").split())
    ]
    assert contextual_controls
    assert all(attrs.get("data-tooltip") for attrs in contextual_controls)
    icon_only = [attrs for attrs in contextual_controls if {"icon-button", "quiet-icon", "activity", "send-button"}.intersection((attrs.get("class") or "").split())]
    assert all(attrs.get("aria-label") for attrs in icon_only)
    assert html.count('aria-label="New project"') == 1
    assert 'id="new-project-btn"' not in html
    assert 'id="status-mode"' not in html
    assert ">Dendrogram<" not in html
    assert "Defaults & recovery settings" not in js
    assert sum(1 for _, attrs in parser.tags if attrs.get("data-tooltip")) >= 20
    assert 'target.closest(".side-panel:not([hidden])") || document.body' in js
    assert 'document.body.append($("#tooltip"))' in js
    assert 'resolveShortcut(event, data.app)' in js
    assert js.count('await safe(() => request("/api/settings"') >= 1
    assert '<dialog id="confirm-dialog"' in html
    assert 'id="settings-panel"' in html and 'id="policy-panel"' in html


def test_semantic_design_tokens_and_transparent_code_native_brand_assets():
    css = (UI / "style.css").read_text()
    for token in (
        "--surface-0", "--text-2", "--accent", "--space-1", "--space-5",
        "--control-xs", "--control-lg", "--icon-xs", "--icon-lg",
        "--radius-sm", "--radius-lg", "--motion-fast", "--motion-normal",
    ):
        assert token in css
    html = (UI / "index.html").read_text()
    # The shipped shell uses transparent SVG/CSS geometry only. Opaque raster
    # references cannot accidentally return to onboarding or app chrome.
    assert "<img" not in html
    assert not re.search(r"assets/[^\"']+\.(?:png|webp|jpe?g)", html, re.I)
    assert "background-image: url(" not in css


def test_wordmark_only_chrome_and_sober_authoring_are_production_ready():
    html = (UI / "index.html").read_text()
    assert '/assets/turn-logo.svg' not in html
    assert 'class="brand-mark"' not in html
    assert '<button class="brand" id="home-btn"' in html
    assert '<img' not in html
    assert 'class="welcome-visual"' not in html and 'class="galaxy-core"' not in html
    assert 'id="help-btn"' in html


def test_welcome_liveness_and_graph_context_actions_are_accessible():
    html = (UI / "index.html").read_text()
    css = (UI / "style.css").read_text()
    js = (UI / "app.js").read_text()
    assert 'class="galaxy-orbit' not in html
    assert 'class="galaxy-node' not in html
    assert 'from "/vendor/xterm/lib/xterm.mjs"' in js
    assert "new WebSocket" in js and "terminal.onData" in js and "terminal.onResize" in js
    assert "prefers-reduced-motion: reduce" in css
    assert 'class: "node-select"' in js and 'role: "menuitem"' in js
    assert '"data-node-menu": node.id' in js
    assert 'event.key === "ContextMenu"' in js
    for label in ("Open inspector", "View terminal", "View run history", "Pause branch", "Resume branch", "Cancel branch"):
        assert label in js


def test_dendrogram_and_model_dependent_reasoning_contracts_are_wired():
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    assert "Workgraph dendrogram" in html and 'aria-label="Graph relationships"' in html
    assert 'from "./dendrogram.js"' in js
    assert "dendrogramPath(" in js and "layoutDendrogram(" in js
    assert 'list="new-model-options"' in html
    assert "reasoningLevels(" in js and "reasoning_profiles" in js
    assert "session_id: harness.value === config.harness" in js


def test_switching_harnesses_drops_the_incompatible_provider_session():
    js = (UI / "app.js").read_text()
    assert "harness.value === config.harness ? config.session_id : null" in js
    assert "session_id:" in js


def test_information_hierarchy_has_one_home_for_each_fact():
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    css = (UI / "style.css").read_text()
    assert 'id="new-name"' in html
    assert 'id="status-project"' not in html + js
    assert 'id="workspace-usage"' not in html + js
    assert 'class="sidebar-footer"' not in html
    assert 'id="crumbs"' not in html + js
    assert 'id="live-state"' not in html + js
    assert 'id="graph-legend"' in html.split('<footer class="statusbar">', 1)[1]
    assert 'id="graph-zoom"' in html.split('<footer class="statusbar">', 1)[1]
    assert "terminal-future" not in html + js + css
    assert "PTY future" not in html + js
    assert 'class: "agent-config inline-form"' in js
    assert 'class: "node-config"' in js
    assert "node.agent?.mcp_servers?.length" in js
    assert ".node-title" in css and "white-space:nowrap" in css
    # Branch-wide actions are progressively disclosed in the graph menu once,
    # rather than repeated as a row of oversized inspector buttons.
    assert js.count('menuItem("Pause branch"') == 1
    assert js.count('menuItem("Resume branch"') == 1
    assert js.count('menuItem("Cancel branch"') == 1
    assert 'row.append(button("Pause branch"' not in js
    assert js.count('["fork", "Create fork", "git-fork"]') == 1
    assert 'fork: "Create fork"' not in js
    assert 'hasProject ? "Workgraph ready"' not in js
    assert 'hasProject ? workgraphStatusMessage()' in js


def test_authoring_is_one_visible_surface_and_project_identity_is_contextual():
    html = (UI / "index.html").read_text()
    js = (UI / "app.js").read_text()
    assert html.count('id="author-prompt"') == 1
    assert 'id="new-prompt"' not in html + js
    assert 'id="project-dialog"' not in html + js
    for control in ("new-dir", "new-harness", "new-model", "new-reasoning", "new-permission", "new-auto", "new-sequential", "new-delay"):
        assert f'id="{control}"' in html
    assert 'class: "project-item-actions"' in js
    assert 'showProjectMenu(project' in js
    assert 'json("PATCH", { name })' in js
    assert 'Remove from Turn…' in js


def test_design_and_scope_keep_current_and_future_capabilities_separate():
    design = (ROOT / "DESIGN.md").read_text()
    audit = (ROOT / "docs" / "GAP_AUDIT.md").read_text()
    assert "One fact gets one primary home" in design
    assert "Future-ready only" in design
    assert "Current implementation" in audit and "Future-ready only" in audit
    assert "must not render dead “future” controls" in design


def test_review_copy_and_internal_harnesses_are_derived_from_real_state():
    js = (UI / "app.js").read_text()
    assert 'node.review_owner === "parent" ? "Parent verification" : "Review"' in js
    assert 'text: "Descendant review required"' in js
    assert 'option.value === config.harness' in js
    assert 'text: `${config.harness} · internal`' in js
