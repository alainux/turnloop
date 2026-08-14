export type NodeStatus =
  | "PENDING"
  | "BLOCKED"
  | "RUNNABLE"
  | "RUNNING"
  | "EXPANDED"
  | "COMPLETE"
  | "FAILED"
  | "CANCELLED";
export type UIState =
  | "pending"
  | "ready"
  | "running"
  | "verifying"
  | "review"
  | "waiting_input"
  | "waiting_dependency"
  | "complete"
  | "accepted"
  | "failed"
  | "cancelled"
  | "paused";
export type HarnessId =
  "codex" | "claude" | "opencode" | "pi" | "echo" | "shell";
export type Reasoning = "default" | "low" | "medium" | "high" | "xhigh" | "max";
export type Permission = "ask" | "workspace" | "full";

export interface AgentConfig {
  type_id: string;
  harness: HarnessId;
  model: string | null;
  reasoning: Reasoning;
  permission: Permission;
  skills: string[];
  tools: string[];
  mcp_servers: string[];
  session_id: string | null;
}

export interface RunPolicy {
  auto_run: boolean;
  force_sequential: boolean;
  delay_between_jobs_ms: number;
  timeout_seconds: number;
  stall_timeout_seconds: number;
  max_retries: number;
  retry_backoff_ms: number;
  retry_choked_models: boolean;
  compact_on_context_pressure: boolean;
  review_mode: "manual" | "parent" | "auto_accept";
}

export interface InputSpec {
  id: string;
  label: string;
  kind: string;
  description?: string;
  satisfied_by?: string | null;
}
export interface GraphNode {
  id: string;
  project_id: string;
  parent_id: string | null;
  objective: string;
  project_name?: string | null;
  generated_prompt?: string | null;
  repo_path?: string | null;
  executor?: string | null;
  agent?: AgentConfig | null;
  status: NodeStatus;
  ui_state: UIState;
  state_reason?: string | null;
  allowed_actions: string[];
  generation_active: boolean;
  paused: boolean;
  auto_run: boolean;
  run_policy?: RunPolicy | null;
  required_inputs: InputSpec[];
  revision: number;
  progress?: number | null;
  needs_review: boolean;
  merge_accepted: boolean;
  superseded_by?: string | null;
  forked_from?: string | null;
  review_owner?: "manual" | "parent";
  verification_status?:
    "pending" | "running" | "accepted" | "rejected" | "error" | null;
  verification_summary?: string | null;
  verification_round: number;
  created_at?: string;
  updated_at?: string;
}
export interface Edge {
  id: string;
  src: string;
  dst: string;
  type: "CONTAINS" | "DEPENDS_ON";
}
export interface Artifact {
  id: string;
  node_id: string;
  kind: string;
  name: string;
  content?: unknown;
  ref?: string | null;
}
export interface Run {
  id: string;
  worker: string;
  status: string;
  attempt?: number;
  summary?: string | null;
  logs?: string | null;
  usage?: Usage;
}
export interface Usage {
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  cost_usd?: number | null;
}
export interface GraphResponse {
  project_id: string;
  nodes: GraphNode[];
  edges: Edge[];
  artifacts: Artifact[];
}
export interface NodeDetail {
  node: GraphNode;
  runs: Run[];
  artifacts: Artifact[];
}
export interface ModelCapability {
  id: string;
  label: string;
  reasoning?: Reasoning[];
  source?: string;
}
export interface HarnessCapability {
  id: HarnessId;
  label: string;
  available: boolean;
  models: ModelCapability[];
  reasoning: Reasoning[];
  accepts_custom_models: boolean;
}
export interface Capabilities {
  harnesses: HarnessCapability[];
}
export interface ProjectsResponse {
  projects: GraphNode[];
}
export interface UsageResponse {
  totals: Usage;
  by_node: Record<string, Usage>;
  by_branch: Record<string, Usage>;
}

export function isGraphResponse(value: unknown): value is GraphResponse {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.project_id === "string" &&
    Array.isArray(record.nodes) &&
    Array.isArray(record.edges)
  );
}

export function tokens(usage?: Usage): number {
  return (
    (usage?.input_tokens ?? 0) +
    (usage?.cached_input_tokens ?? 0) +
    (usage?.output_tokens ?? 0)
  );
}
