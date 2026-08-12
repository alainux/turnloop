"use strict";

const state = {
  projectId: null,
  graph: { nodes: [], edges: [], artifacts: [] },
  openNodeId: null,
  projectAutoRun: true,
  editing: false,
  streaming: false,
  streamTimer: null,
  termFollow: true,
  es: null,
};

const $ = (sel) => document.querySelector(sel);
const api = (path, opts) => fetch(path, opts).then((r) => r.json());

// ---------------------------------------------------------------- projects
async function refreshProjects(selectId) {
  const { projects } = await api("/api/projects");
  const sel = $("#project-select");
  sel.innerHTML = '<option value="">— none —</option>';
  for (const p of projects) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = (p.objective || "").slice(0, 60);
    sel.appendChild(o);
  }
  if (selectId) sel.value = selectId;
}

async function createProject(prompt) {
  const res = await api("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  state.projectId = res.project_id;
  await refreshProjects(state.projectId);
  connectStream();
  await loadGraph();
}

// ---------------------------------------------------------------- graph
async function loadGraph() {
  if (!state.projectId) return;
  const data = await api(`/api/projects/${state.projectId}/graph`);
  state.graph = data;
  // project auto-run mode comes from the root node (id === project_id)
  const root = (data.nodes || []).find((n) => n.id === state.projectId) || (data.nodes || []).find((n) => !n.parent_id);
  if (root) state.projectAutoRun = root.auto_run !== false;
  renderGraph();
  syncModeControls();
  if (state.openNodeId) renderDetail();
}

function buildTree() {
  const { nodes, edges } = state.graph;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const children = new Map();
  let root = null;
  for (const n of nodes) {
    if (!n.parent_id) root = n;
    else {
      if (!children.has(n.parent_id)) children.set(n.parent_id, []);
      children.get(n.parent_id).push(n.id);
    }
  }
  return { byId, children, root, edges };
}

const SVGNS = "http://www.w3.org/2000/svg";
const G_BOX_W = 232;
const G_BOX_H = 66;
const G_COL = 290;
const G_ROW = 74;
const STATUS_COLOR = {
  COMPLETE: "#3fb950",
  RUNNING: "#5b8cff",
  BLOCKED: "#d29922",
  RUNNABLE: "#a371f7",
  PENDING: "#8b93a3",
  FAILED: "#f85149",
  CANCELLED: "#8b93a3",
  EXPANDED: "#5b8cff",
};

function layoutGraph() {
  const { byId, children, root } = buildTree();
  const pos = new Map();
  let yc = 0;
  function walk(id, depth) {
    const kids = children.get(id) || [];
    if (!kids.length) {
      pos.set(id, { x: depth * G_COL, y: yc * G_ROW });
      yc += 1;
      return yc - 1;
    }
    const ys = kids.map((k) => walk(k, depth + 1));
    const myY = (Math.min(...ys) + Math.max(...ys)) / 2;
    pos.set(id, { x: depth * G_COL, y: myY });
    return myY;
  }
  if (root) walk(root.id, 0);
  return { byId, children, root, pos };
}

function renderGraph() {
  const { byId, children, root, pos } = layoutGraph();
  const container = $("#graph");
  container.innerHTML = "";
  if (!root) {
    container.innerHTML = '<p class="muted">No workgraph yet. Enter an objective above.</p>';
    return;
  }

  // compute the bounding box of all node boxes, then shift so nothing is clipped
  let minBX = Infinity, minBY = Infinity, maxBX = -Infinity, maxBY = -Infinity;
  for (const p of pos.values()) {
    minBX = Math.min(minBX, p.x);
    minBY = Math.min(minBY, p.y - G_BOX_H / 2);
    maxBX = Math.max(maxBX, p.x + G_BOX_W);
    maxBY = Math.max(maxBY, p.y + G_BOX_H / 2);
  }
  const PAD = 16;
  const xOff = PAD - minBX;
  const yOff = PAD - minBY;
  for (const p of pos.values()) {
    p.x += xOff;
    p.y += yOff;
  }
  const W = maxBX - minBX + 2 * PAD;
  const Ht = maxBY - minBY + 2 * PAD;

  const canvas = el("div", {
    className: "graph-canvas",
    style: `width:${W}px;height:${Ht}px;`,
  });

  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("class", "graph-edges");
  svg.setAttribute("width", W);
  svg.setAttribute("height", Ht);
  canvas.appendChild(svg);

  const edge = (a, b, cls) => {
    const pa = pos.get(a);
    const pb = pos.get(b);
    if (!pa || !pb) return;
    const x1 = pa.x + G_BOX_W; // parent right-center
    const y1 = pa.y;
    const x2 = pb.x; // child left-center
    const y2 = pb.y;
    const midX = (x1 + x2) / 2;
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("d", `M ${x1} ${y1} H ${midX} V ${y2} H ${x2}`);
    path.setAttribute("class", cls || "edge-contains");
    path.setAttribute("fill", "none");
    svg.appendChild(path);
  };

  // CONTAINS (hierarchy) from parent_id
  for (const n of state.graph.nodes) {
    if (n.parent_id) edge(n.parent_id, n.id, "edge-contains");
  }
  // DEPENDS_ON (dependency) drawn dashed
  for (const e of state.graph.edges || []) {
    if (e.type === "DEPENDS_ON") edge(e.src, e.dst, "edge-depends");
  }

  // nodes
  for (const n of state.graph.nodes) {
    const p = pos.get(n.id);
    if (!p) continue;
    const box = el("div", {
      className: "gnode " + n.status + (state.openNodeId === n.id ? " selected" : ""),
      id: "gnode-" + n.id,
      style: `left:${p.x}px;top:${p.y - G_BOX_H / 2}px;width:${G_BOX_W}px;`,
    });
    box.style.borderLeftColor = STATUS_COLOR[n.status] || "#2a2f3a";
    box.appendChild(el("span", { className: "badge " + n.status }, n.status));
    box.appendChild(el("div", { className: "gobj" }, n.objective || "(no objective)"));
    if (n.progress != null && children.has(n.id)) {
      const pb = el("div", { className: "progress" });
      const fill = el("div", {});
      fill.style.width = Math.round((n.progress || 0) * 100) + "%";
      pb.appendChild(fill);
      box.appendChild(pb);
    }
    box.onclick = () => {
      state.openNodeId = n.id;
      state.streaming = false;
      if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
      renderGraph();
      renderDetail();
    };
    canvas.appendChild(box);
  }

  container.appendChild(canvas);
}

// ---------------------------------------------------------------- detail
async function renderDetail() {
  state.editing = false;
  state.streaming = false;
  if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
  const res = await api(`/api/nodes/${state.openNodeId}`);
  const node = res.node;
  const d = $("#detail");
  d.innerHTML = "";

  d.appendChild(el("div", { className: "kv" }, [
    el("label", {}, "Objective"),
    el("div", {}, node.objective || ""),
  ]));

  if (node.generated_prompt) {
    d.appendChild(el("div", { className: "kv" }, [
      el("label", {}, "Generated prompt"),
      el("pre", {}, node.generated_prompt),
    ]));
  }

  // inputs
  const inputs = node.required_inputs || [];
  if (inputs.length) {
    const box = el("div", { className: "kv" }, [el("label", {}, "Required inputs")]);
    for (const inp of inputs) {
      const satisfied = !!inp.satisfied_by;
      const tag = el("span", { className: "tag" + (satisfied ? "" : " missing") },
        `${inp.label} (${inp.kind})${satisfied ? " ✓" : ""}`);
      box.appendChild(tag);
      if (!satisfied) {
        const form = document.createElement("div");
        form.style.marginTop = "4px";
        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "Supply: " + inp.label;
        const btn = document.createElement("button");
        btn.className = "secondary";
        btn.textContent = "Provide";
        btn.onclick = async () => {
          await api(`/api/nodes/${node.id}/provide-input`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ input_id: inp.id, value: input.value }),
          });
          await loadGraph();
        };
        form.append(input, btn);
        box.appendChild(form);
      }
    }
    d.appendChild(box);
  }

  // actions
  const actions = el("div", { className: "actions" });
  const mk = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = cls || "";
    b.textContent = label;
    b.onclick = fn;
    return b;
  };
  if (node.status === "RUNNABLE") {
    actions.append(mk("Run ▶", "run", async () => { await runNode(node.id); }));
  }
  actions.append(
    mk("Edit", "secondary", () => editNode(node)),
    mk("Regenerate ↓", "secondary", async () => { await api(`/api/nodes/${node.id}/regenerate`, { method: "POST" }); await loadGraph(); }),
    mk("Fork", "secondary", async () => { await api(`/api/nodes/${node.id}/fork`, { method: "POST" }); await loadGraph(); }),
    mk("Retry", "secondary", async () => { await api(`/api/nodes/${node.id}/retry`, { method: "POST" }); await loadGraph(); }),
    mk("Pause", "secondary", async () => { await api(`/api/nodes/${node.id}/pause`, { method: "POST" }); await loadGraph(); }),
    mk("Resume", "secondary", async () => { await api(`/api/nodes/${node.id}/resume`, { method: "POST" }); await loadGraph(); }),
    mk("Cancel", "danger", async () => { await api(`/api/nodes/${node.id}/cancel`, { method: "POST" }); await loadGraph(); }),
  );
  d.appendChild(actions);

  // live transcript (raw Codex output) — terminal-styled pane
  const transcriptArt = (res.artifacts || []).find((a) => a.name === "transcript");
  const transcriptContent =
    transcriptArt && transcriptArt.content
      ? (typeof transcriptArt.content === "string"
          ? transcriptArt.content
          : JSON.stringify(transcriptArt.content, null, 2))
      : "";
  if (transcriptContent.length > 0 || node.status === "RUNNING") {
    const termBox = el("div", { className: "kv" }, [
      el("label", {}, "Live transcript (Codex)"),
    ]);
    const pane = el("pre", { className: "terminal", id: "terminal-pane" }, transcriptContent);
    pane.addEventListener("scroll", () => {
      // Remember whether the user is pinned to the bottom; if they scrolled
      // up to read history, stop auto-following so we don't yank them down.
      const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
      state.termFollow = atBottom;
    });
    if (state.termFollow) pane.scrollTop = pane.scrollHeight;
    termBox.appendChild(pane);
    d.appendChild(termBox);
  }

  // artifacts
  if (res.artifacts && res.artifacts.length) {
    d.appendChild(el("hr"));
    d.appendChild(el("label", { className: "kv" }, "Artifacts"));
    for (const a of res.artifacts) {
      if (a.name === "transcript") continue; // shown in the terminal pane above
      const box = el("div", { className: "artifact" });
      box.appendChild(el("strong", {}, a.name + " · " + a.kind));
      if (a.ref) box.appendChild(el("div", { className: "muted" }, a.ref));
      const content = a.content == null ? "" : (typeof a.content === "string" ? a.content : JSON.stringify(a.content, null, 2));
      if (content) box.appendChild(el("pre", {}, content.slice(0, 4000)));
      d.appendChild(box);
    }
  }

  // runs
  if (res.runs && res.runs.length) {
    d.appendChild(el("hr"));
    d.appendChild(el("label", { className: "kv" }, "Run history"));
    for (const r of res.runs.slice().reverse()) {
      const box = el("div", { className: "run" });
      box.appendChild(el("div", {}, `${r.worker} · ${r.status}${r.outcome ? " · " + r.outcome : ""}`));
      if (r.summary) box.appendChild(el("pre", {}, r.summary));
      d.appendChild(box);
    }
  }
}

function editNode(node) {
  state.editing = true;
  const d = $("#detail");
  const objBox = d.querySelector(".kv div");
  const ta = document.createElement("textarea");
  ta.value = node.objective;
  ta.rows = 3;
  objBox.replaceWith(ta);
  ta.focus();
  ta.select();
  const save = document.createElement("button");
  save.textContent = "Save revision";
  save.onclick = async () => {
    await api(`/api/nodes/${node.id}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ objective: ta.value }),
    });
    state.editing = false;
    await loadGraph();
  };
  const cancel = document.createElement("button");
  cancel.textContent = "Cancel";
  cancel.className = "secondary";
  cancel.onclick = () => {
    state.editing = false;
    loadGraph();
  };
  const actions = d.querySelector(".actions");
  d.insertBefore(save, actions);
  d.insertBefore(cancel, actions);
}

// ---------------------------------------------------------------- helpers
function el(tag, attrs, children) {
  const e = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (k === "className") e.className = v; else e.setAttribute(k, v);
  }
  for (const c of children || []) e.append(c);
  return e;
}

// ---------------------------------------------------------------- streaming
function connectStream() {
  if (state.es) state.es.close();
  if (!state.projectId) return;
  state.es = new EventSource(`/api/projects/${state.projectId}/stream`);
  state.es.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.type === "connected") {
        $("#status-line").textContent = "live";
      } else if (data.type === "node.terminal") {
        // Live Codex output for the open node: append in place, no graph reload.
        if (data.data && data.data.node_id === state.openNodeId) {
          const pane = document.getElementById("terminal-pane");
          if (pane) {
            pane.textContent += data.data.chunk;
            // Only auto-follow to the bottom if the user is already there;
            // if they scrolled up to read, leave their position alone.
            if (state.termFollow) pane.scrollTop = pane.scrollHeight;
          }
          // Mark the open node as actively streaming and arm a fallback that
          // ends "streaming" mode shortly after output stops (in case the
          // terminal-status event is missed).
          state.streaming = true;
          if (state.streamTimer) clearTimeout(state.streamTimer);
          state.streamTimer = setTimeout(() => {
            state.streaming = false;
            state.streamTimer = null;
            loadGraph();
          }, 3000);
        }
      } else {
        // Graph-change events. Avoid rebuilding the detail pane (which
        // recreates the terminal <pre> and resets its scroll) while the user
        // is editing or while the open node is actively producing output.
        const isOpenUpdate =
          data.type === "node.updated" && data.data && data.data.id === state.openNodeId;
        const st = isOpenUpdate ? data.data.status : null;
        const terminalTransition =
          st === "COMPLETE" || st === "FAILED" || st === "CANCELLED";

        if (isOpenUpdate && st === "RUNNING") state.streaming = true;

        if (state.editing) {
          // never reload while editing
        } else if (state.streaming && !terminalTransition) {
          // actively streaming: ignore intermediate updates (don't rebuild terminal)
        } else {
          if (terminalTransition) {
            state.streaming = false;
            if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
          }
          $("#status-line").textContent = "update: " + data.type;
          loadGraph();
        }
      }
    } catch (_) {}
  };
}

// ---------------------------------------------------------------- wire up
$("#prompt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#prompt-input").value.trim();
  if (!v) return;
  await createProject(v);
  $("#prompt-input").value = "";
});

$("#project-select").addEventListener("change", async (e) => {
  state.projectId = e.target.value || null;
  state.openNodeId = null;
  state.streaming = false;
  if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
  connectStream();
  await loadGraph();
});

document.getElementById("auto-run").addEventListener("change", (e) => setMode(e.target.checked));
document.getElementById("step-btn").addEventListener("click", stepProject);

function syncModeControls() {
  const cb = document.getElementById("auto-run");
  const step = document.getElementById("step-btn");
  if (cb) cb.checked = state.projectAutoRun;
  if (step) step.hidden = state.projectAutoRun;
}

async function setMode(autoRun) {
  if (!state.projectId) return;
  await api(`/api/projects/${state.projectId}/mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_run: autoRun }),
  });
  state.projectAutoRun = autoRun;
  syncModeControls();
}

async function stepProject() {
  if (!state.projectId) return;
  await api(`/api/projects/${state.projectId}/step`, { method: "POST" });
  await loadGraph();
}

async function runNode(nid) {
  if (!nid) return;
  await api(`/api/nodes/${nid}/run`, { method: "POST" });
  await loadGraph();
}

refreshProjects();
