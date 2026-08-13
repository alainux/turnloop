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
  termBuffer: "",
  autoAccept: false,
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
    o.title = p.repo_path ? `repo: ${p.repo_path}` : "";
    sel.appendChild(o);
  }
  if (selectId) sel.value = selectId;
  updateRepoLine();
}

// Reflect the currently-selected project's repo path in the project bar.
function updateRepoLine() {
  const sel = $("#project-select");
  const opt = sel && sel.selectedOptions && sel.selectedOptions[0];
  document.getElementById("repo-line").textContent = opt && opt.title ? opt.title : "";
}

async function createProject(prompt, mode, workingDir) {
  const m = mode || "create";
  const wd = workingDir && workingDir.trim() ? workingDir.trim() : null;
  const res = await api("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, mode: m, working_dir: wd }),
  });
  state.projectId = res.project_id;
  await refreshProjects(state.projectId);
  updateRepoLine();
  connectStream();
  await loadGraph();
}

function openNewModal() {
  const modal = document.getElementById("new-modal");
  document.getElementById("new-prompt").value = "";
  document.getElementById("new-dir").value = "";
  const create = document.getElementById("new-create");
  create.onclick = async () => {
    const prompt = document.getElementById("new-prompt").value.trim();
    if (!prompt) return;
    const mode = document.querySelector('input[name="new-mode"]:checked').value;
    const wd = document.getElementById("new-dir").value;
    modal.hidden = true;
    await createProject(prompt, mode, wd);
  };
  document.getElementById("new-cancel").onclick = () => { modal.hidden = true; };
  modal.hidden = false;
  document.getElementById("new-prompt").focus();
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

// A node that ran and was merged up into its parent is "done" but still
// awaits human acceptance before its subtree is cleaned. Surface that as a
// distinct BLOCKED status so it isn't confused with a fully-accepted COMPLETE.
function displayStatus(n) {
  if (n.parent_id && n.needs_review && !n.merge_accepted) return "BLOCKED";
  return n.status;
}

function layoutGraph() {
  const { byId, children, root } = buildTree();
  const pos = new Map();
  let yc = 0;
  function walk(id, depth) {
    const kids = children.get(id) || [];
    if (!kids.length) {
      const y = yc * G_ROW;
      pos.set(id, { x: depth * G_COL, y });
      yc += 1;
      return y;
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
      className: "gnode " + displayStatus(n) + (state.openNodeId === n.id ? " selected" : ""),
      id: "gnode-" + n.id,
      title: n.objective || "",
      style: `left:${p.x}px;top:${p.y - G_BOX_H / 2}px;width:${G_BOX_W}px;`,
    });
    box.style.borderLeftColor = STATUS_COLOR[displayStatus(n)] || "#2a2f3a";
    box.appendChild(el("span", { className: "badge " + displayStatus(n) }, displayStatus(n)));
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
      state.termBuffer = "";
      if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
      renderGraph();
      renderDetail();
    };
    canvas.appendChild(box);
  }

  container.appendChild(canvas);
}

// ---------------------------------------------------------------- detail
// Remember the scroll position of every inner scrollable block so that
// rebuilding the detail pane (on each SSE update) does not yank the user
// back to the top of the terminal / run history / artifacts.
function snapshotScroll(d) {
  const s = {};
  const pane = d.querySelector("#terminal-pane");
  if (pane) s.terminal = pane.scrollTop;
  d.querySelectorAll(".run").forEach((r) => {
    const idx = r.dataset.runIdx;
    const p = r.querySelector("pre");
    if (idx != null && p) s["run:" + idx] = p.scrollTop;
  });
  d.querySelectorAll(".artifact").forEach((a) => {
    const idx = a.dataset.artIdx;
    const p = a.querySelector("pre");
    if (idx != null && p) s["art:" + idx] = p.scrollTop;
  });
  return s;
}

function restoreScroll(d, s) {
  if (!s) return;
  const pane = d.querySelector("#terminal-pane");
  if (pane && s.terminal != null) pane.scrollTop = s.terminal;
  d.querySelectorAll(".run").forEach((r) => {
    const idx = r.dataset.runIdx;
    const p = r.querySelector("pre");
    if (idx != null && p && s["run:" + idx] != null) p.scrollTop = s["run:" + idx];
  });
  d.querySelectorAll(".artifact").forEach((a) => {
    const idx = a.dataset.artIdx;
    const p = a.querySelector("pre");
    if (idx != null && p && s["art:" + idx] != null) p.scrollTop = s["art:" + idx];
  });
}

async function renderDetail() {
  state.editing = false;
  state.streaming = false;
  if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
  const d = $("#detail");
  const scroll = snapshotScroll(d);
  const res = await api(`/api/nodes/${state.openNodeId}`);
  const node = res.node;
  d.innerHTML = "";

  // Small button factory, used by both the merge-review block and the actions
  // row below. Defined up front so it is available before any early return.
  const mk = (label, cls, fn) => {
    const b = document.createElement("button");
    b.className = cls || "";
    b.textContent = label;
    b.onclick = fn;
    return b;
  };

  d.appendChild(el("div", { className: "kv status-row" }, [
    el("label", {}, "Status"),
    el("span", { className: "badge " + displayStatus(node) }, displayStatus(node)),
  ]));
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

  // --- merge review ---------------------------------------------------
  // A node whose worktree was merged up into its parent is now redundant on
  // disk. Offer Accept (clean the subtree) or Reject (feedback into the same
  // node so it re-runs in place). The root has no parent, so it is never
  // reviewed.
  if (node.parent_id && node.needs_review && !node.merge_accepted) {
    const review = el("div", { className: "review" });
    review.appendChild(el("div", { className: "kv" }, [
      el("label", {}, "Merged up — review result"),
    ]));
    const rb = el("div", { className: "actions" });
    rb.appendChild(mk("Accept ✓", "run", async () => {
      await api(`/api/nodes/${node.id}/accept`, { method: "POST" });
      await loadGraph();
    }));
    rb.appendChild(mk("Reject ↺", "secondary", () => showRejectForm(review, node)));
    review.appendChild(rb);
    d.appendChild(review);
  } else if (node.merge_accepted) {
    d.appendChild(el("div", { className: "kv accepted-note" },
      "✓ Merged & cleaned — subtree removed from disk"));
  }

  // actions — only show controls that make sense for the current status
  const actions = el("div", { className: "actions" });
  const st = node.status;
  const act = (label, cls, fn) => actions.append(mk(label, cls, fn));

  if (st === "RUNNABLE" || st === "PAUSED" || st === "CANCELLED") {
    act("Run ▶", "run", () => runNode(node.id));
  }
  if (st === "RUNNING") {
    act("Cancel", "danger", () => nodeAction(node.id, "cancel"));
  }
  if (st === "PAUSED") {
    act("Resume", "secondary", () => nodeAction(node.id, "resume"));
  }
  if (st === "FAILED") {
    act("Retry", "secondary", () => nodeAction(node.id, "retry"));
  }
  if (st !== "RUNNING") {
    act("Edit", "secondary", () => editNode(node));
    act("Regenerate ↓", "secondary", () => nodeAction(node.id, "regenerate"));
    act("Fork", "secondary", () => nodeAction(node.id, "fork"));
  }
  if (st === "RUNNABLE" || st === "BLOCKED") {
    act("Pause", "secondary", () => nodeAction(node.id, "pause"));
  }
  if (st !== "RUNNING" && st !== "COMPLETE") {
    act("Cancel", "danger", () => nodeAction(node.id, "cancel"));
  }
  d.appendChild(actions);

  // live transcript (raw Codex output) — terminal-styled pane
  const transcriptArt = (res.artifacts || []).find((a) => a.name === "transcript");
  const artifactText =
    transcriptArt && transcriptArt.content
      ? (typeof transcriptArt.content === "string"
          ? transcriptArt.content
          : JSON.stringify(transcriptArt.content, null, 2))
      : "";
  // While the node is running, prefer the accumulated live buffer (the
  // transcript artifact is only written once the run completes).
  const liveText =
    node.status === "RUNNING" && state.termBuffer ? state.termBuffer : artifactText;
  if (liveText.length > 0 || node.status === "RUNNING") {
    const termBox = el("div", { className: "kv" }, [
      el("label", {}, "Live transcript (Codex)"),
    ]);
    const pane = el("pre", { className: "terminal", id: "terminal-pane" }, liveText);
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
    let ai = 0;
    for (const a of res.artifacts) {
      if (a.name === "transcript") continue; // shown in the terminal pane above
      const box = el("div", { className: "artifact", "data-art-idx": String(ai) });
      box.appendChild(el("strong", {}, a.name + " · " + a.kind));
      if (a.ref) box.appendChild(el("div", { className: "muted" }, a.ref));
      const content = a.content == null ? "" : (typeof a.content === "string" ? a.content : JSON.stringify(a.content, null, 2));
      if (content) box.appendChild(el("pre", {}, content.slice(0, 4000)));
      d.appendChild(box);
      ai++;
    }
  }

  // runs
  if (res.runs && res.runs.length) {
    d.appendChild(el("hr"));
    d.appendChild(el("label", { className: "kv" }, "Run history"));
    res.runs.slice().reverse().forEach((r, i) => {
      const box = el("div", { className: "run", "data-run-idx": String(i) });
      box.appendChild(el("div", {}, `${r.worker} · ${r.status}${r.outcome ? " · " + r.outcome : ""}`));
      if (r.summary) box.appendChild(el("pre", {}, r.summary));
      d.appendChild(box);
    });
  }
  restoreScroll(d, scroll);
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
  const editActions = document.createElement("div");
  editActions.className = "edit-actions";
  editActions.append(save, cancel);
  const actions = d.querySelector(".actions");
  d.insertBefore(editActions, actions);
}

function showRejectForm(container, node) {
  const rb = container.querySelector(".actions");
  if (rb) rb.remove();
  const ta = document.createElement("textarea");
  ta.rows = 3;
  ta.placeholder = "Feedback for this node (re-run in place)…";
  const send = document.createElement("button");
  send.textContent = "Send feedback ↺";
  send.onclick = async () => {
    if (!ta.value.trim()) return;
    await api(`/api/nodes/${node.id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback: ta.value }),
    });
    await loadGraph();
  };
  const cancel = document.createElement("button");
  cancel.textContent = "Cancel";
  cancel.className = "secondary";
  cancel.onclick = () => loadGraph();
  const form = document.createElement("div");
  form.className = "actions";
  form.append(send, cancel);
  container.append(ta, form);
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

// ---------------------------------------------------------------- scroll guard
// Rebuilding the detail pane tears down its scrollable blocks; doing that in
// the middle of a user scroll gesture "blocks" the scroll. While the user is
// actively scrolling we skip reloads and instead refresh once they settle.
let userScrolling = false;
let userScrollTimer = null;
function markUserScroll() {
  userScrolling = true;
  if (userScrollTimer) clearTimeout(userScrollTimer);
  userScrollTimer = setTimeout(() => {
    userScrolling = false;
    if (!state.editing && !state.streaming) loadGraph();
  }, 450);
}
function scheduleReload() {
  if (userScrolling) return;
  loadGraph();
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
          // Accumulate into a persistent buffer so the transcript survives any
          // incidental detail-pane rebuild (which would otherwise recreate the
          // <pre> and wipe streamed output). Render the full buffer each time.
          state.termBuffer += data.data.chunk;
          const pane = document.getElementById("terminal-pane");
          if (pane) {
            pane.textContent = state.termBuffer;
            if (state.termFollow) pane.scrollTop = pane.scrollHeight;
          }
          state.streaming = true;
          if (state.streamTimer) clearTimeout(state.streamTimer);
          state.streamTimer = setTimeout(() => {
            state.streaming = false;
            state.streamTimer = null;
          }, 3000);
        }
      } else {
        const isOpenUpdate =
          data.type === "node.updated" && data.data && data.data.id === state.openNodeId;
        const st = isOpenUpdate ? data.data.status : null;
        const terminalTransition =
          st === "COMPLETE" || st === "FAILED" || st === "CANCELLED";
        // A fresh run of the open node starts a new live transcript.
        if (data.type === "run.created" && data.data && data.data.node_id === state.openNodeId) {
          state.termBuffer = "";
        }
        if (isOpenUpdate && st === "RUNNING") state.streaming = true;
        if (terminalTransition) {
          // Live run finished: the transcript artifact now holds the full
          // output, so drop the accumulated live buffer.
          state.streaming = false;
          state.termBuffer = "";
          if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
        }
        // While the open node is actively streaming (or the user is editing),
        // never rebuild the detail pane -- that recreates the terminal <pre>
        // and would lose scroll. The persistent termBuffer keeps the live
        // transcript intact across any incidental reloads.
        if (state.editing || state.streaming) return;
        $("#status-line").textContent = "update: " + data.type;
        scheduleReload();
      }
    } catch (_) {}
  };
}

// ---------------------------------------------------------------- wire up
document.getElementById("new-btn").addEventListener("click", openNewModal);

document.getElementById("new-modal").addEventListener("click", (e) => {
  if (e.target.id === "new-modal") e.target.hidden = true;
});

$("#project-select").addEventListener("change", async (e) => {
  state.projectId = e.target.value || null;
  state.openNodeId = null;
  state.streaming = false;
  if (state.streamTimer) { clearTimeout(state.streamTimer); state.streamTimer = null; }
  // Show the selected project's repo path.
  updateRepoLine();
  connectStream();
  await loadGraph();
});

document.getElementById("auto-run").addEventListener("change", (e) => setMode(e.target.checked));
document.getElementById("step-btn").addEventListener("click", stepProject);
document.getElementById("clear-btn").addEventListener("click", clearProjects);

// Don't let live updates rebuild the detail pane in the middle of a scroll.
document.getElementById("detail-pane").addEventListener("scroll", markUserScroll, { passive: true });
document.addEventListener("wheel", markUserScroll, { passive: true });
document.addEventListener("touchmove", markUserScroll, { passive: true });

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
  // Remember this choice as the default for future projects.
  try {
    await api(`/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_auto_run: autoRun }),
    });
  } catch (_) {}
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

// Generic node action (pause/resume/cancel/retry/regenerate/fork) that
// refreshes the graph afterwards.
async function nodeAction(id, action) {
  await api(`/api/nodes/${id}/${action}`, { method: "POST" });
  await loadGraph();
}

async function clearProjects() {
  if (!confirm("Clear all projects? This cannot be undone.")) return;
  await api("/api/projects", { method: "DELETE" });
  state.projectId = null;
  state.openNodeId = null;
  if (state.es) { state.es.close(); state.es = null; }
  await refreshProjects();
  await loadGraph();
}

document.getElementById("auto-accept").addEventListener("change", (e) => setAutoAccept(e.target.checked));

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    state.autoAccept = !!s.auto_accept_merges;
    const cb = document.getElementById("auto-accept");
    if (cb) cb.checked = state.autoAccept;
  } catch (_) {}
}

async function setAutoAccept(v) {
  state.autoAccept = !!v;
  try {
    await api(`/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_accept_merges: !!v }),
    });
  } catch (_) {}
}

refreshProjects();
loadSettings();
