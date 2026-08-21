import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { GraphNode, WorkItem } from "../domain";
import { WorkView } from "./WorkView";

const item = (overrides: Partial<WorkItem> = {}): WorkItem => ({
  id: "work-1",
  project_id: "project",
  organization_id: "root",
  node_id: "node-1",
  key: "first",
  agent_type: "executor",
  organization_contract: null,
  title: "Draft the deliverable",
  objective: "Create the first version",
  acceptance_criteria: [],
  priority: 0,
  status: "ACTIVE",
  depends_on: [],
  artifact_refs: [],
  evidence_refs: [],
  claimed_by: null,
  rejection_reason: null,
  budget_request_id: null,
  metadata: {},
  created_at: "",
  updated_at: "",
  ...overrides,
});

const node = (id: string, objective: string): GraphNode => ({
  id,
  project_id: "project",
  parent_id: null,
  objective,
  project_name: null,
  generated_prompt: null,
  repo_path: null,
  executor: "codex",
  agent: null,
  verification: null,
  status: "COMPLETE",
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
  ui_state: "complete",
  allowed_actions: [],
  state_reason: null,
  generation_active: false,
  capability_status: [],
});

describe("project work view", () => {
  it("keeps backlog visible without materializing it as graph nodes", () => {
    const rendered = renderToStaticMarkup(
      createElement(WorkView, {
        items: [
          item(),
          item({ id: "work-2", node_id: null, status: "BACKLOG", title: "Reserve a later wave" }),
          item({ id: "work-3", status: "BLOCKED", title: "Resolve an open decision" }),
        ],
        nodes: [node("root", "Root organization"), node("node-1", "Draft")],
        onSelectNode: () => undefined,
      }),
    );

    expect(rendered).toContain("Work");
    expect(rendered).toContain("Reserve a later wave");
    expect(rendered).toContain("Backlog");
    expect(rendered).toContain("Open node");
    expect(rendered).toContain("Blocked");
  });

  it("explains why a focused graph has no durable ticket backlog", () => {
    const rendered = renderToStaticMarkup(
      createElement(WorkView, {
        items: [],
        nodes: [
          {
            ...node("root", "Focused service"),
            organization_contract: { scale: "focused" } as GraphNode["organization_contract"],
          },
          node("step", "Implement the service"),
        ],
        onSelectNode: () => undefined,
      }),
    );

    expect(rendered).toContain("focused workflow has no durable work-item backlog");
  });
});
