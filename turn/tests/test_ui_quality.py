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
    assert "GraphResponse" in source("domain.ts")


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
    assert "node.generation_active" in graph and 'className="run-spinner"' in graph
    assert 'item["generation_active"]' in api
    assert ".edge-active" not in css and "@keyframes flow" not in css
    assert "node-breathe" not in css


def test_terminal_separates_machine_result_from_shadow_dom_presentation():
    terminal = source("components", "TerminalView.tsx")
    transport = (ROOT / "turn" / "workers" / "terminal.py").read_text()
    worker = (ROOT / "turn" / "workers" / "codex_worker.py").read_text()
    assert "attachShadow" in terminal and "Terminal(" in terminal
    assert "HarnessOutputPresenter" in transport and "display_output" in transport
    assert '"--output-last-message", result_path' in worker
    assert "raw_stdout" in worker and "terminal.display_output" in worker


def test_inspector_prioritizes_markdown_instructions_and_human_diffs():
    inspector = source("components", "Inspector.tsx")
    diff = source("components", "DiffView.tsx")
    assert inspector.index("Agent instructions") < inspector.index("Agent configuration")
    assert "ReactMarkdown" in inspector and 'tab === "diff"' in inspector
    assert "diff-add" in diff and "diff-del" in diff
    assert "Save instructions" in inspector and "disabled={!scopeDirty}" in inspector
    assert "disabled={!agentDirty}" in inspector


def test_model_and_reasoning_controls_share_dynamic_capabilities():
    control = source("components", "ModelControl.tsx")
    harnesses = (ROOT / "turn" / "workers" / "harnesses.py").read_text()
    assert "capability?.models" in control
    assert "selected?.reasoning ?? capability?.reasoning" in control
    assert "<datalist" not in control
    assert "value={model}" in control and "Harness default" in control
    assert "MODEL_DISCOVERY_COMMANDS" in harnesses
    assert '"--offline", "--list-models"' in harnesses


def test_parent_review_copy_has_mutually_exclusive_terminal_states():
    inspector = source("components", "Inspector.tsx")
    assert 'status === "accepted"' in inspector
    assert "Accepted by parent" in inspector
    assert "Awaiting parent verification" in inspector
    assert "parent && status === \"accepted\"" in inspector
    assert "!parent && node.merge_accepted" in inspector
    assert "!/\\b(awaiting|waiting)\\b/i" in inspector


def test_only_readme_and_design_are_product_markdown_documents():
    product_docs = sorted(path.name for path in ROOT.glob("*.md"))
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
    assert "GRAPH_PADDING" in layout and 'type === "DEPENDS_ON"' in layout
    assert "applyAppearance(settings)" in app
    assert "Next turn" in app and "Auto verify" in app
