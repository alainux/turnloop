import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type {
  Agent,
  Capabilities,
  GraphNode,
  Graph,
  HarnessId,
  Project,
  ProjectsResponse,
  Reasoning,
  RunPolicy,
  UsageResponse,
} from "./domain";
import {
  isGraph,
  primaryNodeAction,
  primaryNodeActionLabel,
  tokens,
} from "./domain";
import { api, json } from "./api";
import { Graph as GraphCanvas } from "./components/Graph";
import { DocumentView } from "./components/DocumentView";
import { Icon } from "./components/Icon";
import { Inspector } from "./components/Inspector";
import { ModelControl } from "./components/ModelControl";
import { deriveStatus } from "./state";

const defaultPolicy: RunPolicy = {
  auto_run: false,
  delay_between_jobs_ms: 0,
  timeout_seconds: 600,
  stall_timeout_seconds: 90,
  max_retries: 1,
  retry_backoff_ms: 750,
  retry_choked_models: true,
  compact_on_context_pressure: true,
};
const emptyAgent: Agent = {
  id: crypto.randomUUID(),
  type_id: "planner",
  harness: "codex",
  model: null,
  reasoning: "default",
  permission: "workspace",
  skills: [],
  tools: [],
  mcp_servers: [],
  session_id: null,
};

type ResizeTarget = "sidebar" | "inspector";

function usePanelResize() {
  const [sidebarWidth, setSidebarWidth] = useState(236);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [resizing, setResizing] = useState<ResizeTarget | null>(null);

  useEffect(() => {
    if (!resizing) return;
    const onMove = (event: PointerEvent) => {
      if (resizing === "sidebar") {
        setSidebarWidth(Math.min(420, Math.max(180, event.clientX)));
      } else {
        setInspectorWidth(
          Math.min(520, Math.max(280, window.innerWidth - event.clientX)),
        );
      }
    };
    const stop = () => setResizing(null);
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", stop, { once: true });
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    return () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", stop);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [resizing]);

  const beginResize = (target: ResizeTarget, event: React.PointerEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setResizing(target);
  };

  const adjustResize = (target: ResizeTarget, delta: number) => {
    if (target === "sidebar") {
      setSidebarWidth((value) => Math.min(420, Math.max(180, value + delta)));
    } else {
      setInspectorWidth((value) => Math.min(520, Math.max(280, value + delta)));
    }
  };

  return {
    sidebarWidth,
    inspectorWidth,
    resizing,
    beginResize,
    adjustResize,
  };
}

function ResizeHandle({
  target,
  value,
  onResize,
  onAdjust,
}: {
  target: ResizeTarget;
  value: number;
  onResize: (target: ResizeTarget, event: React.PointerEvent) => void;
  onAdjust: (target: ResizeTarget, delta: number) => void;
}) {
  const label = target === "sidebar" ? "Projects panel width" : "Inspector panel width";
  return (
    <div
      className={`resize-handle ${target}-resize`}
      role="separator"
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={target === "sidebar" ? 180 : 280}
      aria-valuemax={target === "sidebar" ? 420 : 520}
      aria-valuenow={value}
      tabIndex={0}
      onPointerDown={(event) => onResize(target, event)}
      onKeyDown={(event) => {
        const direction = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
        if (!direction) return;
        event.preventDefault();
        const step = event.shiftKey ? 40 : 10;
        onAdjust(target, direction * step * (target === "sidebar" ? 1 : -1));
      }}
    >
      <span />
    </div>
  );
}

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const graphLoadVersion = useRef(0);
  const [capabilities, setCapabilities] = useState<Capabilities>({
    harnesses: [],
  });
  const [workspaceSettings, setWorkspaceSettings] = useState<Record<string, unknown> | null>(null);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<"graph" | "document">("graph");
  const [sidebar, setSidebar] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [policyOpen, setPolicyOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const { sidebarWidth, inspectorWidth, beginResize, adjustResize } = usePanelResize();
  const [nodeMenu, setNodeMenu] = useState<{
    node: GraphNode;
    x: number;
    y: number;
  } | null>(null);
  const notify = useCallback((text: string) => {
    // Terminal sessions are the conversation surface. Keep failures visible
    // to developers without interrupting the workgraph with transient toasts.
    console.error(`[Turn] ${text}`);
  }, []);
  const loadProjects = useCallback(async () => {
    const result = await api<ProjectsResponse>("/api/projects");
    setProjects(result.projects);
  }, []);
  const loadGraph = useCallback(async () => {
    if (!projectId) return;
    const version = ++graphLoadVersion.current;
    const [next, nextUsage] = await Promise.all([
      api<unknown>(`/api/projects/${projectId}/graph`),
      api<UsageResponse>(`/api/projects/${projectId}/usage`),
    ]);
    if (version !== graphLoadVersion.current) return;
    if (!isGraph(next))
      throw new Error("Server returned an invalid graph schema");
    setGraph(next);
    setUsage(nextUsage);
    await loadProjects();
  }, [projectId, loadProjects]);
  useEffect(() => {
    setCapabilitiesLoading(true);
    void Promise.all([
      loadProjects(),
      api<Capabilities>("/api/capabilities").then(setCapabilities),
      api<Record<string, unknown>>("/api/settings").then((value) => {
        setWorkspaceSettings(value);
        applyAppearance(value);
      }),
    ])
      .catch((error) => notify(String(error)))
      .finally(() => setCapabilitiesLoading(false));
  }, [loadProjects, notify]);
  useEffect(() => {
    setSelected(null);
    setViewMode("graph");
    setPolicyOpen(false);
    if (!projectId) {
      return;
    }
    void loadGraph();
    const stream = new EventSource(`/api/projects/${projectId}/stream`);
    stream.onopen = () => setConnected(true);
    stream.onerror = () => setConnected(false);
    stream.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as {
          type?: string;
          project_id?: string;
        };
        if (message.type === "project.deleted" && message.project_id === projectId) {
          setProjectId(null);
          setSelected(null);
          setGraph(null);
          setUsage(null);
          void loadProjects();
          return;
        }
        if (message.type !== "heartbeat" && message.type !== "node.terminal")
          void loadGraph();
      } catch {
        /* ignore malformed external event */
      }
    };
    return () => stream.close();
  }, [projectId, loadGraph]);
  useEffect(() => {
    const interval = window.setInterval(() => {
      void loadProjects();
    }, 2000);
    return () => window.clearInterval(interval);
  }, [loadProjects]);
  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "b") {
        event.preventDefault();
        setSidebar((value) => !value);
      }
      if ((event.metaKey || event.ctrlKey) && event.key === ",") {
        event.preventDefault();
        setSettingsOpen((value) => !value);
      }
    };
    addEventListener("keydown", listener);
    return () => removeEventListener("keydown", listener);
  }, []);
  useEffect(() => {
    if (!nodeMenu) return;
    const close = () => setNodeMenu(null);
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    addEventListener("pointerdown", close);
    addEventListener("keydown", key);
    return () => {
      removeEventListener("pointerdown", close);
      removeEventListener("keydown", key);
    };
  }, [nodeMenu]);
  const graphReady = Boolean(projectId && graph?.project_id === projectId);
  const root = graphReady
    ? graph?.nodes.find((node) => node.id === projectId)
    : undefined;
  const selectedProject = projects.find((node) => node.id === projectId);
  const projectTitle = root || selectedProject;
  const status = deriveStatus(graphReady ? graph?.nodes ?? [] : []);
  return (
    <div
      id="app-shell"
      className={`${sidebar ? "" : "sidebar-collapsed"} ${selected ? "inspector-open" : ""}`}
      style={
        {
          "--sidebar-w": `${sidebarWidth}px`,
          "--inspector-w": `${inspectorWidth}px`,
        } as CSSProperties
      }
    >
      <header className="titlebar">
        <button
          className="icon-button"
          onClick={() => setSidebar((value) => !value)}
          title="Toggle projects (⌘B)"
          aria-label="Toggle projects"
        >
          <Icon name="panel-left" />
        </button>
        <button className="brand" onClick={() => setProjectId(null)}>
          Turn
        </button>
        <span className="title-spacer" />
        {projectId && (
          <button
            className="icon-button"
            onClick={() => setProjectId(null)}
            title="New project"
            aria-label="New project"
          >
            <Icon name="plus" />
          </button>
        )}
        <button
          className="icon-button"
          onClick={() => setSettingsOpen(true)}
          title="Workspace settings"
          aria-label="Workspace settings"
        >
          <Icon name="settings-2" />
        </button>
      </header>
      <Projects
        projects={projects}
        selected={projectId}
        open={sidebar}
        onSelect={setProjectId}
        onChanged={loadProjects}
        onDeleted={(id) => {
          if (id === projectId) {
            setProjectId(null);
            setGraph(null);
            setSelected(null);
          }
        }}
        notify={notify}
      />
      {sidebar && (
        <ResizeHandle
          target="sidebar"
          value={sidebarWidth}
          onResize={beginResize}
          onAdjust={adjustResize}
        />
      )}
      <main className="workspace">
        {projectId ? (
          <section id="graph-view" className="graph-view">
            <div className="workspace-toolbar">
              <div className="project-title">
                <div className="project-title-copy">
                  <strong>
                    {projectTitle?.project_name || projectTitle?.objective || "Loading project…"}
                  </strong>
                  <small className="project-path" title="Project directory">
                    {projectTitle?.repo_path || "Current directory"}
                  </small>
                </div>
              </div>
              <div className="toolbar-actions">
                <div className="segmented view-toggle" role="tablist" aria-label="Project view">
                  <button
                    role="tab"
                    aria-selected={viewMode === "graph"}
                    className={viewMode === "graph" ? "selected" : ""}
                    onClick={() => setViewMode("graph")}
                  >
                    Graph
                  </button>
                  <button
                    role="tab"
                    aria-selected={viewMode === "document"}
                    className={viewMode === "document" ? "selected" : ""}
                    onClick={() => {
                      setSelected(null);
                      setViewMode("document");
                    }}
                  >
                    Document
                  </button>
                </div>
                <div className="segmented">
                  <button
                    className={root?.run_policy?.auto_run ? "selected" : ""}
                    disabled={!root}
                    onClick={() => void setMode(projectId, true, loadGraph)}
                  >
                    Auto
                  </button>
                  <button
                    className={root && !root.run_policy?.auto_run ? "selected" : ""}
                    disabled={!root}
                    onClick={() => void setMode(projectId, false, loadGraph)}
                  >
                    Step
                  </button>
                </div>
                {root && !root.run_policy?.auto_run && (
                  <button
                    className="button accent"
                    onClick={() =>
                      void api(`/api/projects/${projectId}/step`, {
                        method: "POST",
                      }).then(loadGraph)
                    }
                  >
                    Next stage
                  </button>
                )}
                <button
                  className="icon-button"
                  disabled={!root}
                  onClick={() => setPolicyOpen(true)}
                  aria-label="Project execution policy"
                >
                  <Icon name="gauge" />
                </button>
              </div>
            </div>
            {graphReady && root ? (
              viewMode === "document" ? (
                <DocumentView
                  nodes={graph!.nodes}
                  edges={graph!.edges}
                  architectureSpec={graph!.architecture_spec}
                />
              ) : (
                <div id="graph" className="graph">
                  <div className="graph-guide" aria-label="Graph guide">
                    <span><i className="legend-line contains" /> nesting</span>
                    <span><i className="legend-line depends" /> dependency — must finish first</span>
                    <span>same stage = can run in parallel</span>
                  </div>
                  <GraphCanvas
                    nodes={graph!.nodes}
                    edges={graph!.edges}
                    usage={usage?.by_node ?? {}}
                    selected={selected}
                    onSelect={setSelected}
                    onRun={(node, action) =>
                      void api(`/api/nodes/${node.id}/${action}`, { method: "POST" })
                        .then(loadGraph)
                        .catch((error) => notify(String(error)))
                    }
                    onContextMenu={(node, x, y) => setNodeMenu({ node, x, y })}
                  />
                </div>
              )
            ) : (
              <div className="project-loading" aria-live="polite">
                Loading project…
              </div>
            )}
          </section>
        ) : (
          <Author
            capabilities={capabilities}
            capabilitiesLoading={capabilitiesLoading}
            workspaceSettings={workspaceSettings}
            onCreated={(id) => {
              setProjectId(id);
              void loadProjects();
            }}
            notify={notify}
          />
        )}
      </main>
      {selected && (
        <>
          <ResizeHandle
            target="inspector"
            value={inspectorWidth}
            onResize={beginResize}
            onAdjust={adjustResize}
          />
          <Inspector
            nodeId={selected}
            refreshKey={(() => {
              const node = graph?.nodes.find((item) => item.id === selected);
              return node
                // Dependency evaluation can change the projected UI state
                // without changing the node's persisted timestamp. Include
                // that projection so a selected inspector never keeps saying
                // "waiting dependency" after its parents complete.
                ? `${node.updated_at}:${node.status}:${node.ui_state}:${node.state_reason}:${node.generation_active}`
                : selected;
            })()}
            capabilities={capabilities.harnesses}
            onClose={() => setSelected(null)}
            onChanged={loadGraph}
            notify={notify}
          />
        </>
      )}
      <footer className="statusbar">
        <span>{status}</span>
        <span>
          {connected && projectId
            ? "Connected"
            : projectId
              ? "Reconnecting"
              : "Ready"}
        </span>
        <span className="status-spacer" />
        {graphReady && graph && (
          <>
            <span>{graph.nodes.length} nodes</span>
            <span>{tokens(usage?.totals).toLocaleString()} tokens</span>
          </>
        )}
      </footer>
      {settingsOpen && (
        <Settings
          capabilities={capabilities}
          onClose={() => setSettingsOpen(false)}
          notify={notify}
        />
      )}
      {policyOpen && root?.run_policy && (
        <Policy
          projectId={projectId!}
          policy={root.run_policy}
          onClose={() => setPolicyOpen(false)}
          onSaved={loadGraph}
          notify={notify}
        />
      )}
      {nodeMenu && (
        <div
          className="popover context-menu"
          role="menu"
          style={menuPosition(nodeMenu.x, nodeMenu.y)}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <div className="popover-label">Node actions</div>
          <button
            role="menuitem"
            onClick={() => {
              setSelected(nodeMenu.node.id);
              setNodeMenu(null);
            }}
          >
            <Icon name="panel-right-close" /> Inspect node
          </button>
          {primaryNodeAction(nodeMenu.node) && primaryNodeAction(nodeMenu.node) !== "cancel" && (
            <button
              role="menuitem"
              onClick={() => {
                const node = nodeMenu.node;
                setNodeMenu(null);
                const action = primaryNodeAction(node);
                if (!action || action === "cancel") return;
                if (
                  action === "regenerate" &&
                  !confirm(
                    "Run this planner again and replace its entire descendant tree? This cannot be undone.",
                  )
                )
                  return;
                void api(`/api/nodes/${node.id}/${action}`, { method: "POST" })
                  .then(loadGraph)
                  .catch((error) => notify(String(error)));
              }}
            >
              <Icon name="circle-play" /> {primaryNodeActionLabel(primaryNodeAction(nodeMenu.node) ?? "run")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function Projects({
  projects,
  selected,
  open,
  onSelect,
  onChanged,
  onDeleted,
  notify,
}: {
  projects: Project[];
  selected: string | null;
  open: boolean;
  onSelect: (id: string) => void;
  onChanged: () => Promise<void>;
  onDeleted: (id: string) => void;
  notify: (s: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [menu, setMenu] = useState<{
    node: Project;
    x: number;
    y: number;
  } | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [draftName, setDraftName] = useState("");
  useEffect(() => {
    if (!menu) return;
    const close = () => {
      setMenu(null);
      setRenaming(false);
    };
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    addEventListener("pointerdown", close);
    addEventListener("keydown", key);
    return () => {
      removeEventListener("pointerdown", close);
      removeEventListener("keydown", key);
    };
  }, [menu]);
  const openMenu = (node: Project, x: number, y: number) => {
    setDraftName(node.project_name || node.objective);
    setRenaming(false);
    setMenu({ node, x, y });
  };
  const updateProject = async (action: "rename" | "delete") => {
    if (!menu) return;
    try {
      if (action === "delete") {
        if (
          !confirm(`Delete “${menu.node.project_name || menu.node.objective}”?`)
        )
          return;
        await api(`/api/projects/${menu.node.id}`, { method: "DELETE" });
        onDeleted(menu.node.id);
      } else {
        if (!draftName.trim()) return;
        await api(
          `/api/projects/${menu.node.id}`,
          json("PATCH", { name: draftName.trim() }),
        );
      }
      setMenu(null);
      setRenaming(false);
      await onChanged();
    } catch (error) {
      notify(String(error));
    }
  };
  const visible = projects.filter((node) =>
    (node.project_name || node.objective)
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  return (
    <aside className={`sidebar ${open ? "mobile-open" : ""}`}>
      <div className="panel-heading">Projects</div>
      <label className="sidebar-search">
        <Icon name="search" />
        <input
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter projects"
        />
      </label>
      <div className="project-list">
        {visible.map((node) => (
          <div
            className={`project-item ${selected === node.id ? "selected" : ""}`}
            key={node.id}
            onContextMenu={(event) => {
              event.preventDefault();
              openMenu(node, event.clientX, event.clientY);
            }}
          >
            <button
              className="project-select"
              onClick={() => onSelect(node.id)}
            >
              <span className="project-copy">
                <strong>{node.project_name || node.objective}</strong>
                <small>{node.agent?.harness ?? "agent"}</small>
              </span>
            </button>
            <button
              className="quiet-icon project-menu"
              aria-label={`Options for ${node.project_name || node.objective}`}
              onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                openMenu(node, rect.right, rect.bottom);
              }}
            >
              <Icon name="ellipsis" />
            </button>
          </div>
        ))}
      </div>
      {menu && (
        <div
          className="popover context-menu"
          role="menu"
          style={menuPosition(menu.x, menu.y)}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <div className="popover-label">Project actions</div>
          {renaming ? (
            <form
              className="popover-rename"
              onSubmit={(event) => {
                event.preventDefault();
                void updateProject("rename");
              }}
            >
              <input
                className="popover-input"
                value={draftName}
                onChange={(event) => setDraftName(event.target.value)}
                aria-label="Project name"
                autoFocus
              />
              <button type="submit" disabled={!draftName.trim()}>
                <Icon name="check" /> Save name
              </button>
            </form>
          ) : (
            <button role="menuitem" onClick={() => setRenaming(true)}>
              <Icon name="pencil" /> Rename
            </button>
          )}
          <div className="popover-separator" />
          <button
            role="menuitem"
            className="danger-option"
            onClick={() => void updateProject("delete")}
          >
            <Icon name="trash-2" /> Delete project
          </button>
        </div>
      )}
    </aside>
  );
}

function Author({
  capabilities,
  capabilitiesLoading,
  workspaceSettings,
  onCreated,
  notify,
}: {
  capabilities: Capabilities;
  capabilitiesLoading: boolean;
  workspaceSettings: Record<string, unknown> | null;
  onCreated: (id: string) => void;
  notify: (s: string) => void;
}) {
  const [promptText, setPromptText] = useState("");
  const [name, setName] = useState("");
  const [directory, setDirectory] = useState("");
  const [agent, setAgent] = useState<Agent>(emptyAgent);
  const [policy, setPolicy] = useState(defaultPolicy);
  const [config, setConfig] = useState(false);
  const [attachments, setAttachments] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (capabilitiesLoading || !workspaceSettings) return;
    const first = capabilities.harnesses.find((item) => item.available);
    if (!first) return;
    const configuredHarness = String(workspaceSettings.default_harness ?? "");
    const harness =
      capabilities.harnesses.find(
        (item) => item.available && item.id === configuredHarness,
      ) ?? first;
    const configuredModel = String(workspaceSettings.default_model ?? "");
    const model =
      harness.models.find((item) => item.id === configuredModel) ??
      harness.models[0];
    const configuredReasoning = String(workspaceSettings.reasoning ?? "default");
    const reasoning =
      model?.reasoning?.some((level) => level === configuredReasoning)
        ? configuredReasoning
        : model?.reasoning?.[0] ?? "default";
    setAgent((value) => ({
      ...value,
      harness: harness.id,
      model: model?.id ?? value.model,
      reasoning: reasoning as Reasoning,
    }));
    // New projects must inherit the workspace defaults exposed by Settings.
    // Previously only the agent defaults were applied, so changing the
    // workspace's auto-run, timing, or retry values had no effect on the
    // next workgraph the user created.
    setPolicy((value) => ({
      ...value,
      auto_run: Boolean(workspaceSettings.default_auto_run ?? value.auto_run),
      delay_between_jobs_ms: Number(
        workspaceSettings.delay_between_jobs_ms ?? value.delay_between_jobs_ms,
      ),
      timeout_seconds: Number(
        workspaceSettings.timeout_seconds ?? value.timeout_seconds,
      ),
      stall_timeout_seconds: Number(
        workspaceSettings.stall_timeout_seconds ?? value.stall_timeout_seconds,
      ),
      max_retries: Number(workspaceSettings.max_retries ?? value.max_retries),
      retry_backoff_ms: Number(
        workspaceSettings.retry_backoff_ms ?? value.retry_backoff_ms,
      ),
      retry_choked_models: Boolean(
        workspaceSettings.retry_choked_models ?? value.retry_choked_models,
      ),
    }));
  }, [capabilities, capabilitiesLoading, workspaceSettings]);
  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!promptText.trim() || busy) return;
    setBusy(true);
    try {
      const encoded = await Promise.all(
        attachments.map(async (file) => ({
          name: file.name,
          mime: file.type,
          content_base64: await fileBase64(file),
        })),
      );
      const result = await api<{ project_id: string }>(
        "/api/projects",
        json("POST", {
          prompt: promptText.trim(),
          name: name.trim() || null,
          working_dir: directory || null,
          mode: "create",
          agent,
          run_policy: policy,
          attachments: encoded,
        }),
      );
      onCreated(result.project_id);
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section id="empty-view" className="empty-view">
      <p className="eyebrow">Adaptive development environment</p>
      <h1>What should the workgraph build?</h1>
      <p className="lede">
        Describe an outcome. Turn decomposes it into inspectable, independently
        runnable work.
      </p>
      <form className="composer" onSubmit={create}>
        <textarea
          value={promptText}
          onChange={(event) => setPromptText(event.target.value)}
          placeholder="Build a narrative game with independent world, character, and engine branches…"
          aria-label="Project objective"
        />
        <div className="attachment-list">
          {attachments.map((file, index) => (
            <span className="attachment-chip" key={`${file.name}-${file.size}`}>
              <Icon name="paperclip" />
              <span title={file.name}>
                {file.name} <small>{formatBytes(file.size)}</small>
              </span>
              <button
                type="button"
                onClick={() =>
                  setAttachments((value) =>
                    value.filter((_, itemIndex) => itemIndex !== index),
                  )
                }
                aria-label={`Remove ${file.name}`}
              >
                <Icon name="x" />
              </button>
            </span>
          ))}
        </div>
        <div className="composer-toolbar">
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(event) =>
              setAttachments(Array.from(event.target.files ?? []))
            }
          />
          <button
            className="composer-tool"
            type="button"
            onClick={() => fileRef.current?.click()}
            aria-label="Attach files"
          >
            <Icon name="paperclip" />
          </button>
          <button
            className="composer-tool"
            type="button"
            onClick={async () => {
              try {
                const result = await api<{ path: string | null }>(
                  "/api/system/pick-directory",
                  { method: "POST" },
                );
                if (result.path) setDirectory(result.path);
              } catch (error) {
                notify(String(error));
              }
            }}
            title={directory || "Current directory"}
            aria-label="Choose project directory"
          >
            <Icon name="folder" />
          </button>
          {directory && (
            <span className="directory-selection" title={directory}>
              <Icon name="folder-open" />
              <span>{lastPathPart(directory)}</span>
              <button
                type="button"
                onClick={() => setDirectory("")}
                aria-label="Use current directory"
              >
                <Icon name="x" />
              </button>
            </span>
          )}
          <ModelControl
            harness={agent.harness}
            model={agent.model ?? ""}
            reasoning={agent.reasoning}
            capabilities={capabilities.harnesses}
            loading={capabilitiesLoading}
            onHarness={(harness: HarnessId) => {
              const model = capabilities.harnesses.find(
                (item) => item.id === harness,
              )?.models[0];
              setAgent({
                ...agent,
                harness,
                model: model?.id ?? null,
                reasoning: model?.reasoning?.[0] ?? "default",
              });
            }}
            onModel={(model) => setAgent({ ...agent, model: model || null })}
            onReasoning={(reasoning: Reasoning) =>
              setAgent({ ...agent, reasoning })
            }
          />
          <span className="composer-spacer" />
          <button
            className="composer-tool"
            type="button"
            onClick={() => setConfig((value) => !value)}
            aria-label="Project and run configuration"
          >
            <Icon name="settings-2" />
          </button>
          <button
            className="send-button"
            type="submit"
            disabled={busy || !promptText.trim()}
            aria-label="Create workgraph"
          >
            <Icon name={busy ? "rotate-cw" : "arrow-up"} />
          </button>
        </div>
        {config && (
          <div className="composer-config-panel">
            <div className="form-grid">
              <label className="field">
                <span>Project name</span>
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Derived from objective"
                />
              </label>
              <label className="field">
                <span>Permissions</span>
                <select
                  value={agent.permission}
                  onChange={(event) =>
                    setAgent({
                      ...agent,
                      permission: event.target
                        .value as Agent["permission"],
                    })
                  }
                >
                  <option value="ask">Ask</option>
                  <option value="workspace">Workspace</option>
                  <option value="full">Full access</option>
                </select>
              </label>
            </div>
            <div className="run-options">
              <label className="check">
                <input
                  type="checkbox"
                  checked={policy.auto_run}
                  onChange={(event) =>
                    setPolicy({ ...policy, auto_run: event.target.checked })
                  }
                />
                Auto-run
              </label>
              <label className="inline-field">
                <span>Delay</span>
                <input
                  type="number"
                  min={0}
                  value={policy.delay_between_jobs_ms}
                  onChange={(event) =>
                    setPolicy({
                      ...policy,
                      delay_between_jobs_ms: Number(event.target.value),
                    })
                  }
                />
                <small>ms</small>
              </label>
            </div>
          </div>
        )}
      </form>
      <p className="author-disclaimer">
              Agents act directly in the assigned project directory. Check
              permissions before starting.
      </p>
    </section>
  );
}

function Settings({
  capabilities,
  onClose,
  notify,
}: {
  capabilities: Capabilities;
  onClose: () => void;
  notify: (s: string) => void;
}) {
  const [settings, setSettings] = useState<Record<string, unknown> | null>(
    null,
  );
  const [initial, setInitial] = useState("");
  useEffect(() => {
    void api<Record<string, unknown>>("/api/settings").then((value) => {
      setSettings(value);
      setInitial(JSON.stringify(value));
    });
  }, []);
  if (!settings)
    return (
      <aside className="side-panel">
        <p>Loading settings…</p>
      </aside>
    );
  const dirty = JSON.stringify(settings) !== initial;
  const harness = String(settings.default_harness ?? "codex") as HarnessId;
  return (
    <aside className="side-panel">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!dirty) return;
          try {
            await api("/api/settings", json("POST", settings));
            setInitial(JSON.stringify(settings));
            applyAppearance(settings);
            notify("Settings saved");
          } catch (error) {
            notify(String(error));
          }
        }}
      >
        <div className="panel-head">
          <div>
            <p className="eyebrow">Preferences</p>
            <h2>Workspace settings</h2>
          </div>
          <button
            type="button"
            className="quiet-icon"
            onClick={onClose}
            aria-label="Close settings"
          >
            <Icon name="x" />
          </button>
        </div>
        <section className="settings-section">
          <h3>Appearance</h3>
          <label className="field">
            <span>Theme</span>
            <select
              value={String(settings.theme ?? "system")}
              onChange={(event) =>
                setSettings({ ...settings, theme: event.target.value })
              }
            >
              <option value="dark">Dark</option>
              <option value="light">Light</option>
              <option value="system">System</option>
            </select>
          </label>
        </section>
        <section className="settings-section">
          <h3>Agent defaults</h3>
          <div className="form-grid">
            <ModelControl
              harness={harness}
              model={String(settings.default_model ?? "")}
              reasoning={String(settings.reasoning ?? "default") as Reasoning}
              capabilities={capabilities.harnesses}
              onHarness={(value) => {
                const model = capabilities.harnesses.find(
                  (item) => item.id === value,
                )?.models[0];
                setSettings({
                  ...settings,
                  default_harness: value,
                  default_model: model?.id ?? "",
                  reasoning: model?.reasoning?.[0] ?? "default",
                });
              }}
              onModel={(value) =>
                setSettings({ ...settings, default_model: value })
              }
              onReasoning={(value) =>
                setSettings({ ...settings, reasoning: value })
              }
            />
          </div>
        </section>
        <div className="panel-actions">
          <button className="button accent" disabled={!dirty}>
            Save settings
          </button>
        </div>
      </form>
    </aside>
  );
}

function Policy({
  projectId,
  policy,
  onClose,
  onSaved,
  notify,
}: {
  projectId: string;
  policy: RunPolicy;
  onClose: () => void;
  onSaved: () => Promise<void>;
  notify: (s: string) => void;
}) {
  const [value, setValue] = useState(policy);
  const dirty = JSON.stringify(value) !== JSON.stringify(policy);
  return (
    <aside className="side-panel">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!dirty) return;
          try {
            await api(
              `/api/projects/${projectId}/policy`,
              json("POST", { run_policy: value }),
            );
            await onSaved();
            onClose();
          } catch (error) {
            notify(String(error));
          }
        }}
      >
        <div className="panel-head">
          <div>
            <p className="eyebrow">Current project</p>
            <h2>Execution policy</h2>
          </div>
          <button
            type="button"
            className="quiet-icon"
            onClick={onClose}
            aria-label="Close execution policy"
          >
            <Icon name="x" />
          </button>
        </div>
        <section className="settings-section">
          <h3>Timing and recovery</h3>
          <div className="form-grid">
            {(
              [
                "delay_between_jobs_ms",
                "timeout_seconds",
                "stall_timeout_seconds",
                "max_retries",
                "retry_backoff_ms",
              ] as const
            ).map((key) => (
              <label className="field" key={key}>
                <span>{key.replaceAll("_", " ")}</span>
                <input
                  type="number"
                  min={0}
                  value={value[key]}
                  onChange={(event) =>
                    setValue({ ...value, [key]: Number(event.target.value) })
                  }
                />
              </label>
            ))}
          </div>
        </section>
        <div className="panel-actions">
          <button className="button accent" disabled={!dirty}>
            Apply policy
          </button>
        </div>
      </form>
    </aside>
  );
}
async function setMode(
  projectId: string,
  autoRun: boolean,
  refresh: () => Promise<void>,
) {
  await api(
    `/api/projects/${projectId}/mode`,
    json("POST", { auto_run: autoRun }),
  );
  await refresh();
}
async function fileBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  bytes.forEach((value) => {
    binary += String.fromCharCode(value);
  });
  return btoa(binary);
}
function applyAppearance(settings: Record<string, unknown>): void {
  const theme = String(settings.theme ?? "system");
  const effective =
    theme === "system"
      ? matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "dark"
      : theme;
  document.documentElement.dataset.theme = effective;
  document.documentElement.dataset.density = String(
    settings.density ?? "comfortable",
  );
}

function lastPathPart(path: string): string {
  return (
    path
      .replace(/[\\/]+$/, "")
      .split(/[\\/]/)
      .pop() || path
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function menuPosition(x: number, y: number): React.CSSProperties {
  return {
    left: Math.max(8, Math.min(x, window.innerWidth - 216)),
    top: Math.max(8, Math.min(y, window.innerHeight - 180)),
  };
}
