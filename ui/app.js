"use strict";

const state = {
  projectId: null,
  graph: { nodes: [], edges: [], artifacts: [] },
  openNodeId: null,
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
  state.graph = await api(`/api/projects/${state.projectId}/graph`);
  renderGraph();
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
const G_BOX_W = 210;
const G_BOX_H = 54;
const G_COL = 250;
const G_ROW = 60;
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

  let maxX = 0;
  let maxY = 0;
  for (const p of pos.values()) {
    maxX = Math.max(maxX, p.x);
    maxY = Math.max(maxY, p.y);
  }
  const W = maxX + G_COL;
  const Ht = maxY + G_ROW;

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
    const line = document.createElementNS(SVGNS, "line");
    line.setAttribute("x1", pa.x + G_BOX_W / 2);
    line.setAttribute("y1", pa.y);
    line.setAttribute("x2", pb.x + G_BOX_W / 2);
    line.setAttribute("y2", pb.y);
    if (cls) line.setAttribute("class", cls);
    svg.appendChild(line);
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
      renderGraph();
      renderDetail();
    };
    canvas.appendChild(box);
  }

  container.appendChild(canvas);
}

// ---------------------------------------------------------------- detail
async function renderDetail() {
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
    termBox.appendChild(
      el("pre", { className: "terminal", id: "terminal-pane" }, transcriptContent),
    );
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
  const d = $("#detail");
  const objBox = d.querySelector(".kv div");
  const ta = document.createElement("textarea");
  ta.value = node.objective;
  ta.rows = 3;
  objBox.replaceWith(ta);
  const save = document.createElement("button");
  save.textContent = "Save revision";
  save.onclick = async () => {
    await api(`/api/nodes/${node.id}/edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ objective: ta.value }),
    });
    await loadGraph();
  };
  d.insertBefore(save, d.querySelector(".actions"));
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
        // Append raw Codex output to the open node's terminal pane (no graph reload).
        if (data.data && data.data.node_id === state.openNodeId) {
          const pane = document.getElementById("terminal-pane");
          if (pane) {
            pane.textContent += data.data.chunk;
            pane.scrollTop = pane.scrollHeight;
          }
        }
      } else {
        $("#status-line").textContent = "update: " + data.type;
        loadGraph();
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
  connectStream();
  await loadGraph();
});

refreshProjects();
