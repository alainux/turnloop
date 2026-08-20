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
    assert (UI / "icons" / "loader.svg").exists()
    assert 'src={`/icons/${name}.svg`}' in icon
    assert 'aria-label="Toggle projects"' in app
    assert 'aria-label="New project"' in app
    assert 'aria-label="Workspace settings"' in app
    assert "hero" not in app.lower() and "logo" not in app.lower()


def test_graph_motion_is_truthful_and_manual_run_is_first_class():
    graph = source("components", "Graph.tsx")
    app = source("App.tsx")
    css = (UI / "style.css").read_text()
    api = (ROOT / "turn" / "server" / "api.py").read_text()
    assert 'const runAction = active ? "cancel" : primaryAction;' in graph
    assert "node.generation_active" in graph
    assert 'name={nodeAgentIcon(node)}' in graph
    assert 'name={nodeRunIcon(active, runAction, freshRun)}' in graph
    assert 'return "rotate-cw"' in graph
    assert 'const preparing = node.ui_state === "preparing";' in graph
    assert 'preparing\n                        ? "loader"' not in graph
    inspector = source("components", "Inspector.tsx")
    css = (UI / "style.css").read_text()
    assert 'primaryAction === "cancel" ? "danger stop-action"' in inspector
    assert ".node-run.running" in css and ".stop-action" in css
    assert 'item["generation_active"]' in api
    assert 'message_type == "scroll"' in api
    assert ".edge-active" not in css and "@keyframes flow" not in css
    assert "node-breathe" not in css
    assert "displayEdges" in graph and "visibleEdges" in graph
    assert "GRAPH_PADDING" in graph
    assert "layout.edgePaths.get(edge.id)" in graph
    assert "returnPathBetween(a, b)" in graph
    assert 'data-flow-edge="true"' in graph
    assert "flow_edges" in app
    assert ".edge-flow-return" in css
    assert "<g transform=" not in graph
    assert "align-items: flex-start" in css
    assert "align-self: flex-start" in css
    assert "node-icons" in graph and "bottom-right" not in graph
    assert "error instanceof ApiError" in app
    assert "error.status === 404" in app
    assert "clearDeletedProject" in app


def test_planner_document_and_role_defaults_are_first_class():
    app = source("App.tsx")
    document = source("components", "DocumentView.tsx")
    css = (UI / "style.css").read_text()
    api = (ROOT / "turn" / "server" / "api.py").read_text()
    assert 'id: "planner"' in app and 'id: "verifier"' in app
    assert "agent_defaults" in app
    assert "documentParentMap" in document
    assert "agent_defaults" in api
    assert ".document-reader-content pre" in css
    assert "architecture-filesystem" not in css
    for role in ("planner", "executor", "integrator", "verifier"):
        assert f'id: "{role}"' in app
    assert "agent-default-role" in app


def test_document_reader_keeps_references_generic_and_explicit():
    document = source("components", "DocumentView.tsx")
    assert "MarkdownContent" in document
    assert "projectPathHref" in document
    assert "DocumentReader" in document
    assert "architecture_spec" not in document
    assert "<ArchitectureDocument" not in document
    assert "components={{" in document
    assert "img: ({ src, alt" in document


def test_terminal_is_a_raw_dom_pty_view():
    terminal = source("components", "TerminalView.tsx")
    css = (UI / "style.css").read_text()
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
    assert 'addEventListener("wheel"' in terminal
    assert 'removeEventListener("wheel"' in terminal
    assert "scrollback: 0" in terminal
    assert "overflow:hidden!important" in terminal
    assert "terminal.scrollToBottom()" not in terminal
    assert "terminal.onScroll" not in terminal
    assert ":host{" not in terminal
    assert "convertEol: true" in terminal
    assert "line-height:1!important" in terminal
    assert "lineHeight: 1" in terminal
    assert "HarnessOutputPresenter" not in transport
    assert "is_native_command" not in transport
    assert "display_output" in transport and "raw stream" in transport
    assert "--output-last-message" not in worker
    assert "--output-schema" not in worker
    # Normal Codex execution remains a raw interactive terminal with the
    # documented notify sidecar. A non-interactive transport gets JSONL
    # automatically; neither source is inferred from a PTY transcript.
    assert "prepare_codex_notify_telemetry" in worker and '"--json"' in worker
    # Provider output stays in Herdr's session log during normal use. The
    # machine-transport branch may consume its documented JSONL channel,
    # never ``display_output`` or an ANSI transcript.
    assert "terminal.display_output" not in worker
    assert "machine_output_path.read_text" in worker
    assert "await emit_jsonl_telemetry" in worker
    assert "Telemetry {telemetryStatus(telemetry)}" in terminal
    assert "telemetry-unavailable" in css


def test_quality_panel_reuses_the_logs_panel_shell_and_record_layout():
    quality = source("components", "QualityPanel.tsx")
    css = (UI / "style.css").read_text()
    assert 'className="logs-panel quality-panel"' in quality
    assert '<div className="quality-content">' in quality
    assert ".quality-content" in css
    assert ".quality-agent pre, .quality-run pre" in css
    assert "border-left: 2px solid var(--border-strong);" in css


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
    assert "Review" not in inspector
    assert "/accept" not in inspector and "/reject" not in inspector


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


def test_composer_submit_states_use_design_tokens():
    css = (UI / "style.css").read_text()
    assert ".send-button:disabled" in css
    assert "background: var(--accent);" in css
    assert "background: var(--surface-3);" in css
    assert ".review-card" not in css


def test_inspector_always_cascades_agent_configuration():
    inspector = source("components", "Inspector.tsx")
    runner = (ROOT / "turn" / "runner" / "runner.py").read_text()
    assert "Cascade options" not in inspector
    assert "if node.agent is not None:" in runner


def test_review_surface_is_not_exposed():
    inspector = source("components", "Inspector.tsx")
    assert "Accept result" not in inspector
    assert "Request changes" not in inspector
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
    css = (UI / "style.css").read_text()
    assert "useState(false)" in app
    assert 'sidebar ? "" : "sidebar-collapsed"' in app
    assert 'aria-label="Project objective"' in app
    assert 'aria-label="Attach files"' in app
    assert 'aria-label="Choose project directory"' in app
    assert "directory-selection" in app and "attachment-chip" in app
    assert 'type="file"' in app and 'multiple' in app
    assert "composer-config-panel" in app
    assert "form.composer:has(.composer-config-panel)" in css
    assert "flex: 0 0 145px" in css


def test_object_context_menus_edges_and_theme_are_wired_to_real_actions():
    app = source("App.tsx")
    graph = source("components", "Graph.tsx")
    layout = source("layout.ts")
    assert 'role="menu"' in app and "Delete project" in app
    assert "onContextMenu" in graph and "node-menu-trigger" in graph
    assert "GRAPH_PADDING" in layout and "edge-workflow" in graph
    assert "displayEdges" in layout and "sequenceProjection" in layout
    assert "applyAppearance(settings)" in app
    assert "Next stage" in app and "Auto-run" in app


def test_project_deletion_requires_explicit_destructive_options_and_reports_progress():
    app = source("App.tsx")
    css = (UI / "style.css").read_text()
    api = (ROOT / "turn" / "server" / "api.py").read_text()
    conversations = (ROOT / "turn" / "workers" / "conversations.py").read_text()
    assert "project-delete-dialog" in app
    assert 'Delete files from disk' in app and 'Delete conversations' in app
    assert "setDeleteFiles(false)" in app and "setDeleteConversations(false)" in app
    assert 'json("DELETE"' in app
    assert "project.deletion_progress" in app and 'aria-live="polite"' in app
    assert ".modal-scrim" in css and ".delete-option" in css
    assert "DeleteProjectOptions" in api
    assert "conversation_refs" in api and "cleanup_conversations" in api
    assert "conversation_delete_command" in conversations
    assert "does not support non-interactive conversation deletion" in conversations


def test_document_view_is_a_read_only_spec_projection():
    app = source("App.tsx")
    document = source("components", "DocumentView.tsx")
    css = (UI / "style.css").read_text()
    assert "DocumentView" in app and 'viewMode === "document"' in app
    assert "Read-only specification" in document
    assert "orderDocumentNodes" in document
    assert "ReactMarkdown" in document
    # The document remains read-only. It may expose navigation controls for
    # opening and returning from linked project documents, but never editing
    # controls or form fields.
    assert "document-reader-back" in document
    assert "<input" not in document and "<textarea" not in document
    assert ".document-children" in css and ".document-node-summary" in css
    assert "#app-shell .document-view" in css
    assert "#app-shell .document-view *" in css
    assert "user-select: text" in css
    assert "className=\"project-path\"" in app
    assert "#app-shell .project-title small.project-path" in css
    assert "depth-${Math.min(Math.max(path.length - 1, 0), 4)}" in document
    assert "--document-indent" in css
    assert ".document-node.depth-4" in css
    assert ".document-node.depth-0 > .document-node-body" in css
    assert "padding: 0 4px 22px;" in css
