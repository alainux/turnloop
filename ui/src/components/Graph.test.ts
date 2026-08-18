import { describe, expect, it } from "vitest";
import { nodeAgentIcon, nodeRunIcon, nodeRunLabel } from "./Graph";
import { primaryNodeAction } from "../domain";
import type { GraphNode } from "../domain";

const node = (overrides: Partial<GraphNode> = {}): GraphNode =>
  ({
    id: "node",
    project_id: "project",
    parent_id: null,
    objective: "Node",
    project_name: null,
    generated_prompt: null,
    repo_path: null,
    executor: "codex",
    agent: { type_id: "planner" },
    verification: null,
    status: "READY",
    paused: false,
    auto_run: false,
    run_policy: null,
    required_inputs: [],
    resource_refs: [],
    document_refs: [],
    artifact_refs: [],
    created_at: "",
    updated_at: "",
    progress: null,
    agent_state: null,
    agent_message: null,
    ui_state: "ready",
    allowed_actions: ["run"],
    state_reason: null,
    generation_active: false,
    capability_status: [],
    ...overrides,
  }) as GraphNode;

describe("graph node controls", () => {
  it("keeps the agent avatar tied to role rather than run state", () => {
    const planner = node();

    expect(nodeAgentIcon(planner)).toBe("git-branch");
    expect(nodeAgentIcon({ ...planner, status: "RUNNING", ui_state: "running", generation_active: true })).toBe(
      "git-branch",
    );
    expect(nodeAgentIcon({ ...planner, status: "FAILED", ui_state: "failed", generation_active: false })).toBe(
      "git-branch",
    );
  });

  it("keeps run state on the play/stop control", () => {
    expect(nodeRunIcon(false, "run")).toBe("play");
    expect(nodeRunIcon(true, "cancel")).toBe("stop");
    expect(nodeRunIcon(false, "retry")).toBe("rotate-cw");
    expect(nodeRunIcon(false, "regenerate")).toBe("rotate-cw");
    expect(nodeRunIcon(false, "run", true)).toBe("rotate-cw");
    expect(nodeRunLabel(false, "run", true)).toBe("Run again");
  });

  it("does not create a run control for a dependency-waiting node", () => {
    expect(
      primaryNodeAction(
        node({
          ui_state: "waiting_dependency",
          allowed_actions: ["pause", "edit"],
        }),
      ),
    ).toBeNull();
  });
});
