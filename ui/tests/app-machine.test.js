import test from "node:test";
import assert from "node:assert/strict";
import { acceptsProjectResult, deriveWorkgraphStatus, initialAppState, reduceAppState, resolveShortcut } from "../app-machine.js";

test("onboarding, project loading, and graph-ready states are explicit", () => {
  let state = reduceAppState(initialAppState, { type: "BOOTED", hasProjects: false });
  assert.equal(state.phase, "onboarding");
  state = reduceAppState(state, { type: "SELECT_PROJECT", projectId: "p1" });
  assert.equal(state.phase, "loading");
  assert.equal(state.projectId, "p1");
  state = reduceAppState(state, { type: "GRAPH_LOADED" });
  assert.equal(state.phase, "project");
});

test("connection and command substates compose without erasing selection", () => {
  let state = { ...initialAppState, projectId: "p1", selectedNodeId: "n1" };
  state = reduceAppState(state, { type: "STREAM_CONNECTING" });
  state = reduceAppState(state, { type: "COMMAND_START", command: "cancel" });
  assert.equal(state.connection, "connecting");
  assert.equal(state.pendingCommand, "cancel");
  assert.equal(state.selectedNodeId, "n1");
  state = reduceAppState(state, { type: "STREAM_OPEN" });
  state = reduceAppState(state, { type: "COMMAND_DONE" });
  assert.equal(state.connection, "live");
  assert.equal(state.pendingCommand, null);
});

test("node tabs reset coherently across project changes", () => {
  let state = reduceAppState(initialAppState, { type: "SELECT_NODE", nodeId: "n1", tab: "terminal" });
  assert.equal(state.tab, "terminal");
  state = reduceAppState(state, { type: "SELECT_PROJECT", projectId: "p2" });
  assert.equal(state.selectedNodeId, null);
  assert.equal(state.tab, "overview");
});

test("home closes project, inspector, command, and live connection state", () => {
  const project = reduceAppState(initialAppState, { type: "SELECT_PROJECT", projectId: "p1" });
  const live = reduceAppState(reduceAppState(project, { type: "SELECT_NODE", nodeId: "n1", tab: "terminal" }), { type: "STREAM_OPEN" });
  const home = reduceAppState(live, { type: "GO_HOME" });
  assert.deepEqual(home, { ...initialAppState, phase: "onboarding" });
});

test("one command owns the busy substate until its matching completion", () => {
  const saving = reduceAppState(initialAppState, { type: "COMMAND_START", command: "save" });
  const duplicate = reduceAppState(saving, { type: "COMMAND_START", command: "delete" });
  assert.strictEqual(duplicate, saving);
  assert.equal(duplicate.pendingCommand, "save");
  const stale = reduceAppState(duplicate, { type: "COMMAND_DONE", command: "delete" });
  assert.strictEqual(stale, duplicate);
  assert.equal(reduceAppState(stale, { type: "COMMAND_DONE", command: "save" }).pendingCommand, null);
});

test("overlays cannot stack and shortcuts respect overlay and busy substates", () => {
  const project = reduceAppState(initialAppState, { type: "OPEN_OVERLAY", overlay: "project" });
  assert.equal(project.overlay, "project");
  assert.strictEqual(reduceAppState(project, { type: "OPEN_OVERLAY", overlay: "settings" }), project);
  assert.equal(resolveShortcut({ metaKey: true, key: "," }, project), null);
  const closed = reduceAppState(project, { type: "CLOSE_OVERLAY", overlay: "project" });
  assert.equal(resolveShortcut({ metaKey: true, key: "," }, closed), "settings");
  assert.equal(resolveShortcut({ ctrlKey: true, key: "k" }, closed), "new_project");
  assert.equal(resolveShortcut({ key: "Escape" }, { ...closed, selectedNodeId: "n1" }), "close_node");
  const busy = reduceAppState(closed, { type: "COMMAND_START", command: "save" });
  assert.equal(resolveShortcut({ metaKey: true, key: "k" }, busy), null);
});

test("late graph and stream results cannot overwrite a newer project selection", () => {
  const first = reduceAppState(initialAppState, { type: "SELECT_PROJECT", projectId: "p1" });
  assert.equal(acceptsProjectResult(first, "p1"), true);
  const second = reduceAppState(first, { type: "SELECT_PROJECT", projectId: "p2" });
  assert.equal(acceptsProjectResult(second, "p1"), false);
  assert.equal(acceptsProjectResult(second, "p2"), true);
});

test("a graph load failure exits loading and leaves a retryable project shell", () => {
  const loading = reduceAppState(initialAppState, { type: "SELECT_PROJECT", projectId: "p1" });
  const failed = reduceAppState({ ...loading, connection: "reconnecting", selectedNodeId: "stale" }, { type: "GRAPH_FAILED" });
  assert.equal(failed.phase, "project");
  assert.equal(failed.connection, "offline");
  assert.equal(failed.selectedNodeId, null);
});

test("workgraph status remains derived from graph state after command completion", () => {
  const complete = [{ id: "p1", parent_id: null, status: "COMPLETE", ui_state: "complete" }];
  let state = reduceAppState(
    { ...initialAppState, projectId: "p1" },
    { type: "COMMAND_START", command: "theme" },
  );
  state = reduceAppState(state, { type: "COMMAND_DONE", command: "theme" });
  assert.equal(state.pendingCommand, null);
  assert.equal(deriveWorkgraphStatus(complete, state.projectId), "Workgraph complete");
  assert.equal(
    deriveWorkgraphStatus([...complete, { id: "n1", parent_id: "p1", ui_state: "verifying" }], "p1"),
    "1 verifying",
  );
});

test("workgraph status counts only actionable human gates", () => {
  const root = { id: "p1", parent_id: null, status: "EXPANDED", ui_state: "review", needs_review: false, review_owner: "manual" };
  const manual = { id: "n1", parent_id: "p1", ui_state: "review", needs_review: true, review_owner: "manual" };
  const parent = { id: "n2", parent_id: "p1", ui_state: "review", needs_review: true, review_owner: "parent" };
  const input = { id: "n3", parent_id: "p1", ui_state: "waiting_input" };

  assert.equal(deriveWorkgraphStatus([root, manual], "p1"), "1 needs you");
  assert.equal(deriveWorkgraphStatus([root, parent], "p1"), "1 awaiting parent verification");
  assert.equal(deriveWorkgraphStatus([root, manual, input], "p1"), "2 needs you");
});
