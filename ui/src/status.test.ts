import { describe, expect, it } from "vitest";
import { deriveStatus } from "./state";
import { nodeStatusLabel } from "./components/Graph";
import type { GraphNode } from "./domain";
const base: GraphNode = {
  id: "n",
  project_id: "n",
  parent_id: null,
  objective: "n",
  project_name: null,
  generated_prompt: null,
  architecture_spec: null,
  repo_path: null,
  executor: null,
  agent: null,
  verification: null,
  status: "RUNNING",
  run_policy: null,
  ui_state: "running",
  state_reason: null,
  agent_state: null,
  agent_message: null,
  allowed_actions: [],
  generation_active: false,
  paused: false,
  auto_run: false,
  required_inputs: [],
  resource_refs: [],
  artifact_refs: [],
  progress: null,
  created_at: "",
  updated_at: "",
};
describe("truthful status", () => {
  it("only calls a live provider generating", () => {
    expect(deriveStatus([base])).not.toContain("generating");
    expect(deriveStatus([{ ...base, generation_active: true }])).toBe(
      "1 model generating",
    );
  });
  it("shows the agent machine state and working message", () => {
    expect(
      nodeStatusLabel({
        ...base,
        agent_state: "working",
        agent_message: "Implementing the parser",
      }),
    ).toBe("working — Implementing the parser");
  });
});
