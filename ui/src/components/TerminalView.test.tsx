import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ControlActivity, GraphNode } from "../domain";
import { TerminalView } from "./TerminalView";

const node = (id: string): GraphNode => ({
  id,
  project_id: "project",
  parent_id: null,
  objective: "Build the thing",
  project_name: null,
  generated_prompt: null,
  repo_path: null,
  executor: "pi",
  agent: null,
  verification: null,
  status: "RUNNING",
  paused: false,
  auto_run: false,
  run_policy: null,
  required_inputs: [],
  resource_refs: [],
  document_refs: [],
  artifact_refs: [],
  provides: [],
  consumes: [],
  outputs: {},
  route_taken: null,
  created_at: "",
  updated_at: "",
  progress: null,
  agent_state: null,
  agent_message: null,
  ui_state: "running",
  allowed_actions: [],
  state_reason: null,
  generation_active: false,
  capability_status: [],
});

const control = (terminalNodeId: string): ControlActivity => ({
  kind: "plan_audit",
  status: "running",
  started_at: "",
  attempt: 1,
  run_id: "run-1",
  terminal_node_id: terminalNodeId,
});

describe("terminal view control surface", () => {
  it("keeps the agent shell primary and exposes the audit terminal as a separate surface", () => {
    const rendered = renderToStaticMarkup(
      createElement(TerminalView, {
        node: node("11111111-1111-1111-1111-111111111111"),
        runs: [],
        control: control("22222222-2222-2222-2222-222222222222"),
      }),
    );

    // The organization's own shell is the default surface.
    expect(rendered).toContain("shell · 11111111");
    expect(rendered).not.toContain("control · plan audit · 22222222");
    // Both surfaces are explicitly selectable and labeled for what they are.
    expect(rendered).toContain(">Agent</button>");
    expect(rendered).toContain(">Plan audit</button>");
  });

  it("renders no surface switcher without control activity", () => {
    const rendered = renderToStaticMarkup(
      createElement(TerminalView, { node: node("11111111-1111-1111-1111-111111111111"), runs: [] }),
    );

    expect(rendered).toContain("shell · 11111111");
    expect(rendered).not.toContain("Manager review");
    expect(rendered).not.toContain(">Plan audit</button>");
  });
});
