import { request, json } from "./api.js";
import { acceptsProjectResult, deriveWorkgraphStatus, initialAppState, reduceAppState, resolveShortcut } from "./app-machine.js";
import { dendrogramPath, layoutDendrogram } from "./dendrogram.js";
import { Terminal } from "/vendor/xterm/lib/xterm.mjs";
import { FitAddon } from "/vendor/xterm-fit/lib/addon-fit.mjs";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const ICON_BASE = "/icons";
const h = (tag, attrs = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child !== null && child !== undefined) node.append(child);
  }
  return node;
};

function icon(name, className = "") {
  return h("span", {
    class: `icon ${className}`.trim(),
    "data-icon": name,
    "aria-hidden": "true",
    style: `--icon-url:url("${ICON_BASE}/${name}.svg")`,
  });
}

function hydrateIcons(root = document) {
  $$(`[data-icon]`, root).forEach((element) => {
    element.style.setProperty("--icon-url", `url("${ICON_BASE}/${element.dataset.icon}.svg")`);
  });
  $$(`[data-tooltip]`, root).forEach((element) => {
    if (!element.getAttribute("aria-label") && element.matches(".help-hint")) {
      element.setAttribute("aria-label", element.dataset.tooltip);
    }
  });
}

const data = {
  app: { ...initialAppState },
  projects: [],
  graph: { nodes: [], edges: [], artifacts: [] },
  usage: { totals: {}, by_node: {}, by_branch: {} },
  capabilities: { harnesses: [] },
  settings: {},
  stream: null,
  reloadTimer: null,
  zoom: 1,
  tab: "overview",
  terminal: new Map(),
  detailEditing: false,
  detailDirty: false,
  attachments: [],
  graphSignature: "",
  detailSignature: "",
  refreshSelectedDetail: false,
  formBaselines: new Map(),
};

function dispatch(event) {
  data.app = reduceAppState(data.app, event);
  renderChrome();
}

function toast(message, kind = "info") {
  const item = h("div", { class: `toast ${kind}` }, [icon(kind === "error" ? "alert-triangle" : "check"), h("span", { text: message })]);
  const region = $("#toast-region");
  (document.querySelector(".side-panel:not([hidden])") || document.body).append(region);
  region.append(item);
  setTimeout(() => item.remove(), 3800);
}

function confirmAction(title, message, confirmLabel = "Continue") {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  $("#confirm-submit").textContent = confirmLabel;
  hydrateIcons(dialog);
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

let tooltipTimer = null;
let tooltipTarget = null;
function showTooltip(target) {
  clearTimeout(tooltipTimer);
  tooltipTimer = setTimeout(() => {
    const tip = $("#tooltip"), label = target.dataset.tooltip;
    if (!tip || !label) return;
    tooltipTarget?.removeAttribute("aria-describedby");
    tooltipTarget = target;
    target.setAttribute("aria-describedby", "tooltip");
    $("#tooltip-label").textContent = label;
    const shortcut = $("#tooltip-shortcut");
    shortcut.textContent = target.dataset.shortcut || "";
    shortcut.hidden = !target.dataset.shortcut;
    (target.closest(".side-panel:not([hidden])") || document.body).append(tip);
    tip.hidden = false;
    tip.classList.remove("visible");
    const rect = target.getBoundingClientRect();
    const width = tip.offsetWidth, height = tip.offsetHeight;
    let left = Math.min(innerWidth - width - 8, Math.max(8, rect.left + rect.width / 2 - width / 2));
    let top = rect.bottom + 7;
    if (top + height > innerHeight - 8) top = rect.top - height - 7;
    tip.style.left = `${left}px`; tip.style.top = `${Math.max(8, top)}px`;
    requestAnimationFrame(() => tip.classList.add("visible"));
  }, 360);
}

function hideTooltip() {
  clearTimeout(tooltipTimer);
  const tip = $("#tooltip");
  if (!tip) return;
  tooltipTarget?.removeAttribute("aria-describedby");
  tooltipTarget = null;
  tip.classList.remove("visible");
  tooltipTimer = setTimeout(() => { tip.hidden = true; }, 130);
}

async function safe(work, success) {
  if (data.app.pendingCommand) {
    toast("Finish the current action before starting another one");
    return { ok: false, busy: true };
  }
  const command = success || "working";
  try {
    dispatch({ type: "COMMAND_START", command });
    const result = await work();
    if (success) toast(success);
    return { ok: true, value: result };
  } catch (error) {
    toast(error.message || String(error), "error");
    return { ok: false, error };
  } finally {
    dispatch({ type: "COMMAND_DONE", command });
  }
}

function renderChrome() {
  const connection = data.app.connection;
  const hasProject = Boolean(data.app.projectId);
  $("#status-connection").textContent = hasProject
    ? (({ live: "Connected", connecting: "Connecting…", reconnecting: "Reconnecting…", offline: "Offline" })[connection] || connection)
    : "Local workspace";
  $("#empty-view").hidden = hasProject;
  $("#graph-view").hidden = !hasProject;
  $("#inspector").hidden = !data.app.selectedNodeId;
  document.body.toggleAttribute("aria-busy", Boolean(data.app.pendingCommand));
  document.body.dataset.busy = data.app.pendingCommand ? "true" : "false";
  for (const control of $$("#command-btn,#settings-btn,#auto-mode-btn,#step-mode-btn,#run-step-btn,#project-options-btn,#popover button,.side-panel button,.side-panel input,.side-panel select")) {
    control.disabled = Boolean(data.app.pendingCommand);
  }
  $("#status-message").textContent = data.app.pendingCommand
    ? `${String(data.app.pendingCommand).replaceAll("_", " ")}…`
    : hasProject ? workgraphStatusMessage() : "Describe a project to begin";
  $("#graph-legend").hidden = !hasProject;
  $("#graph-zoom").hidden = !hasProject;
  if (!hasProject) {
    $("#graph-stats").textContent = "";
    $("#status-tokens").textContent = "Usage unavailable";
  }
}

function currentProject() { return data.projects.find((project) => project.id === data.app.projectId); }
function currentNode() { return data.graph.nodes.find((node) => node.id === data.app.selectedNodeId); }
function workgraphStatusMessage() {
  return deriveWorkgraphStatus(data.graph.nodes, data.app.projectId);
}
function formatCount(value = 0) { return Intl.NumberFormat("en", { notation: value > 9999 ? "compact" : "standard", maximumFractionDigits: 1 }).format(value); }
function usageTokens(usage = {}) { return (usage.input_tokens || 0) + (usage.cached_input_tokens || 0) + (usage.output_tokens || 0); }
function formatCost(value) { return value ? `$${Number(value).toFixed(value < .1 ? 3 : 2)}` : "—"; }
function harnessLabel(id) { return data.capabilities.harnesses.find((item) => item.id === id)?.label || id || "Agent"; }
function harnessIconName(id) { return ({ codex: "square-terminal", claude: "sparkles", opencode: "braces", pi: "orbit" })[id] || "terminal"; }
function projectDisplayName(project) {
  const storedName = String(project?.project_name || "").replace(/\s+/g, " ").trim();
  if (storedName) return storedName;
  const raw = String(project?.objective || "").replace(/\s+/g, " ").trim();
  if (!raw) return "Untitled project";
  // Legacy projects predate project_name. Apply a deterministic presentation
  // fallback regardless of later prompt revisions; never infer identity from
  // mutable execution instructions.
  const called = raw.match(/\b(?:called|named|titled)\s+[“\"']?([^.!?,”\"']+(?:\s+[^.!?,”\"']+){0,4})/i);
  if (called) return called[1].trim();
  const clause = raw.split(/[.!?;:]|\s[-–—]\s/, 1)[0]
    .replace(/^(?:please\s+)?(?:build|create|make|develop|design|implement|write|refactor|plan|generate)\s+(?:(?:a|an|the)\s+)?(?:(?:tiny|small|simple|scoped)\s+)?/i, "");
  if (clause.length <= 48) return clause;
  const words = clause.split(/\s+/).filter(Boolean);
  return `${words.slice(0, 6).join(" ")}${words.length > 6 ? "…" : ""}`;
}
function agentModelLabel(agent = {}) {
  const model = agent.model || "default";
  return model.includes("/") ? model.split("/").at(-1) : model;
}
function harnessCapability(id) { return data.capabilities.harnesses.find((item) => item.id === id); }
function reasoningLevels(harnessId, model = "") {
  const capability = harnessCapability(harnessId);
  const base = capability?.reasoning || ["default"];
  const normalized = model.trim().toLowerCase();
  for (const profile of capability?.reasoning_profiles || []) {
    const matches = (profile.match_any || []).some((token) => {
      const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return new RegExp(`(^|[^a-z0-9])${escaped}($|[^a-z0-9])`).test(normalized);
    });
    if (matches) {
      return profile.reasoning.filter((level) => base.includes(level));
    }
  }
  return base;
}
function syncAgentCapabilityControls(prefix, preferredReasoning = null) {
  const harness = $(`#${prefix}-harness`).value;
  if (prefix === "new") {
    const glyph = $(".harness-select .icon");
    if (glyph) glyph.style.setProperty("--icon-url", `url("${ICON_BASE}/${harnessIconName(harness)}.svg")`);
  }
  const model = $(`#${prefix}-model`).value.trim();
  const select = $(`#${prefix}-reasoning`);
  const supported = reasoningLevels(harness, model);
  const preferred = preferredReasoning ?? select.value ?? "default";
  select.replaceChildren(...supported.map((level) => h("option", { value: level, text: level === "default" ? "Default" : level[0].toUpperCase() + level.slice(1) })));
  select.value = supported.includes(preferred) ? preferred : "default";
  const note = $(`#${prefix}-reasoning-note`);
  if (note) note.textContent = `${supported.length} level${supported.length === 1 ? "" : "s"} supported by ${model || "the harness default"}`;
  const datalist = $(`#${prefix}-model-options`);
  if (datalist) {
    const models = harnessCapability(harness)?.models || [];
    datalist.replaceChildren(...models.map((item) => h("option", { value: item.id, label: item.label })));
  }
  const modelControl = $(`#${prefix}-model`);
  if (modelControl?.tagName === "SELECT") {
    const current = modelControl.value || model;
    const models = harnessCapability(harness)?.models || [];
    modelControl.replaceChildren(
      h("option", { value: "", text: "Harness default" }),
      ...models.map((item) => h("option", { value: item.id, text: item.label || item.id })),
    );
    if (current && !models.some((item) => item.id === current)) modelControl.append(h("option", { value: current, text: current }));
    modelControl.value = current;
  }
  return select.value;
}
function relativeTime(value) {
  const normalized = value && !/[zZ]|[+-]\d\d:\d\d$/.test(value) ? `${value}Z` : value;
  const seconds = Math.max(0, (Date.now() - new Date(normalized).getTime()) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

async function init() {
  try {
    [data.settings, data.capabilities] = await Promise.all([request("/api/settings"), request("/api/capabilities")]);
    applyAppearance();
    populateHarnesses();
    $("#new-name").dataset.automatic = "true";
    $("#new-dir").placeholder = data.settings.projects_dir || "Current directory";
    await loadProjects();
    dispatch({ type: "BOOTED", hasProjects: false });
    // Authoring is intentionally the first-load focus. Saved projects remain
    // one click away behind the collapsed project panel.
    renderProjects();
  } catch (error) {
    dispatch({ type: "BOOTED", hasProjects: false });
    toast(`Could not start Turn: ${error.message}`, "error");
  }
}

async function loadProjects() {
  const response = await request("/api/projects");
  data.projects = response.projects || [];
  renderProjects();
}

function renderProjects() {
  const list = $("#project-list");
  const filter = $("#project-filter").value.trim().toLowerCase();
  list.replaceChildren();
  const projects = data.projects.filter((p) => `${projectDisplayName(p)} ${p.objective || ""} ${p.generated_prompt || ""}`.toLowerCase().includes(filter));
  if (!projects.length) {
    list.append(h("div", { class: "empty-list", text: filter ? "No matching projects" : "No projects yet" }));
    return;
  }
  for (const project of projects) {
    const agent = project.agent?.harness || project.executor;
    const row = h("div", {
      class: `project-item ${project.id === data.app.projectId ? "selected" : ""}`,
      role: "listitem",
      "aria-label": projectDisplayName(project),
    }, [
      h("button", {
        class: "project-select",
        type: "button",
        "aria-current": project.id === data.app.projectId ? "page" : null,
        title: project.generated_prompt || project.objective || "",
        onclick: () => selectProject(project.id),
      }, [h("span", { class: "project-copy" }, [
          h("strong", { text: projectDisplayName(project) }),
          h("small", {}, [h("span", { text: harnessLabel(agent) })]),
        ])]),
      h("button", {
        class: "project-item-actions",
        type: "button",
        "aria-label": `Rename or remove ${projectDisplayName(project)}`,
        "data-tooltip": "Rename or remove project",
        onclick: (event) => { event.stopPropagation(); showProjectMenu(project, event.currentTarget); },
      }, icon("ellipsis")),
    ]);
    list.append(row);
  }
}

async function selectProject(projectId) {
  if (!projectId || (projectId === data.app.projectId && data.graph.nodes.length)) return;
  data.graph = { nodes: [], edges: [], artifacts: [] };
  data.usage = { totals: {}, by_node: {}, by_branch: {} };
  dispatch({ type: "SELECT_PROJECT", projectId });
  localStorage.setItem("turn.project", projectId);
  $("#project-sidebar").classList.remove("mobile-open");
  const project = data.projects.find((item) => item.id === projectId);
  $("#project-title").textContent = projectDisplayName(project);
  $("#repo-line").textContent = "Loading workgraph…";
  $("#graph-stats").textContent = "Loading";
  $("#graph").replaceChildren(h("div", { class: "graph-empty", text: "Loading workgraph…" }));
  connectStream();
  try {
    await loadGraph({ projectId });
  } catch (error) {
    if (acceptsProjectResult(data.app, projectId)) {
      dispatch({ type: "GRAPH_FAILED" });
      $("#repo-line").textContent = "Workgraph unavailable";
      $("#graph-stats").textContent = "Load failed";
      $("#graph").replaceChildren(h("div", { class: "graph-empty" }, [
        h("span", { text: "The workgraph could not be loaded." }),
        h("button", { class: "button", text: "Retry loading", onclick: () => selectProject(projectId) }),
      ]));
      toast(error.message || "Could not load workgraph", "error");
    }
  }
  renderProjects();
}

async function loadGraph({ quiet = false, projectId = data.app.projectId } = {}) {
  if (!projectId) return;
  const [graph, usage] = await Promise.all([
    request(`/api/projects/${projectId}/graph`),
    request(`/api/projects/${projectId}/usage`).catch(() => data.usage),
  ]);
  // Project selection may change while these requests are in flight. Never
  // let a slower, stale response overwrite the currently selected workgraph.
  if (!acceptsProjectResult(data.app, projectId)) return;
  data.graph = graph;
  data.usage = usage;
  dispatch({ type: "GRAPH_LOADED" });
  renderWorkspace();
  if (data.refreshSelectedDetail && data.app.selectedNodeId && !data.detailEditing && !data.detailDirty) {
    data.refreshSelectedDetail = false;
    await renderDetail({ loading: false });
  }
  if (!quiet) renderProjects();
}

function scheduleReload(delay = 120, refreshDetail = false) {
  data.refreshSelectedDetail ||= refreshDetail;
  clearTimeout(data.reloadTimer);
  data.reloadTimer = setTimeout(() => loadGraph({ quiet: true }).catch((error) => toast(error.message, "error")), delay);
}

function connectStream() {
  if (data.stream) data.stream.close();
  if (!data.app.projectId) return;
  dispatch({ type: "STREAM_CONNECTING" });
  const source = new EventSource(`/api/projects/${data.app.projectId}/stream`);
  const projectId = data.app.projectId;
  data.stream = source;
  const current = () => data.stream === source && acceptsProjectResult(data.app, projectId);
  source.onopen = () => { if (current()) dispatch({ type: "STREAM_OPEN" }); };
  source.onerror = () => { if (current()) dispatch({ type: "STREAM_ERROR" }); };
  source.onmessage = (message) => {
    if (!current()) return;
    let event;
    try { event = JSON.parse(message.data); } catch { return; }
    if (event.type === "connected") { dispatch({ type: "STREAM_OPEN" }); return; }
    if (event.type === "project.manual") return;
    if (event.type === "node.terminal") return; // xterm receives raw PTY bytes over its websocket.
    const changedId = event.data?.node_id || event.data?.id;
    scheduleReload(120, Boolean(changedId && changedId === data.app.selectedNodeId));
  };
}

function renderWorkspace() {
  const root = data.graph.nodes.find((node) => node.id === data.app.projectId) || data.graph.nodes.find((node) => !node.parent_id);
  if (!root) return;
  $("#project-title").textContent = projectDisplayName(root);
  const repo = root.repo_path || "Headless project";
  const parts = repo.split(/[\\/]/).filter(Boolean);
  $("#repo-line").textContent = parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : repo;
  $("#repo-line").title = repo;
  const auto = root.auto_run !== false;
  $("#auto-mode-btn").classList.toggle("selected", auto);
  $("#step-mode-btn").classList.toggle("selected", !auto);
  $("#run-step-btn").hidden = auto;
  const resolved = data.graph.nodes.filter((node) => ["complete", "accepted", "failed", "cancelled"].includes(node.ui_state)).length;
  const active = data.graph.nodes.filter((node) => ["running", "verifying"].includes(node.ui_state)).length;
  const manualReviews = data.graph.nodes.filter((node) => node.ui_state === "review" && node.needs_review && node.review_owner === "manual").length;
  const parentReviews = data.graph.nodes.filter((node) => node.ui_state === "review" && node.needs_review && node.review_owner === "parent").length;
  $("#graph-stats").textContent = `${resolved}/${data.graph.nodes.length} resolved${active ? ` · ${active} active` : ""}${manualReviews ? ` · ${manualReviews} review` : ""}${parentReviews ? ` · ${parentReviews} parent review` : ""}`;
  $("#status-message").textContent = workgraphStatusMessage();
  const tokens = usageTokens(data.usage.totals);
  $("#status-tokens").textContent = tokens ? `${formatCount(tokens)} tokens` : "Usage unavailable";
  const signature = JSON.stringify({
    nodes: data.graph.nodes.map((n) => [n.id,n.parent_id,n.objective,n.ui_state,n.status,n.progress,n.needs_review,n.review_owner,n.merge_accepted,n.verification_status,n.agent,usageTokens(data.usage.by_node?.[n.id])]),
    edges: data.graph.edges,
    selected: data.app.selectedNodeId,
    zoom: data.zoom,
  });
  if (signature !== data.graphSignature) {
    data.graphSignature = signature;
    renderGraph();
  }
}

const NODE_W = 224, NODE_H = 58;
function graphLayout() {
  const nodes = data.graph.nodes.filter((node) => !node.superseded_by || node.status !== "CANCELLED");
  return layoutDendrogram(nodes, { nodeWidth: NODE_W, nodeHeight: NODE_H });
}

function renderGraph() {
  const host = $("#graph");
  host.replaceChildren();
  const layout = graphLayout();
  if (!layout.nodes.length) { host.append(h("div", { class: "graph-empty", text: "Planning the first nodes…" })); return; }
  const maxX = layout.width, maxY = layout.height;
  const pad = 38;
  const canvas = h("div", { class: "graph-canvas" });
  canvas.style.width = `${maxX + pad * 2}px`; canvas.style.height = `${maxY + pad * 2}px`; canvas.style.transform = `scale(${data.zoom})`;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "graph-edges"); svg.setAttribute("width", maxX + pad * 2); svg.setAttribute("height", maxY + pad * 2);
  const drawEdge = (src, dst, className) => {
    const a = layout.positions.get(src), b = layout.positions.get(dst); if (!a || !b) return;
    const path = document.createElementNS(svg.namespaceURI, "path");
    const translatedA = { x: a.x + pad, y: a.y + pad };
    const translatedB = { x: b.x + pad, y: b.y + pad };
    path.setAttribute("d", dendrogramPath(translatedA, translatedB, { nodeWidth: NODE_W, nodeHeight: NODE_H }));
    const activeStates = new Set(["running", "verifying"]);
    const active = activeStates.has(layout.byId.get(src)?.ui_state) || activeStates.has(layout.byId.get(dst)?.ui_state);
    path.setAttribute("class", `${className}${active ? " edge-active" : ""}`); svg.append(path);
  };
  for (const node of layout.nodes) if (node.parent_id) drawEdge(node.parent_id, node.id, "edge-contains");
  for (const edge of data.graph.edges || []) if (edge.type === "DEPENDS_ON") drawEdge(edge.src, edge.dst, "edge-depends");
  canvas.append(svg);
  for (const node of layout.nodes) {
    const p = layout.positions.get(node.id); if (!p) continue;
    const iconName = node.executor === "planner" ? "workflow"
      : node.ui_state === "review" ? "check"
      : node.required_inputs?.some((i) => !i.satisfied_by) ? "circle-help"
      : node.ui_state === "failed" ? "alert-triangle"
      : node.ui_state === "paused" ? "pause"
      : ["running", "verifying"].includes(node.ui_state) ? "activity"
      : "bot";
    const card = h("div", {
      class: `gnode ${node.ui_state} ${node.id === data.app.selectedNodeId ? "selected" : ""}`,
      "data-node-id": node.id,
    }, [
      h("button", {
        class: "node-select",
        type: "button",
        "aria-label": `${node.objective}, ${node.ui_state}`,
        "data-tooltip": "Open node inspector",
        onclick: () => selectNode(node.id),
        oncontextmenu: (event) => { event.preventDefault(); showNodeMenu(node, event.clientX, event.clientY); },
        onkeydown: (event) => {
          if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
            event.preventDefault();
            const rect = event.currentTarget.getBoundingClientRect();
            showNodeMenu(node, rect.left + rect.width - 12, rect.top + 24);
          }
        },
      }, [
        h("span", { class: "node-glyph" }, icon(iconName)),
        h("span", { class: "node-main" }, [
          h("span", { class: "node-title", text: node.objective, title: node.objective }),
          h("span", { class: "node-meta" }, [
            h("span", { class: "node-state", text: node.ui_state.replaceAll("_", " ") }),
            h("span", { text: harnessLabel(node.agent?.harness || node.executor || "unassigned") }),
          ]),
          h("span", { class: "node-config" }, [
            h("span", { text: agentModelLabel(node.agent) }),
            node.agent?.reasoning && node.agent.reasoning !== "default" ? h("span", { text: node.agent.reasoning }) : null,
            node.agent?.skills?.length ? h("span", { class: "node-resource", "data-tooltip": `${node.agent.skills.length} skills` }, [icon("sparkles"), h("i", { text: node.agent.skills.length })]) : null,
            node.agent?.tools?.length ? h("span", { class: "node-resource", "data-tooltip": `${node.agent.tools.length} tools` }, [icon("bot"), h("i", { text: node.agent.tools.length })]) : null,
            node.agent?.mcp_servers?.length ? h("span", { class: "node-resource", "data-tooltip": `${node.agent.mcp_servers.length} MCP servers` }, [icon("workflow"), h("i", { text: node.agent.mcp_servers.length })]) : null,
            usageTokens(data.usage.by_node?.[node.id]) ? h("span", { class: "node-tokens", text: `${formatCount(usageTokens(data.usage.by_node?.[node.id]))} tok` }) : null,
          ]),
        ]),
      ]),
      h("button", {
        class: "node-menu-trigger",
        type: "button",
        "data-node-menu": node.id,
        "data-tooltip": "Node actions",
        "aria-label": `Actions for ${node.objective}`,
        "aria-haspopup": "menu",
        "aria-controls": "popover",
        onclick: (event) => {
          event.stopPropagation();
          const rect = event.currentTarget.getBoundingClientRect();
          showNodeMenu(node, rect.right, rect.bottom + 3);
        },
      }, icon("ellipsis")),
    ]);
    card.style.left = `${p.x + pad}px`; card.style.top = `${p.y + pad}px`;
    if (node.progress !== null && node.progress !== undefined && layout.children.has(node.id)) card.append(h("span", { class: "node-progress" }, h("span", { style: `width:${Math.round(node.progress * 100)}%` })));
    canvas.append(card);
  }
  hydrateIcons(canvas);
  host.append(canvas);
  $("#zoom-label").textContent = `${Math.round(data.zoom * 100)}%`;
}

function menuItem(label, iconName, action, { danger = false } = {}) {
  return h("button", {
    role: "menuitem",
    class: danger ? "danger-option" : "",
    onclick: async () => {
      $("#popover").hidden = true;
      await action();
    },
  }, [icon(iconName), h("span", { text: label })]);
}

function placePopover(x, y) {
  const popover = $("#popover");
  popover.hidden = false;
  const rect = popover.getBoundingClientRect();
  popover.style.left = `${Math.max(8, Math.min(x, innerWidth - rect.width - 8))}px`;
  popover.style.top = `${Math.max(8, Math.min(y, innerHeight - rect.height - 8))}px`;
}

function showNodeMenu(node, x, y) {
  hideTooltip();
  const popover = $("#popover");
  const actions = new Set(node.allowed_actions || []);
  popover.replaceChildren(
    h("div", { class: "popover-label", text: "Node" }),
    menuItem("Open inspector", "panel-right-close", () => selectNode(node.id)),
    menuItem("View terminal", "terminal", () => selectNode(node.id, "terminal")),
    menuItem("View run history", "activity", () => selectNode(node.id, "history")),
  );
  const direct = [
    ["run", "Run now", "play"], ["pause", "Pause", "pause"],
    ["resume", "Resume", "circle-play"], ["retry", "Retry", "rotate-cw"],
    ["fork", "Create fork", "git-fork"],
    ["cancel", "Cancel", "square-stop"],
  ].filter(([action]) => actions.has(action));
  if (direct.length) {
    popover.append(h("div", { class: "popover-separator" }), h("div", { class: "popover-label", text: "Execution" }));
    for (const [action, label, iconName] of direct) {
      popover.append(menuItem(label, iconName, async () => {
        if (action === "fork") { await selectNode(node.id); showForkEditor($("#detail"), node); }
        else await nodeAction(node, action);
      }, { danger: action === "cancel" }));
    }
  }
  if (node.parent_id || node.status === "EXPANDED") {
    popover.append(
      h("div", { class: "popover-separator" }),
      h("div", { class: "popover-label", text: "Branch and descendants" }),
      menuItem("Pause branch", "pause", () => branchAction(node.id, "pause")),
      menuItem("Resume branch", "circle-play", () => branchAction(node.id, "resume")),
      menuItem("Cancel branch", "square-stop", () => branchAction(node.id, "cancel"), { danger: true }),
    );
  }
  placePopover(x, y);
}

async function selectNode(nodeId, tab = "overview") {
  $("#project-sidebar").classList.remove("mobile-open");
  dispatch({ type: "SELECT_NODE", nodeId, tab });
  data.app.tab = tab;
  data.detailSignature = "";
  data.detailDirty = false;
  renderGraph();
  await renderDetail();
}

function button(label, action, className = "button", iconName = null, tooltip = null) {
  return h("button", { type: "button", class: className, onclick: action, "data-tooltip": tooltip }, [iconName ? icon(iconName) : null, h("span", { text: label })]);
}
function section(title, content) { return h("section", { class: "section" }, [h("div", { class: "section-heading", text: title }), content]); }
function stateBadge(node) { return h("span", { class: `badge ${node.ui_state}`, text: node.ui_state.replaceAll("_", " ") }); }

function formValueSignature(form) {
  return JSON.stringify([...form.querySelectorAll("input,select,textarea")].map((control) => [
    control.name || control.id,
    control.type === "checkbox" || control.type === "radio" ? control.checked : control.value,
  ]));
}

function trackPristine(form, submit) {
  form.dataset.baseline = formValueSignature(form);
  const sync = () => {
    const dirty = formValueSignature(form) !== form.dataset.baseline;
    submit.disabled = !dirty;
    form.dataset.dirty = String(dirty);
    if (form.closest("#detail")) data.detailDirty = Boolean(document.querySelector("#detail form[data-dirty='true']"));
  };
  if (!form.dataset.pristineBound) {
    form.addEventListener("input", sync);
    form.addEventListener("change", sync);
    form.dataset.pristineBound = "true";
  }
  sync();
}

async function renderDetail({ loading = true, force = false } = {}) {
  const nodeId = data.app.selectedNodeId; if (!nodeId) return;
  const detail = $("#detail");
  if (loading && !detail.childElementCount) detail.replaceChildren(h("div", { class: "detail-loading", text: "Loading node…" }));
  let response;
  try { response = await request(`/api/nodes/${nodeId}`); }
  catch (error) {
    if (nodeId !== data.app.selectedNodeId) return;
    detail.replaceChildren(h("div", { class: "detail-error" }, [h("p", { text: error.message || "Could not load this node." }), button("Retry", () => renderDetail({ force: true }), "button", "rotate-cw")]));
    return;
  }
  if (nodeId !== data.app.selectedNodeId) return;
  const node = response.node;
  $$(".inspector-tabs button").forEach((tab) => {
    const active = tab.dataset.tab === data.app.tab;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  const signature = JSON.stringify([data.app.tab, node, response.runs, response.artifacts, data.usage.by_node?.[node.id], data.usage.by_branch?.[node.id]]);
  if (!force && signature === data.detailSignature) return;
  data.detailSignature = signature;
  if (data.app.tab === "terminal") renderTerminal(detail, node, response);
  else if (data.app.tab === "history") renderHistory(detail, node, response);
  else renderOverview(detail, node, response);
}

function renderOverview(detail, node, response) {
  data.detailDirty = false;
  detail.replaceChildren();
  detail.append(h("h2", { class: "detail-title", text: node.objective }));
  detail.append(h("div", { class: "detail-meta" }, [stateBadge(node), h("span", { class: "badge", text: `Revision ${node.revision}` })]));
  if (node.state_reason) detail.append(h("p", { class: "mono", text: node.state_reason }));
  const config = node.agent;
  if (config) {
    const harness = h("select", { name: "harness", "aria-label": "Agent harness" });
    for (const item of data.capabilities.harnesses) harness.append(h("option", { value: item.id, text: item.label, disabled: item.available ? null : "disabled" }));
    if (![...harness.options].some((option) => option.value === config.harness)) {
      harness.append(h("option", { value: config.harness, text: `${config.harness} · internal` }));
    }
    harness.value = config.harness;
    const model = h("input", { name: "model", "aria-label": "Agent model", value: config.model || "", placeholder: "Harness default" });
    const reasoning = h("select", { name: "reasoning", "aria-label": "Agent reasoning" });
    const permissions = h("select", { name: "permission", "aria-label": "Agent permissions" }, [h("option", { value: "ask", text: "Ask" }), h("option", { value: "workspace", text: "Workspace" }), h("option", { value: "full", text: "Full access" })]);
    permissions.value = config.permission;
    const cascade = h("input", { name: "cascade", type: "checkbox" });
    const saveAgent = button("Save agent", null, "button accent", "check"); saveAgent.type = "submit";
    const syncReasoning = () => { const levels = reasoningLevels(harness.value, model.value); const preferred = reasoning.value || config.reasoning; reasoning.replaceChildren(...levels.map((level) => h("option", { value: level, text: level === "default" ? "Default" : level }))); reasoning.value = levels.includes(preferred) ? preferred : "default"; };
    harness.onchange = syncReasoning; model.oninput = syncReasoning; syncReasoning();
    const configBody = h("form", { class: "agent-config inline-form" }, [
      h("div", { class: "form-grid" }, [
        h("label", { class: "field" }, [h("span", { text: "Harness" }), harness]),
        h("label", { class: "field" }, [h("span", { text: "Model" }), model]),
        h("label", { class: "field" }, [h("span", { text: "Reasoning" }), reasoning]),
        h("label", { class: "field" }, [h("span", { text: "Permissions" }), permissions]),
      ]),
      h("div", { class: "agent-resources" }, [
        h("span", { "data-tooltip": "Configured agent skills" }, [icon("sparkles"), h("b", { text: `${config.skills?.length || 0} skills` })]),
        h("span", { "data-tooltip": "Configured agent tools" }, [icon("bot"), h("b", { text: `${config.tools?.length || 0} tools` })]),
        h("span", { "data-tooltip": "Configured MCP servers" }, [icon("workflow"), h("b", { text: `${config.mcp_servers?.length || 0} MCP` })]),
      ]),
      h("label", { class: "check cascade-check" }, [cascade, h("span", { text: "Apply to active descendants" })]),
      saveAgent,
    ]);
    configBody.onsubmit = async (event) => {
      event.preventDefault();
      const updated = { ...config, harness: harness.value, model: model.value.trim() || null, reasoning: reasoning.value, permission: permissions.value, session_id: harness.value === config.harness ? config.session_id : null };
      const result = await safe(() => request(`/api/nodes/${node.id}/edit`, json("POST", { agent: updated, cascade_agent: cascade.checked })), "Agent configuration updated");
      if (result.ok) { data.detailDirty = false; data.detailSignature = ""; await loadGraph(); }
    };
    trackPristine(configBody, saveAgent);
    detail.append(section("Agent configuration", configBody));
  }
  const missing = (node.required_inputs || []).filter((input) => !input.satisfied_by);
  if (missing.length) detail.append(section("Human input", h("div", {}, missing.map((input) => renderInputCard(node, input)))));
  if (node.ui_state === "review" || node.ui_state === "verifying" || node.verification_status) {
    detail.append(section(node.review_owner === "parent" ? "Parent verification" : "Review", renderReviewCard(node)));
  }
  const nodeUsage = data.usage.by_node?.[node.id] || {};
  const branchUsage = data.usage.by_branch?.[node.id] || {};
  detail.append(section("Usage", h("div", { class: "metric-grid" }, [metric("Agent", formatCount(usageTokens(nodeUsage))), metric("Branch", formatCount(usageTokens(branchUsage))), metric("Cost", formatCost(branchUsage.cost_usd))])));
  detail.append(section("Actions", renderActions(node)));
  const scopeForm = h("form", { class: "scope-form inline-form" });
  const objective = h("input", { name: "objective", value: node.objective, maxlength: "160", "aria-label": "Node objective" });
  const prompt = h("textarea", { name: "prompt", "aria-label": "Agent instructions" }); prompt.value = node.generated_prompt || "";
  const saveScope = button("Save revision", null, "button accent", "check"); saveScope.type = "submit";
  const regenerate = button("Regenerate branch", async () => {
    const confirmed = await confirmAction("Regenerate this branch?", "Active descendants will be cancelled and preserved as history while a replacement plan is created.", "Regenerate");
    if (!confirmed) return;
    const result = await safe(() => request(`/api/nodes/${node.id}/regenerate`, { method: "POST" }), "Branch regenerated");
    if (result.ok) { data.detailSignature = ""; await loadGraph(); }
  }, "button", "git-branch");
  scopeForm.append(h("label", { class: "field" }, [h("span", { text: "Objective" }), objective]), h("label", { class: "field" }, [h("span", { text: "Agent instructions" }), prompt]), h("div", { class: "action-row" }, [saveScope, regenerate]));
  scopeForm.onsubmit = async (event) => { event.preventDefault(); const result = await safe(() => request(`/api/nodes/${node.id}/edit`, json("POST", { objective: objective.value.trim(), generated_prompt: prompt.value.trim() || null })), "Revision saved"); if (result.ok) { data.detailDirty = false; data.detailSignature = ""; await loadGraph(); } };
  trackPristine(scopeForm, saveScope);
  detail.append(section("Scope", scopeForm));
  hydrateIcons(detail);
}

function metric(label, value) { return h("div", { class: "metric" }, [h("small", { text: label }), h("strong", { text: value })]); }
function renderInputCard(node, input) {
  const field = h("textarea", { placeholder: input.description || `Answer: ${input.label}`, "aria-label": input.label });
  const send = button("Provide input", async () => {
    if (!field.value.trim()) return;
    const result = await safe(() => request(`/api/nodes/${node.id}/provide-input`, json("POST", { input_id: input.id, value: field.value.trim() })), "Input supplied");
    if (!result.ok) return;
    await loadGraph();
  }, "button accent", "arrow-up", "Supply this clarification and resume the branch");
  return h("div", { class: "input-card" }, [h("span", { class: "input-kind", text: input.kind }), h("h3", { text: input.label }), h("p", { text: input.description || "This branch needs a human decision before it can continue." }), field, send]);
}

function renderReviewCard(node) {
  if (!node.needs_review && node.ui_state === "review") {
    return h("div", { class: "review-card" }, [
      h("h3", { text: "Descendant review required" }),
      h("p", { text: "Resolve the highlighted descendant review gates before this container can complete." }),
    ]);
  }
  const parentOwned = node.review_owner === "parent";
  const status = node.verification_status || "pending";
  const title = parentOwned
    ? (({ running: "Parent is verifying", accepted: "Accepted by parent", rejected: "Changes requested by parent", error: "Parent verification needs attention" })[status] || "Awaiting parent verification")
    : "Review merged result";
  const fallback = parentOwned && status === "running"
    ? "The parent agent is inspecting the child diff, artifacts, logs, and focused checks."
    : "Accept the branch or return feedback to the same agent session and worktree.";
  const card = h("div", { class: `review-card verification-${status}` }, [
    h("h3", { text: title }),
    h("p", { text: (parentOwned || status === "error") && node.verification_summary ? node.verification_summary : fallback }),
    node.verification_round ? h("small", { class: "verification-round", text: `Verification round ${node.verification_round}` }) : null,
  ]);
  const allowed = new Set(node.allowed_actions || []);
  if (node.ui_state === "review" && allowed.has("accept") && allowed.has("reject")) {
    card.append(h("div", { class: "action-row" }, [button("Accept result", () => nodeAction(node, "accept"), "button accent", "check", "Keep the merged result and resolve review"), button("Request changes", () => showReject(card, node), "button", "message-square-more", "Continue the same agent session with feedback")]));
  }
  return card;
}

function renderActions(node) {
  const row = h("div", { class: "action-row" });
  const actions = new Set(node.allowed_actions || []);
  const labels = { run: "Run now", pause: "Pause", resume: "Resume", cancel: "Cancel", retry: "Retry" };
  const icons = { run: "play", pause: "pause", resume: "circle-play", cancel: "square-stop", retry: "rotate-cw" };
  const help = { run: "Run this node immediately", pause: "Pause automatic dispatch", resume: "Allow this node to run again", cancel: "Cancel without deleting history", retry: "Retry from preserved context" };
  for (const action of ["run", "pause", "resume", "retry", "cancel"]) {
    if (!actions.has(action)) continue;
    const style = action === "run" ? "button compact accent" : action === "cancel" ? "button compact danger" : "button compact";
    row.append(button(labels[action], () => nodeAction(node, action), style, icons[action], help[action]));
  }
  return row;
}

async function nodeAction(node, action) {
  const paths = { run: "run", pause: "pause", resume: "resume", cancel: "cancel", retry: "retry", regenerate: "regenerate", fork: "fork", accept: "accept" };
  if (!paths[action]) return;
  if (["cancel", "regenerate"].includes(action)) {
    const confirmed = await confirmAction(
      action === "cancel" ? "Cancel this run?" : "Regenerate this branch?",
      action === "cancel" ? "The current process will stop; its run history remains available." : "Active descendants will be cancelled and replaced. Existing history remains recoverable.",
      action === "cancel" ? "Cancel run" : "Regenerate",
    );
    if (!confirmed) return;
  }
  const result = await safe(() => request(`/api/nodes/${node.id}/${paths[action]}`, { method: "POST" }), action === "fork" ? "Fork created" : `${action[0].toUpperCase()}${action.slice(1)} requested`);
  if (!result.ok) return;
  await loadGraph();
}

async function branchAction(nodeId, action) {
  if (action === "cancel") {
    const confirmed = await confirmAction("Cancel this branch?", "Running descendants will stop. Their graph and run history remain available for inspection.", "Cancel branch");
    if (!confirmed) return;
  }
  const result = await safe(() => request(`/api/nodes/${nodeId}/branch`, json("POST", { action })), `${action[0].toUpperCase()}${action.slice(1)} applied to branch`);
  if (!result.ok) return;
  await loadGraph();
}

function showForkEditor(container, node) {
  data.detailEditing = true;
  const editor = h("div", { class: "inline-editor" });
  const objective = h("textarea", { "aria-label": "Fork objective" }); objective.value = node.objective;
  const prompt = h("textarea", { "aria-label": "Fork agent prompt" }); prompt.value = node.generated_prompt || "";
  editor.append(
    h("h3", { text: "Create an alternative branch" }),
    h("p", { text: "The fork inherits this planner's agent configuration and starts a separate alternative tree in this project." }),
    h("label", { class: "editor-field" }, [h("span", { text: "Alternative objective" }), objective]),
    h("label", { class: "editor-field" }, [h("span", { text: "Planning instructions" }), prompt]),
    h("div", { class: "action-row" }, [
      button("Create and plan fork", async () => {
        const result = await safe(() => request(`/api/nodes/${node.id}/fork`, json("POST", { objective: objective.value.trim(), generated_prompt: prompt.value.trim() || null })), "Alternative branch created");
        if (!result.ok) return;
        data.detailEditing = false; await loadGraph();
      }, "button accent", "git-fork"),
      button("Cancel", () => { data.detailEditing = false; renderDetail(); }, "button", "x"),
    ]),
  );
  container.replaceChildren(editor); objective.focus(); objective.select();
}

function showReject(card, node) {
  data.detailEditing = true;
  const text = h("textarea", { placeholder: "Describe what the parent should verify or what must change…", "aria-label": "Reviewer feedback" });
  card.replaceChildren(h("h3", { text: "Request changes" }), h("p", { text: "Feedback continues the same agent session with its current context and files." }), text, h("div", { class: "action-row" }, [button("Send to agent", async () => {
    if (!text.value.trim()) return;
    const result = await safe(() => request(`/api/nodes/${node.id}/reject`, json("POST", { feedback: text.value.trim() })), "Feedback sent to the same session");
    if (!result.ok) return;
    data.detailEditing = false; await loadGraph();
  }, "button accent", "arrow-up"), button("Cancel", () => { data.detailEditing = false; renderDetail(); }, "button", "x")]));
  text.focus();
}

function terminalText(node, response) {
  const transcript = (response.artifacts || []).slice().reverse().find((artifact) => artifact.name === "transcript");
  if (transcript?.content) return typeof transcript.content === "string" ? transcript.content : JSON.stringify(transcript.content, null, 2);
  const lastRun = (response.runs || []).at(-1);
  if (lastRun?.logs) return lastRun.logs;
  if (lastRun?.summary) return lastRun.summary;
  return node.ui_state === "running" ? "Starting agent session…" : "No terminal output for this node yet.";
}

function disposeTerminal(nodeId) {
  const session = data.terminal.get(nodeId);
  if (!session) return;
  try { session.observer?.disconnect(); session.socket?.close(); session.terminal?.dispose(); } catch {}
  data.terminal.delete(nodeId);
}

function renderTerminal(detail, node, response) {
  disposeTerminal(node.id);
  const output = h("div", { id: "terminal-output", class: "terminal-output", "aria-label": "Agent terminal" });
  const mode = h("span", { class: "terminal-mode", text: "CONNECTING" });
  const shell = h("div", { class: "terminal-shell" }, [
    h("div", { class: "terminal-head" }, [icon("terminal"), h("span", { class: "terminal-title", text: `${node.agent?.harness || node.executor || "agent"} · ${node.id.slice(0, 8)}` }), mode]),
    output,
  ]);
  detail.replaceChildren(shell);
  const terminal = new Terminal({
    convertEol: true,
    cursorBlink: false,
    cursorStyle: "bar",
    disableStdin: true,
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 11,
    lineHeight: 1.35,
    scrollback: 5000,
    allowTransparency: true,
    theme: { background: "#090b0e", foreground: "#c5ccd6", cursor: "#78a9ff", black: "#090b0e", red: "#ef7373", green: "#65c98a", yellow: "#dfb15b", blue: "#78a9ff", magenta: "#b28cff", cyan: "#65c9c4", white: "#e9eef5" },
  });
  const fit = new FitAddon(); terminal.loadAddon(fit); terminal.open(output);
  const historical = terminalText(node, response);
  if (historical) terminal.write(historical.replaceAll("\n", "\r\n"));
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/api/nodes/${node.id}/terminal`);
  const session = { terminal, fit, socket, observer: null, active: false };
  data.terminal.set(node.id, session);
  const setActive = (active) => {
    session.active = Boolean(active);
    mode.textContent = session.active ? "LIVE" : "TRANSCRIPT";
    terminal.options.disableStdin = !session.active;
    terminal.options.cursorBlink = session.active;
    const helper = output.querySelector(".xterm-helper-textarea");
    if (helper) {
      helper.disabled = !session.active;
      helper.readOnly = !session.active;
      helper.tabIndex = session.active ? 0 : -1;
      helper.setAttribute("aria-label", session.active ? "Terminal input" : "Terminal transcript");
    }
  };
  setActive(false);
  socket.onmessage = (event) => {
    let message; try { message = JSON.parse(event.data); } catch { return; }
    if (message.type === "snapshot") {
      setActive(message.active);
      if (message.output) { terminal.reset(); terminal.write(message.output); }
    } else if (message.type === "output") terminal.write(message.data || "");
    else if (message.type === "status") setActive(message.active);
  };
  socket.onclose = () => setActive(false);
  terminal.onData((value) => { if (session.active && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "input", data: value })); });
  terminal.onResize(({ cols, rows }) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "resize", cols, rows })); });
  session.observer = new ResizeObserver(() => { try { fit.fit(); } catch {} }); session.observer.observe(output);
  requestAnimationFrame(() => { try { fit.fit(); } catch {} });
}

function renderHistory(detail, node, response) {
  detail.replaceChildren();
  detail.append(section("Run history", h("div", {}, (response.runs || []).slice().reverse().map((run) => h("article", { class: "history-item" }, [h("div", { class: "history-top" }, [h("strong", { text: `${run.worker} · attempt ${run.attempt || 1}` }), h("small", { text: run.status })]), h("small", { text: `${formatCount(usageTokens(run.usage))} tokens · ${formatCost(run.usage?.cost_usd)}` }), run.summary ? h("p", { text: run.summary }) : null])))));
  detail.append(section("Artifacts", h("div", { class: "artifact-list" }, (response.artifacts || []).filter((a) => a.name !== "transcript").map((artifact) => {
    const rawName = artifact.ref ? artifact.ref.split(/[\\/]/).at(-1) : artifact.name;
    const summary = h("summary", {}, [icon(artifact.kind === "file" ? "file" : "braces"), h("strong", { text: rawName || artifact.name }), h("small", { text: artifact.kind })]);
    const body = artifact.content || artifact.ref ? h("div", { class: "artifact-detail" }, [artifact.ref ? h("code", { text: artifact.ref }) : null, artifact.content ? h("pre", { text: typeof artifact.content === "string" ? artifact.content.slice(0, 8000) : JSON.stringify(artifact.content, null, 2).slice(0, 8000) }) : null]) : null;
    return h("details", { class: "artifact-item" }, [summary, body]);
  }))));
}

function populateHarnesses() {
  const available = data.capabilities.harnesses.filter((item) => item.available);
  const preferred = available.some((item) => item.id === data.settings.default_harness)
    ? data.settings.default_harness
    : (available[0]?.id || "codex");
  data.settings.default_harness = preferred;
  for (const select of [$("#new-harness"), $("#setting-harness")]) {
    select.replaceChildren();
    if (!available.length) select.append(h("option", { value: "", text: "No harness detected", disabled: "disabled" }));
    for (const harness of data.capabilities.harnesses) select.append(h("option", { value: harness.id, text: `${harness.label}${harness.available ? "" : " · not found"}`, disabled: harness.available ? null : "disabled" }));
    select.value = preferred;
  }
  syncAgentCapabilityControls("new", data.settings.reasoning || "default");
  syncAgentCapabilityControls("setting", data.settings.reasoning || "default");
  syncAuthorDefaults();
}

function syncAuthorDefaults() {
  $("#new-harness").value = data.settings.default_harness || "codex";
  $("#new-model").value = data.settings.default_model || "";
  syncAgentCapabilityControls("new", data.settings.reasoning || "default");
  $("#new-permission").value = data.settings.permission || "workspace";
}

function openAuthoring(prefill = "") {
  if (data.app.pendingCommand) return;
  closeSidePanels();
  if (data.stream) data.stream.close();
  data.stream = null;
  dispatch({ type: "GO_HOME" });
  $("#author-prompt").value = prefill;
  $("#new-name").value = prefill.trim() ? projectDisplayName({ objective: prefill }) : "";
  $("#new-name").dataset.automatic = "true";
  $("#new-dir").value = "";
  $("#directory-btn").dataset.tooltip = "Project directory: current directory";
  data.attachments = [];
  renderAttachments();
  $("#author-config-panel").hidden = true;
  syncAuthorDefaults();
  $("#new-auto").checked = data.settings.default_auto_run !== false;
  $("#new-sequential").checked = Boolean(data.settings.force_sequential);
  $("#new-delay").value = data.settings.delay_between_jobs_ms || 0;
  syncProjectMode();
  renderProjects();
  setTimeout(() => $("#author-prompt").focus(), 0);
}

function renderAttachments() {
  const list = $("#attachment-list");
  list.replaceChildren(...data.attachments.map((attachment, index) => h("span", { class: "attachment-chip" }, [
    icon("paperclip"),
    h("span", { text: attachment.name, title: attachment.name }),
    h("button", { type: "button", "aria-label": `Remove ${attachment.name}`, onclick: () => { data.attachments.splice(index, 1); renderAttachments(); } }, icon("x")),
  ])));
  list.hidden = !data.attachments.length;
  hydrateIcons(list);
}

async function addAttachments(files) {
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) { toast(`${file.name} exceeds the 10 MB attachment limit`, "error"); continue; }
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 32768) binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
    data.attachments.push({ name: file.name, mime: file.type || null, content_base64: btoa(binary) });
  }
  renderAttachments();
}

async function chooseDirectory() {
  const result = await safe(() => request("/api/system/pick-directory", { method: "POST" }));
  if (!result.ok || result.value.cancelled) return;
  $("#new-dir").value = result.value.path;
  $("#directory-btn").dataset.tooltip = `Project directory: ${result.value.path}`;
  toast("Project directory selected");
}

function syncProjectMode() {
  const open = $("input[name='project-mode']:checked")?.value === "open";
  $("#project-submit").dataset.tooltip = open ? "Open existing project" : "Create workgraph";
  $("#project-submit").setAttribute("aria-label", open ? "Open existing project" : "Create workgraph");
  $("#project-submit").querySelector(".icon").style.setProperty("--icon-url", `url("${ICON_BASE}/${open ? "folder-open" : "arrow-up"}.svg")`);
  $("#new-dir").required = open;
}

async function createProject() {
  const prompt = $("#author-prompt").value.trim(); if (!prompt) return;
  const autoRun = $("#new-auto").checked;
  const payload = {
    name: $("#new-name").value.trim() || null,
    prompt,
    mode: $("input[name='project-mode']:checked").value,
    working_dir: $("#new-dir").value.trim() || null,
    agent: { type_id: "general", harness: $("#new-harness").value, model: $("#new-model").value.trim() || null, reasoning: $("#new-reasoning").value, permission: $("#new-permission").value, skills: [], tools: [], mcp_servers: [] },
    run_policy: { auto_run: autoRun, force_sequential: $("#new-sequential").checked, delay_between_jobs_ms: Number($("#new-delay").value || 0), timeout_seconds: Number(data.settings.timeout_seconds || 600), max_retries: Number(data.settings.max_retries || 1), retry_backoff_ms: Number(data.settings.retry_backoff_ms || 750), retry_choked_models: data.settings.retry_choked_models !== false, compact_on_context_pressure: true, review_mode: data.settings.auto_accept_merges ? "parent" : "manual" },
    attachments: data.attachments,
  };
  payload.run_policy.stall_timeout_seconds = Number(data.settings.stall_timeout_seconds || 90);
  const result = await safe(() => request("/api/projects", json("POST", payload)), "Project created; planning started");
  if (!result.ok) return;
  await loadProjects();
  await selectProject(result.value.project_id);
}

function showProjectMenu(project, buttonElement) {
  const popover = $("#popover"); popover.replaceChildren();
  popover.append(
    h("div", { class: "popover-label", text: "Project" }),
    h("button", { onclick: (event) => { event.stopPropagation(); showRenameProject(project, buttonElement); } }, [icon("pencil"), h("span", { text: "Rename…" })]),
    h("button", { class: "danger-option", onclick: () => removeProject(project) }, [icon("trash-2"), h("span", { text: "Remove from Turn…" })]),
  );
  const rect = buttonElement.getBoundingClientRect();
  popover.style.left = `${Math.max(8, rect.right - 220)}px`;
  popover.style.top = `${rect.bottom + 5}px`;
  popover.hidden = false;
}

function showRenameProject(project, buttonElement) {
  const popover = $("#popover");
  const input = h("input", { class: "popover-input", value: projectDisplayName(project), maxlength: "72", "aria-label": "Project name" });
  popover.replaceChildren(
    h("div", { class: "popover-label", text: "Rename project" }),
    input,
    h("button", { onclick: async () => {
      const name = input.value.trim(); if (!name) return;
      const result = await safe(() => request(`/api/projects/${project.id}`, json("PATCH", { name })), "Project renamed");
      if (!result.ok) return;
      popover.hidden = true;
      await loadProjects();
      if (project.id === data.app.projectId) $("#project-title").textContent = name;
    } }, [icon("check"), h("span", { text: "Save name" })]),
  );
  const rect = buttonElement.getBoundingClientRect();
  popover.style.left = `${Math.max(8, rect.right - 220)}px`;
  popover.style.top = `${rect.bottom + 5}px`;
  popover.hidden = false;
  setTimeout(() => { input.focus(); input.select(); }, 0);
}

async function removeProject(project) {
  $("#popover").hidden = true;
  if (!await confirmAction("Remove this project from Turn?", `“${projectDisplayName(project)}” will leave the sidebar. Repository files remain on disk.`, "Remove project")) return;
  const result = await safe(() => request(`/api/projects/${project.id}`, { method: "DELETE" }), "Project removed; repository files kept");
  if (!result.ok) return;
  if (project.id === data.app.projectId && data.stream) data.stream.close();
  localStorage.removeItem("turn.project");
  data.graph = { nodes: [], edges: [], artifacts: [] };
  data.usage = { totals: {}, by_node: {}, by_branch: {} };
  await loadProjects();
  dispatch({ type: "PROJECT_DELETED", hasProjects: Boolean(data.projects.length) });
  if (data.projects[0]) await selectProject(data.projects[0].id);
  else renderChrome();
}

function applyAppearance() {
  let theme = data.settings.theme || "dark";
  if (theme === "system") theme = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.density = data.settings.density || "comfortable";
}

function closeSidePanels() {
  $$(".side-panel").forEach((panel) => { panel.hidden = true; });
  hideTooltip();
  document.body.append($("#tooltip"));
  document.body.append($("#toast-region"));
  dispatch({ type: "CLOSE_OVERLAY" });
}

function openSettings() {
  if (data.app.overlay || data.app.pendingCommand) return;
  dispatch({ type: "OPEN_OVERLAY", overlay: "settings" });
  const map = { "setting-theme": "theme", "setting-density": "density", "setting-harness": "default_harness", "setting-model": "default_model", "setting-reasoning": "reasoning", "setting-permission": "permission", "setting-timeout": "timeout_seconds", "setting-stall-timeout": "stall_timeout_seconds", "setting-retries": "max_retries", "setting-backoff": "retry_backoff_ms", "setting-delay": "delay_between_jobs_ms" };
  for (const [id, key] of Object.entries(map)) $("#" + id).value = data.settings[key] ?? "";
  syncAgentCapabilityControls("setting", data.settings.reasoning || "default");
  $("#setting-auto").checked = data.settings.default_auto_run !== false; $("#setting-accept").checked = Boolean(data.settings.auto_accept_merges); $("#setting-sequential").checked = Boolean(data.settings.force_sequential); $("#setting-choked").checked = data.settings.retry_choked_models !== false;
  $("#settings-panel").hidden = false;
  trackPristine($("#settings-form"), $("#settings-form button[type='submit']"));
}

function openPolicy() {
  if (data.app.overlay || data.app.pendingCommand) return;
  const root = data.graph.nodes.find((node) => node.id === data.app.projectId);
  if (!root) return;
  const policy = root.run_policy || {};
  dispatch({ type: "OPEN_OVERLAY", overlay: "policy" });
  $("#policy-auto").checked = root.auto_run !== false;
  $("#policy-sequential").checked = Boolean(policy.force_sequential);
  $("#policy-accept").checked = ["parent", "auto_accept"].includes(policy.review_mode);
  $("#policy-delay").value = policy.delay_between_jobs_ms || 0;
  $("#policy-timeout").value = policy.timeout_seconds || 600;
  $("#policy-stall-timeout").value = policy.stall_timeout_seconds || 90;
  $("#policy-retries").value = policy.max_retries ?? 1;
  $("#policy-backoff").value = policy.retry_backoff_ms || 0;
  $("#policy-choked").checked = policy.retry_choked_models !== false;
  $("#policy-panel").hidden = false;
  trackPristine($("#policy-form"), $("#policy-form button[type='submit']"));
}

async function savePolicy(form) {
  const policy = {
    auto_run: $("#policy-auto").checked,
    force_sequential: $("#policy-sequential").checked,
    delay_between_jobs_ms: Number($("#policy-delay").value || 0),
    timeout_seconds: Number($("#policy-timeout").value || 600),
    stall_timeout_seconds: Number($("#policy-stall-timeout").value || 90),
    max_retries: Number($("#policy-retries").value || 0),
    retry_backoff_ms: Number($("#policy-backoff").value || 0),
    retry_choked_models: $("#policy-choked").checked,
    compact_on_context_pressure: true,
    review_mode: $("#policy-accept").checked ? "parent" : "manual",
  };
  const result = await safe(() => request(`/api/projects/${data.app.projectId}/policy`, json("POST", { run_policy: policy })), "Project policy updated");
  if (!result.ok) return;
  closeSidePanels();
  await loadGraph();
}

async function saveSettings(form) {
  const payload = { theme: $("#setting-theme").value, density: $("#setting-density").value, default_harness: $("#setting-harness").value, default_model: $("#setting-model").value.trim(), reasoning: $("#setting-reasoning").value, permission: $("#setting-permission").value, timeout_seconds: Number($("#setting-timeout").value), stall_timeout_seconds: Number($("#setting-stall-timeout").value), max_retries: Number($("#setting-retries").value), retry_backoff_ms: Number($("#setting-backoff").value), delay_between_jobs_ms: Number($("#setting-delay").value), default_auto_run: $("#setting-auto").checked, auto_accept_merges: $("#setting-accept").checked, force_sequential: $("#setting-sequential").checked, retry_choked_models: $("#setting-choked").checked };
  const result = await safe(() => request("/api/settings", json("POST", payload)), "Settings saved");
  if (!result.ok) return;
  data.settings = { ...data.settings, ...payload };
  applyAppearance();
  syncAuthorDefaults();
  closeSidePanels();
  scheduleReload(0);
}

async function setMode(autoRun) {
  if (!data.app.projectId) return;
  const result = await safe(() => request(`/api/projects/${data.app.projectId}/mode`, json("POST", { auto_run: autoRun })));
  if (!result.ok) return;
  await loadGraph();
}

async function stepProject() { if (data.app.projectId) { const result = await safe(() => request(`/api/projects/${data.app.projectId}/step`, { method: "POST" })); if (result.ok) scheduleReload(100); } }

function closeInspector() {
  if (data.app.selectedNodeId) disposeTerminal(data.app.selectedNodeId);
  data.detailEditing = false;
  data.detailDirty = false;
  data.detailSignature = "";
  dispatch({ type: "CLOSE_NODE" });
  renderGraph();
}

function wireEvents() {
  $("#command-btn").onclick = () => openAuthoring();
  $("#home-btn").onclick = () => openAuthoring();
  $("#settings-btn").onclick = openSettings;
  $("#help-btn").onclick = (event) => {
    const popover = $("#popover");
    popover.replaceChildren(
      h("div", { class: "popover-label", text: "Shortcuts" }),
      h("div", { class: "shortcut-row" }, [h("span", { text: "New project" }), h("kbd", { text: "⌘ K" })]),
      h("div", { class: "shortcut-row" }, [h("span", { text: "Toggle projects" }), h("kbd", { text: "⌘ B" })]),
      h("div", { class: "shortcut-row" }, [h("span", { text: "Settings" }), h("kbd", { text: "⌘ ," })]),
      h("div", { class: "popover-separator" }),
      h("p", { class: "popover-note", text: "Agents run in isolated project worktrees. Review permissions before starting." }),
    );
    const rect = event.currentTarget.getBoundingClientRect(); placePopover(rect.right - 230, rect.bottom + 4);
  };
  $("#project-filter").oninput = renderProjects;
  $("#sidebar-toggle").onclick = () => {
    if (matchMedia("(max-width: 760px)").matches) $("#project-sidebar").classList.toggle("mobile-open");
    else $("#app-shell").classList.toggle("sidebar-collapsed");
    const collapsed = $("#app-shell").classList.contains("sidebar-collapsed");
    $("#sidebar-toggle").setAttribute("aria-pressed", String(!collapsed));
    if (!collapsed) $("#project-filter").focus();
  };
  $("#attachment-btn").onclick = () => $("#attachment-input").click();
  $("#attachment-input").onchange = async (event) => { await addAttachments(event.target.files); event.target.value = ""; };
  $("#directory-btn").onclick = chooseDirectory;
  $("#author-config-btn").onclick = () => { $("#author-config-panel").hidden = !$("#author-config-panel").hidden; };
  $("#author-form").onsubmit = (event) => { event.preventDefault(); createProject(); };
  $("#new-name").oninput = () => { $("#new-name").dataset.automatic = "false"; };
  $("#author-prompt").oninput = () => {
    if ($("#new-name").dataset.automatic === "true") $("#new-name").value = projectDisplayName({ objective: $("#author-prompt").value });
  };
  $$("input[name='project-mode']").forEach((input) => input.onchange = syncProjectMode);
  $("#new-harness").onchange = () => syncAgentCapabilityControls("new");
  $("#new-model").oninput = () => syncAgentCapabilityControls("new");
  $("#new-model").onchange = () => syncAgentCapabilityControls("new");
  $("#setting-harness").onchange = () => syncAgentCapabilityControls("setting");
  $("#setting-model").oninput = () => syncAgentCapabilityControls("setting");
  $("#settings-form").onsubmit = (event) => { event.preventDefault(); saveSettings(event.currentTarget); };
  $("#policy-form").onsubmit = (event) => { event.preventDefault(); savePolicy(event.currentTarget); };
  $$(".panel-close").forEach((buttonElement) => buttonElement.onclick = closeSidePanels);
  $("#auto-mode-btn").onclick = () => setMode(true); $("#step-mode-btn").onclick = () => setMode(false); $("#run-step-btn").onclick = stepProject;
  $("#project-options-btn").onclick = openPolicy;
  $("#close-inspector").onclick = closeInspector;
  $$(".inspector-tabs button").forEach((buttonElement) => buttonElement.onclick = () => { if (data.app.tab === "terminal" && data.app.selectedNodeId) disposeTerminal(data.app.selectedNodeId); dispatch({ type: "SET_TAB", tab: buttonElement.dataset.tab }); data.app.tab = buttonElement.dataset.tab; data.detailSignature = ""; renderDetail({ force: true }); });
  $("#zoom-in").onclick = () => { data.zoom = Math.min(1.5, data.zoom + .1); renderGraph(); }; $("#zoom-out").onclick = () => { data.zoom = Math.max(.5, data.zoom - .1); renderGraph(); }; $("#zoom-fit").onclick = () => { const canvas = $(".graph-canvas"); if (!canvas) return; const host = $("#graph"); data.zoom = Math.min(1, Math.max(.5, (host.clientWidth - 30) / canvas.offsetWidth, (host.clientHeight - 30) / canvas.offsetHeight)); renderGraph(); };
  document.addEventListener("click", (event) => {
    hideTooltip();
    if (!event.target.closest("#help-btn") && !event.target.closest(".project-item-actions") && !event.target.closest("#project-options-btn") && !event.target.closest("[data-node-menu]") && !event.target.closest("#popover")) $("#popover").hidden = true;
  });
  document.addEventListener("pointerover", (event) => { const target = event.target.closest("[data-tooltip]"); if (target && !target.contains(event.relatedTarget)) showTooltip(target); });
  document.addEventListener("pointerout", (event) => { const target = event.target.closest("[data-tooltip]"); if (target && !target.contains(event.relatedTarget)) hideTooltip(); });
  document.addEventListener("focusin", (event) => { const target = event.target.closest("[data-tooltip]"); if (target) showTooltip(target); });
  document.addEventListener("focusout", (event) => { if (event.target.closest("[data-tooltip]")) hideTooltip(); });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "b") {
      event.preventDefault(); $("#sidebar-toggle").click(); return;
    }
    const command = resolveShortcut(event, data.app);
    if (!command) return;
    event.preventDefault();
    if (command === "new_project") openAuthoring();
    if (command === "settings") openSettings();
    if (command === "close_overlay") closeSidePanels();
    if (command === "close_node") closeInspector();
  });
}

hydrateIcons();
wireEvents();
init();
