/* GENERATED FILE. Source: turn.contracts.schema.public_schema. Do not edit. */

export interface Agent {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  tools: Array<string>;
  capabilities: Array<string>;
  session_id: string | null;
}

export type AgentType = "planner" | "executor" | "integrator" | "verifier";

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

export interface CapabilityStatus {
  capability_id: string;
  skills: number;
  mcps: number;
  loaded: boolean;
  installed: boolean;
}

export interface DocumentRef {
  ref: string;
  title: string | null;
  media_type: string | null;
  imports: Array<DocumentRef>;
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

export type EdgeType = "CONTAINS" | "FOLLOWS";

export type EventSource = "transition" | "agent_action" | "schedule" | "cli";

export interface Executor {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  tools: Array<string>;
  capabilities: Array<string>;
  session_id: string | null;
}

export interface FlowEdge {
  id: string;
  src: string;
  dst: string;
  type: FlowEdgeType;
}

export type FlowEdgeType = "RETURN";

export interface Graph {
  project_id: string;
  nodes: Array<Node>;
  edges: Array<Edge>;
  artifacts: Array<Artifact>;
  triggers: Array<Trigger>;
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
  verification: VerificationResult | null;
  trigger_context: TriggerContext | null;
  status: NodeStatus;
  paused: boolean;
  auto_run: boolean;
  run_policy: RunPolicy | null;
  required_inputs: Array<InputSpec>;
  resource_refs: Array<string>;
  document_refs: Array<DocumentRef>;
  subgraph_refs: Array<SubgraphRef>;
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
  capability_status: Array<CapabilityStatus>;
}

export interface GraphView {
  project_id: string;
  nodes: Array<GraphNodeView>;
  edges: Array<Edge>;
  flow_edges: Array<FlowEdge>;
  artifacts: Array<Artifact>;
  triggers: Array<Trigger>;
}

export type HarnessKind = "codex" | "claude" | "opencode" | "pi" | "mock" | "shell";

export type InputKind = "text" | "file" | "decision" | "credential" | "account" | "approval";

export interface InputSpec {
  id: string;
  label: string;
  kind: InputKind;
  description: string | null;
  satisfied_by: string | null;
}

export interface Integrator {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  tools: Array<string>;
  capabilities: Array<string>;
  session_id: string | null;
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
  verification: VerificationResult | null;
  trigger_context: TriggerContext | null;
  status: NodeStatus;
  paused: boolean;
  auto_run: boolean;
  run_policy: RunPolicy | null;
  required_inputs: Array<InputSpec>;
  resource_refs: Array<string>;
  document_refs: Array<DocumentRef>;
  subgraph_refs: Array<SubgraphRef>;
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
  agent_type: AgentType | null;
  required_inputs: Array<InputSpec>;
  resource_refs: Array<string>;
  document_refs: Array<DocumentRef>;
  subgraph_refs: Array<SubgraphRef>;
  artifacts: Array<ArtifactSpec>;
  capabilities: Array<string>;
  parent_key: string | null;
  follows: Array<string>;
  plan: boolean;
}

export type NodeStatus = "PENDING" | "BLOCKED" | "RUNNABLE" | "RUNNING" | "EXPANDED" | "COMPLETE" | "FAILED" | "CANCELLED";

export type NodeUIState = "queued" | "ready" | "running" | "preparing" | "paused" | "waiting_input" | "waiting_sequence" | "complete" | "container" | "failed" | "cancelled";

export type Outcome = "COMPLETE" | "EXPAND" | "BLOCK" | "FAIL";

export interface PlanResult {
  nodes: Array<NodeSpec>;
  project_name: string | null;
  document_refs: Array<DocumentRef>;
  subgraph_refs: Array<SubgraphRef>;
  artifacts: Array<ArtifactSpec>;
  edges: Array<EdgeSpec>;
  triggers: Array<TriggerSpec>;
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
  tools: Array<string>;
  capabilities: Array<string>;
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

export interface SubgraphRef {
  ref: string;
  title: string | null;
  media_type: string | null;
  managed: boolean;
}

export interface Trigger {
  id: string;
  project_id: string;
  target_node_id: string;
  event_name: string | null;
  kind: TriggerKind;
  schedule: string | null;
  data: Record<string, unknown>;
  enabled: boolean;
  last_fired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TriggerContext {
  trigger_id: string;
  event_id: string;
  event_name: string;
  data: Record<string, unknown>;
  source: EventSource;
  source_project_id: string | null;
  source_node_id: string | null;
  occurred_at: string;
}

export type TriggerKind = "event" | "schedule";

export interface TriggerSpec {
  target_key: string;
  event_name: string | null;
  kind: TriggerKind;
  schedule: string | null;
  data: Record<string, unknown>;
  enabled: boolean;
}

export interface Usage {
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export type VerificationDecision = "APPROVE" | "REJECT";

export interface VerificationResult {
  decision: VerificationDecision;
  summary: string;
  findings: Array<string>;
  required_changes: Array<string>;
  evidence_refs: Array<string>;
  target_node_id: string | null;
}

export interface Verifier {
  id: string;
  type_id: AgentType;
  harness: HarnessKind;
  model: string | null;
  reasoning: ReasoningLevel;
  tools: Array<string>;
  capabilities: Array<string>;
  session_id: string | null;
}

export interface WorkerResult {
  outcome: Outcome;
  summary: string;
  artifacts: Array<ArtifactSpec>;
  document_refs: Array<DocumentRef>;
  subgraph_refs: Array<SubgraphRef>;
  children: PlanResult | null;
  missing_inputs: Array<InputSpec>;
  error: string | null;
  retry_recommended: boolean;
  executor_notes: string | null;
  usage: Usage;
  session_id: string | null;
  verification: VerificationResult | null;
}
