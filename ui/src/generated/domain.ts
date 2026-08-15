/* GENERATED FILE. Source: turn.contracts.schema.public_schema. Do not edit. */

export interface Agent {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  permission: PermissionMode;
  skills: Array<string>;
  tools: Array<string>;
  mcp_servers: Array<string>;
  session_id: string | null;
}

export type AgentType = "planner" | "executor";

export interface Artifact {
  id: string;
  node_id: string | null;
  kind: ArtifactKind;
  name: string;
  content: unknown | null;
  ref: string | null;
  created_at: string;
}

export type ArtifactKind = "text" | "json" | "file" | "link" | "credential_ref" | "code_diff" | "log" | "evidence" | "user_input";

export interface ArtifactSpec {
  kind: ArtifactKind;
  name: string;
  content: unknown | null;
  ref: string | null;
}

export interface Edge {
  id: string;
  src: string;
  dst: string;
  type: EdgeType;
  created_at: string;
}

export interface EdgeSpec {
  type: EdgeType;
  src: string;
  dst: string;
}

export type EdgeType = "CONTAINS" | "DEPENDS_ON";

export interface Executor {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  permission: PermissionMode;
  skills: Array<string>;
  tools: Array<string>;
  mcp_servers: Array<string>;
  session_id: string | null;
}

export interface Graph {
  project_id: string;
  nodes: Array<Node>;
  edges: Array<Edge>;
  artifacts: Array<Artifact>;
}

export interface GraphNodeView {
  id: string;
  project_id: string;
  parent_id: string | null;
  objective: string;
  project_name: string | null;
  generated_prompt: string | null;
  repo_path: string | null;
  executor: string | null;
  agent: Agent | null;
  status: NodeStatus;
  paused: boolean;
  auto_run: boolean;
  run_policy: RunPolicy | null;
  required_inputs: Array<InputSpec>;
  resource_refs: Array<string>;
  artifact_refs: Array<string>;
  created_at: string;
  updated_at: string;
  progress: number | null;
  agent_state: string | null;
  agent_message: string | null;
  ui_state: NodeUIState;
  allowed_actions: Array<NodeAction>;
  state_reason: string | null;
  generation_active: boolean;
}

export interface GraphView {
  project_id: string;
  nodes: Array<GraphNodeView>;
  edges: Array<Edge>;
  artifacts: Array<Artifact>;
}

export type HarnessKind = "codex" | "claude" | "opencode" | "pi" | "echo" | "shell";

export type InputKind = "text" | "file" | "decision" | "credential" | "account" | "approval";

export interface InputSpec {
  id: string;
  label: string;
  kind: InputKind;
  description: string | null;
  satisfied_by: string | null;
}

export interface Node {
  id: string;
  project_id: string;
  parent_id: string | null;
  objective: string;
  project_name: string | null;
  generated_prompt: string | null;
  repo_path: string | null;
  executor: string | null;
  agent: Agent | null;
  status: NodeStatus;
  paused: boolean;
  auto_run: boolean;
  run_policy: RunPolicy | null;
  required_inputs: Array<InputSpec>;
  resource_refs: Array<string>;
  artifact_refs: Array<string>;
  created_at: string;
  updated_at: string;
  progress: number | null;
  agent_state: string | null;
  agent_message: string | null;
}

export type NodeAction = "run" | "pause" | "resume" | "cancel" | "retry" | "edit" | "regenerate" | "provide_input";

export interface NodeSpec {
  key: string;
  objective: string;
  generated_prompt: string | null;
  executor: string | null;
  agent: Agent | null;
  required_inputs: Array<InputSpec>;
  resource_refs: Array<string>;
  parent_key: string | null;
  depends_on: Array<string>;
  plan: boolean;
}

export type NodeStatus = "PENDING" | "BLOCKED" | "RUNNABLE" | "RUNNING" | "EXPANDED" | "COMPLETE" | "FAILED" | "CANCELLED";

export type NodeUIState = "queued" | "ready" | "running" | "paused" | "waiting_input" | "waiting_dependency" | "complete" | "container" | "failed" | "cancelled";

export type Outcome = "COMPLETE" | "EXPAND" | "BLOCK" | "FAIL";

export type PermissionMode = "ask" | "workspace" | "full";

export interface PlanResult {
  nodes: Array<NodeSpec>;
  edges: Array<EdgeSpec>;
  notes: string | null;
  usage: Usage;
  session_id: string | null;
}

export interface Planner {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  permission: PermissionMode;
  skills: Array<string>;
  tools: Array<string>;
  mcp_servers: Array<string>;
  session_id: string | null;
}

export type ReasoningLevel = "default" | "low" | "medium" | "high" | "xhigh" | "max";

export interface Run {
  id: string;
  node_id: string;
  worker: string;
  started_at: string;
  ended_at: string | null;
  status: RunStatus;
  outcome: Outcome | null;
  summary: string | null;
  logs: string;
  error: string | null;
  retry_recommended: boolean;
  attempt: number;
  usage: Usage;
  session_id: string | null;
}

export interface RunPolicy {
  auto_run: boolean;
  delay_between_jobs_ms: number;
  timeout_seconds: number;
  stall_timeout_seconds: number;
  max_retries: number;
  retry_backoff_ms: number;
  retry_choked_models: boolean;
  compact_on_context_pressure: boolean;
}

export type RunStatus = "RUNNING" | "COMPLETE" | "FAILED" | "CANCELLED";

export interface Usage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export interface WorkerResult {
  outcome: Outcome;
  summary: string;
  artifacts: Array<ArtifactSpec>;
  children: PlanResult | null;
  missing_inputs: Array<InputSpec>;
  error: string | null;
  retry_recommended: boolean;
  executor_notes: string | null;
  usage: Usage;
  session_id: string | null;
}
