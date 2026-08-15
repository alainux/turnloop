"""Static anti-drift contracts for the typed React workbench."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
SRC = UI / "src"


def source(*parts: str) -> str:
    return (SRC.joinpath(*parts)).read_text()


def test_runtime_is_strict_typed_react_without_legacy_parallel_ui():
    package = (ROOT / "package.json").read_text()
    tsconfig = (ROOT / "tsconfig.json").read_text()
    assert '"react"' in package and '"typescript"' in package
    assert '"strict": true' in tsconfig and '"allowJs": false' in tsconfig
    assert not (UI / "app.js").exists()
    assert "createRoot" in source("main.tsx")
    assert "GraphView" in source("domain.ts")
    assert "./generated/domain" in source("domain.ts")


def test_professional_icons_and_accessible_icon_controls_are_componentized():
    app = source("App.tsx")
    icon = source("components", "Icon.tsx")
    assert (UI / "icons" / "LICENSE-lucide.txt").exists()
    assert 'src={`/icons/${name}.svg`}' in icon
    assert 'aria-label="Toggle projects"' in app
    assert 'aria-label="New project"' in app
    assert 'aria-label="Workspace settings"' in app
    assert "hero" not in app.lower() and "logo" not in app.lower()


def test_graph_motion_is_truthful_and_manual_run_is_first_class():
    graph = source("components", "Graph.tsx")
    css = (UI / "style.css").read_text()
    api = (ROOT / "turn" / "server" / "api.py").read_text()
    assert 'node.allowed_actions.includes("run")' in graph
    assert "node.generation_active" in graph and 'name={running ? "square-stop" : "play"}' in graph
    assert 'item["generation_active"]' in api
    assert ".edge-active" not in css and "@keyframes flow" not in css
    assert "node-breathe" not in css
    assert "displayEdges" in graph and "visibleEdges" in graph
    assert "GRAPH_PADDING" in graph
    assert "pathBetween(a, b, edge.type)" in graph
    assert "<g transform=" not in graph
    assert "align-items: safe center" in css


def test_terminal_is_a_raw_dom_pty_view():
    terminal = source("components", "TerminalView.tsx")
    transport = (ROOT / "turn" / "workers" / "terminal.py").read_text()
    worker = (ROOT / "turn" / "workers" / "codex_worker.py").read_text()
    assert "attachShadow" not in terminal and "Terminal(" in terminal
    assert "terminal.open(mount)" in terminal
    assert 'message.type === "output"' in terminal and "activate(true)" in terminal
    assert "socket.onopen = () =>" in terminal and "setConnection(\"connected\")" in terminal
    assert "terminal.onBinary((value) => sendTerminalInput(value, true))" in terminal
    assert 'encoding: "base64"' in terminal
    assert "Show output" not in terminal and "Hide output" not in terminal
    assert "persistent Herdr shell" in terminal
    assert "Connection interrupted; reconnecting" in terminal
    assert ":host{" not in terminal
    assert "convertEol: true" in terminal
    assert "line-height:1!important" in terminal
    assert "lineHeight: 1" in terminal
    assert "HarnessOutputPresenter" not in transport
    assert "is_native_command" not in transport
    assert "display_output" in transport and "raw stream" in transport
    assert "--output-last-message" not in worker
    assert "--output-schema" not in worker
    assert "\"--json\"" not in worker
    assert "terminal.output" in worker and "terminal.display_output" in worker


def test_inspector_prioritizes_markdown_instructions_without_legacy_review_surfaces():
    inspector = source("components", "Inspector.tsx")
    assert inspector.index("Agent instructions") < inspector.index("Agent configuration")
    assert "ReactMarkdown" in inspector
    assert 'tab === "diff"' not in inspector
    assert 'tab === "history"' not in inspector
    assert 'Fork alternative' not in inspector
    assert 'Revision {node.revision}' not in inspector
    assert "Save instructions" in inspector and "disabled={!scopeDirty}" in inspector
    assert "disabled={!agentDirty}" in inspector


def test_model_and_reasoning_controls_share_dynamic_capabilities():
    control = source("components", "ModelControl.tsx")
    app = source("App.tsx")
    harnesses = (ROOT / "turn" / "workers" / "harnesses.py").read_text()
    assert "capability?.models" in control
    assert "selected?.reasoning ?? capability?.reasoning" in control
    assert "<datalist" not in control
    assert "role=\"combobox\"" in control
    assert "searchable-select-menu" in control
    assert 'value={loading ? "" : model}' in control and "Harness default" in control
    assert "MODEL_DISCOVERY_COMMANDS" in harnesses
    catalog = (ROOT / "turn" / "workers" / "harness_catalog.py").read_text()
    assert '"--offline"' in catalog and '"--list-models"' in catalog
    assert 'Loading harnesses…' in control
    assert "capabilitiesLoading" in app


def test_composer_submit_states_and_review_copy_use_design_tokens():
    css = (UI / "style.css").read_text()
    assert ".send-button:disabled" in css
    assert "background: var(--accent);" in css
    assert "background: var(--surface-3);" in css
    assert ".review-card p" in css and "color: var(--text-2);" in css


def test_inspector_always_cascades_agent_configuration():
    inspector = source("components", "Inspector.tsx")
    runner = (ROOT / "turn" / "runner" / "runner.py").read_text()
    assert "Cascade options" not in inspector
    assert "if node.agent is not None:" in runner


def test_review_copy_has_one_explicit_user_controlled_state():
    inspector = source("components", "Inspector.tsx")
    assert '<span>Review</span>' in inspector
    assert "Accept result" in inspector
    assert "Request changes" in inspector
    assert "Auto verification" not in inspector
    assert "Awaiting parent verification" not in inspector


def test_only_readme_and_design_are_product_markdown_documents():
    product_docs = sorted(
        path.name for path in ROOT.glob("*.md") if path.name != "AGENTS.md"
    )
    assert product_docs == ["DESIGN.md", "README.md"]
    assert not list((ROOT / "docs").glob("*.md"))
    readme = (ROOT / "README.md").read_text()
    design = (ROOT / "DESIGN.md").read_text()
    assert "Current scope" in readme and "Future-ready seams" in readme
    assert "truthfulness rule" in design and "Shadow DOM xterm" in design


def test_authoring_starts_central_and_sidebar_is_collapsed():
    app = source("App.tsx")
    assert "useState(false)" in app
    assert 'sidebar ? "" : "sidebar-collapsed"' in app
    assert 'aria-label="Project objective"' in app
    assert 'aria-label="Attach files"' in app
    assert 'aria-label="Choose project directory"' in app
    assert "directory-selection" in app and "attachment-chip" in app


def test_object_context_menus_edges_and_theme_are_wired_to_real_actions():
    app = source("App.tsx")
    graph = source("components", "Graph.tsx")
    layout = source("layout.ts")
    assert 'role="menu"' in app and "Delete project" in app
    assert "onContextMenu" in graph and "node-menu-trigger" in graph
    assert "GRAPH_PADDING" in layout and "edge-workflow" in graph
    assert "displayEdges" in layout and "hasAlternativePath" in layout
    assert "applyAppearance(settings)" in app
    assert "Next turn" in app and "Auto-run" in app
