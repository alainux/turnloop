export const initialAppState = Object.freeze({
  phase: "booting",
  connection: "offline",
  projectId: null,
  selectedNodeId: null,
  tab: "overview",
  pendingCommand: null,
  overlay: null,
});

export function reduceAppState(state, event) {
  switch (event.type) {
    case "BOOTED": return { ...state, phase: event.hasProjects ? "project" : "onboarding" };
    case "SELECT_PROJECT": return { ...state, phase: "loading", projectId: event.projectId, selectedNodeId: null, tab: "overview" };
    case "GRAPH_LOADED": return { ...state, phase: "project" };
    case "GRAPH_FAILED": return { ...state, phase: "project", connection: "offline", selectedNodeId: null, tab: "overview" };
    case "SELECT_NODE": return { ...state, selectedNodeId: event.nodeId, tab: event.tab || "overview" };
    case "CLOSE_NODE": return { ...state, selectedNodeId: null, tab: "overview" };
    case "GO_HOME": return { ...state, phase: "onboarding", connection: "offline", projectId: null, selectedNodeId: null, tab: "overview", pendingCommand: null, overlay: null };
    case "SET_TAB": return { ...state, tab: event.tab };
    case "STREAM_CONNECTING": return { ...state, connection: "connecting" };
    case "STREAM_OPEN": return { ...state, connection: "live" };
    case "STREAM_ERROR": return { ...state, connection: "reconnecting" };
    case "COMMAND_START": return state.pendingCommand ? state : { ...state, pendingCommand: event.command };
    case "COMMAND_DONE": return event.command && event.command !== state.pendingCommand ? state : { ...state, pendingCommand: null };
    case "OPEN_OVERLAY": return state.overlay ? state : { ...state, overlay: event.overlay };
    case "CLOSE_OVERLAY": return !event.overlay || event.overlay === state.overlay ? { ...state, overlay: null } : state;
    case "PROJECT_DELETED": return { ...initialAppState, phase: event.hasProjects ? "project" : "onboarding" };
    default: return state;
  }
}

export function resolveShortcut(event, state) {
  if (state.pendingCommand) return null;
  const commandKey = Boolean(event.metaKey || event.ctrlKey);
  const key = String(event.key || "").toLowerCase();
  if (commandKey && key === "k" && !state.overlay) return "new_project";
  if (commandKey && key === "," && !state.overlay) return "settings";
  if (key === "escape" && state.overlay) return "close_overlay";
  if (key === "escape" && !state.overlay) return "close_node";
  return null;
}

export function acceptsProjectResult(state, projectId) {
  return Boolean(projectId) && state.projectId === projectId;
}

export function deriveWorkgraphStatus(nodes = [], projectId = null) {
  const root = nodes.find((node) => node.id === projectId || !node.parent_id);
  const running = nodes.filter((node) => node.ui_state === "running").length;
  const verifying = nodes.filter((node) => node.ui_state === "verifying").length;
  const inputs = nodes.filter((node) => node.ui_state === "waiting_input").length;
  const manualReviews = nodes.filter((node) => node.ui_state === "review" && node.needs_review && node.review_owner === "manual").length;
  const parentReviews = nodes.filter((node) => node.ui_state === "review" && node.needs_review && node.review_owner === "parent").length;
  const activity = [];
  if (running) activity.push(`${running} running`);
  if (verifying) activity.push(`${verifying} verifying`);
  if (inputs + manualReviews) activity.push(`${inputs + manualReviews} needs you`);
  if (parentReviews) activity.push(`${parentReviews} awaiting parent verification`);
  if (activity.length) return activity.join(" · ");
  return root?.status === "COMPLETE" ? "Workgraph complete" : "Waiting for the next eligible node";
}
