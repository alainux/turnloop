import { describe, expect, it } from "vitest";
import { nodeAgentIcon, nodeRunIcon, nodeRunLabel, triggerIcon } from "./Graph";
import { organizationManagerPhase, primaryNodeAction } from "../domain";
import type { GraphNode, Trigger } from "../domain";

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

  it("does not create a run control for a sequence-waiting node", () => {
    expect(
      primaryNodeAction(
        node({
          ui_state: "waiting_sequence",
          allowed_actions: ["pause", "edit"],
        }),
      ),
    ).toBeNull();
  });

  it("does not present historical manager state for a focused organization", () => {
    expect(
      organizationManagerPhase(
        node({
          organization_contract: {
            charter: "Ship one focused service",
            scale: "focused",
            deliverables: [],
            acceptance_criteria: [],
            constraints: [],
            quality_policy: [],
            decomposition_policy: "",
            completion_policy: "",
            budget: {
              max_active_workers: null,
              max_tokens: null,
              max_total_runs: null,
              max_input_tokens: null,
              max_output_tokens: null,
              max_cost_usd: null,
              max_wall_time_seconds: null,
            },
            min_first_level_production_owners: 1,
            require_independent_verification: false,
            max_replans: 0,
            escalation: {
              max_plan_corrections: 2,
              max_manager_iterations: 5,
              escalate_on_block: true,
            },
          },
          manager_phase: "REVIEW_PENDING",
          organization_review: {
            phase: "REPLAN",
            revision: 0,
            last_reason: null,
            audit: null,
            reviewed_at: null,
            replan_requested: false,
            review_count: 0,
            accept_count: 0,
            continue_count: 0,
            block_count: 0,
            last_decision: null,
            audit_decision: null,
            audit_summary: null,
            audit_findings: [],
            audit_required_changes: [],
            audit_correction_count: 0,
            audit_updated_at: null,
            control_retry_required: false,
            control_failure_reason: null,
          },
        }),
      ),
    ).toBeNull();
  });
});

describe("graph triggers", () => {
  it("uses distinct glyphs for event and schedule subscriptions", () => {
    expect(triggerIcon({ kind: "event" } as Trigger)).toBe("activity");
    expect(triggerIcon({ kind: "schedule" } as Trigger)).toBe("calendar");
  });
});
