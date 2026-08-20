/* GENERATED FILE. Source: turn.contracts.schema.public_schema. Do not edit. */

export interface AcceptanceCriterion {
  id: string;
  description: string;
}

export interface AcceptanceEvidence {
  criterion_id: string;
  status: EvidenceStatus;
  summary: string;
  refs: Array<string>;
}

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
  schema_name: string | null;
  schema_version: string | null;
  evidence_refs: Array<string>;
  created_at: string;
}

export type ArtifactKind = "text" | "json" | "file" | "link" | "credential_ref" | "code_diff" | "log" | "evidence" | "user_input";

export interface ArtifactSpec {
  kind: ArtifactKind;
  name: string;
  content: unknown | null;
  ref: string | null;
  schema_name: string | null;
  schema_version: string | null;
  evidence_refs: Array<string>;
}

export interface BehaviorExpectations {
  read_docs: boolean | null;
  use_skills: boolean | null;
  verify_after_changes: boolean | null;
}

export interface BudgetRequest {
  id: string;
  project_id: string;
  organization_id: string;
  requested_budget: OrganizationBudget;
  reason: string;
  status: BudgetRequestStatus;
  decision_reason: string | null;
  requested_at: string;
  reviewed_at: string | null;
}

export type BudgetRequestStatus = "PENDING" | "APPROVED" | "REJECTED";

export interface CapabilityStatus {
  capability_id: string;
  skills: number;
  mcps: number;
  loaded: boolean;
  installed: boolean;
}

export interface ControlActivity {
  kind: "plan_audit" | "manager_review";
  status: "running";
  started_at: string;
  attempt: number;
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

export type EvidenceStatus = "PASS" | "FAIL" | "UNVERIFIED";

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
  work_items: Array<WorkItem>;
  handoffs: Array<Handoff>;
  budget_requests: Array<BudgetRequest>;
}

export interface GraphNodeView {
  id: string;
  project_id: string;
  parent_id: string | null;
  objective: string;
  project_name: string | null;
  generated_prompt: string | null;
  repo_path: string | null;
  workspace_path: string | null;
  workspace_commit: string | null;
  workspace: WorkspaceRef | null;
  output_branch: string | null;
  executor: string | null;
  agent: Agent | null;
  verification: VerificationResult | null;
  trigger_context: TriggerContext | null;
  status: NodeStatus;
  paused: boolean;
  auto_run: boolean;
  run_policy: RunPolicy | null;
  organization_contract: OrganizationContract | null;
  organization_review: OrganizationReview | null;
  manager_phase: ManagerPhase | null;
  manager_iteration: number;
  manager_review_reasons: Array<string>;
  work_item_id: string | null;
  acceptance_criteria: Array<AcceptanceCriterion>;
  exported_handoffs: Array<HandoffContract>;
  required_handoffs: Array<HandoffContract>;
  priority: number;
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
  control_activity: ControlActivity | null;
}

export interface GraphView {
  project_id: string;
  nodes: Array<GraphNodeView>;
  edges: Array<Edge>;
  flow_edges: Array<FlowEdge>;
  artifacts: Array<Artifact>;
  triggers: Array<Trigger>;
  work_items: Array<WorkItem>;
  handoffs: Array<Handoff>;
  budget_requests: Array<BudgetRequest>;
}

export interface Handoff {
  id: string;
  project_id: string;
  producer_node_id: string;
  consumer_node_id: string;
  contract: HandoffContract;
  artifact_id: string | null;
  status: HandoffStatus;
  evidence_refs: Array<string>;
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface HandoffContract {
  name: string;
  schema_name: string;
  version: string;
  required: boolean;
  evidence_required: boolean;
}

export type HandoffStatus = "EXPECTED" | "AVAILABLE" | "ACCEPTED" | "REJECTED";

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

export type ManagerDecision = "ACCEPT" | "CONTINUE" | "BLOCK";

export type ManagerPhase = "PLANNING" | "EXECUTING" | "REVIEW_PENDING" | "REVIEWING" | "ACCEPTED" | "BLOCKED";

export interface ManagerResult {
  decision: ManagerDecision;
  summary: string;
  plan: PlanResult | null;
  work_items: Array<WorkItemSpec>;
  missing_inputs: Array<InputSpec>;
}

export interface Node {
  id: string;
  project_id: string;
  parent_id: string | null;
  objective: string;
  project_name: string | null;
  generated_prompt: string | null;
  repo_path: string | null;
  workspace_path: string | null;
  workspace_commit: string | null;
  workspace: WorkspaceRef | null;
  output_branch: string | null;
  executor: string | null;
  agent: Agent | null;
  verification: VerificationResult | null;
  trigger_context: TriggerContext | null;
  status: NodeStatus;
  paused: boolean;
  auto_run: boolean;
  run_policy: RunPolicy | null;
  organization_contract: OrganizationContract | null;
  organization_review: OrganizationReview | null;
  manager_phase: ManagerPhase | null;
  manager_iteration: number;
  manager_review_reasons: Array<string>;
  work_item_id: string | null;
  acceptance_criteria: Array<AcceptanceCriterion>;
  exported_handoffs: Array<HandoffContract>;
  required_handoffs: Array<HandoffContract>;
  priority: number;
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
  organization_contract: OrganizationContract | null;
  exported_handoffs: Array<HandoffContract>;
  required_handoffs: Array<HandoffContract>;
  acceptance_criteria: Array<AcceptanceCriterion>;
  priority: number;
  subgraph_refs: Array<SubgraphRef>;
  artifacts: Array<ArtifactSpec>;
  capabilities: Array<string>;
  parent_key: string | null;
  follows: Array<string>;
  plan: boolean;
}

export type NodeStatus = "PENDING" | "BLOCKED" | "RUNNABLE" | "RUNNING" | "EXPANDED" | "COMPLETE" | "FAILED" | "CANCELLED";

export type NodeUIState = "queued" | "ready" | "running" | "preparing" | "paused" | "waiting_input" | "correction_required" | "waiting_sequence" | "complete" | "container" | "failed" | "cancelled";

export interface OrganizationBudget {
  max_active_workers: number | null;
  max_tokens: number | null;
  max_total_runs: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  max_cost_usd: number | null;
  max_wall_time_seconds: number | null;
}

export interface OrganizationContract {
  charter: string;
  scale: OrganizationScale;
  deliverables: Array<string>;
  acceptance_criteria: Array<AcceptanceCriterion>;
  constraints: Array<string>;
  quality_policy: Array<string>;
  decomposition_policy: string;
  completion_policy: string;
  budget: OrganizationBudget;
  min_first_level_production_owners: number;
  require_independent_verification: boolean;
  max_replans: number;
}

export interface OrganizationMetrics {
  boundary_count: number;
  planner_count: number;
  max_depth: number;
  production_leaf_count: number;
  planner_to_leaf_ratio: number;
  max_ownership_compression: number;
  average_ownership_compression: number;
  converged_boundary_count: number;
  verified_boundary_count: number;
  orphan_production_branches: number;
  fanout_boundary_count: number;
  convergence_boundary_count: number;
  fanout_to_fanin_ratio: number;
  replan_count: number;
  work_item_count: number;
  completed_work_item_count: number;
  handoff_count: number;
  accepted_handoff_count: number;
  budget_spent_usd: number;
  manager_iteration_count: number;
  manager_accept_count: number;
  manager_continue_count: number;
  manager_block_count: number;
  verifier_rejection_count: number;
  open_work_item_count: number;
  active_work_item_count: number;
  peak_concurrency: number;
}

export type OrganizationPhase = "PLAN" | "EXECUTE_FRONTIER" | "OBSERVE" | "REVIEW" | "REPLAN" | "ACCEPT_CHARTER" | "BLOCKED";

export interface OrganizationReview {
  phase: OrganizationPhase;
  revision: number;
  last_reason: string | null;
  audit: PlanAudit | null;
  reviewed_at: string | null;
  replan_requested: boolean;
  review_count: number;
  accept_count: number;
  continue_count: number;
  block_count: number;
  last_decision: ManagerDecision | null;
  audit_decision: PlanAuditDecision | null;
  audit_summary: string | null;
  audit_findings: Array<string>;
  audit_required_changes: Array<string>;
  audit_correction_count: number;
  audit_updated_at: string | null;
}

export type OrganizationScale = "focused" | "delivery" | "organization";

export type Outcome = "COMPLETE" | "EXPAND" | "BLOCK" | "FAIL";

export interface PlanAudit {
  accepted: boolean;
  score: number;
  errors: Array<string>;
  warnings: Array<string>;
  direct_node_count: number;
  planner_count: number;
  integrator_count: number;
  verifier_count: number;
  production_owner_count: number;
  has_convergence: boolean;
  has_independent_verification: boolean;
  ownership_compression: number;
  audited_at: string;
}

export type PlanAuditDecision = "APPROVE" | "REJECT";

export interface PlanAuditResult {
  decision: PlanAuditDecision;
  summary: string;
  findings: Array<string>;
  required_changes: Array<string>;
}

export interface PlanResult {
  nodes: Array<NodeSpec>;
  project_name: string | null;
  document_refs: Array<DocumentRef>;
  subgraph_refs: Array<SubgraphRef>;
  artifacts: Array<ArtifactSpec>;
  edges: Array<EdgeSpec>;
  triggers: Array<TriggerSpec>;
  notes: string | null;
  organization_contract: OrganizationContract | null;
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
  behavior_expectations: BehaviorExpectations | null;
  max_parallel_agents: number;
  max_total_runs: number | null;
  max_input_tokens: number | null;
  max_output_tokens: number | null;
  max_cost_usd: number | null;
  max_wall_time_seconds: number | null;
  workspace_isolation: WorkspaceIsolation;
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
  evidence: Array<AcceptanceEvidence>;
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

export interface WorkItem {
  id: string;
  project_id: string;
  organization_id: string;
  node_id: string | null;
  key: string;
  agent_type: string;
  organization_contract: OrganizationContract | null;
  title: string;
  objective: string;
  acceptance_criteria: Array<AcceptanceCriterion>;
  priority: number;
  status: WorkItemStatus;
  depends_on: Array<string>;
  artifact_refs: Array<string>;
  evidence_refs: Array<string>;
  claimed_by: string | null;
  rejection_reason: string | null;
  budget_request_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface WorkItemSpec {
  key: string;
  title: string;
  instructions: string;
  acceptance_criteria: Array<AcceptanceCriterion>;
  agent_type: string;
  depends_on: Array<string>;
  priority: number;
  organization_contract: OrganizationContract | null;
}

export type WorkItemStatus = "BACKLOG" | "BACKLOG" | "ACTIVE" | "READY" | "CLAIMED" | "RUNNING" | "BLOCKED" | "COMPLETE" | "COMPLETE" | "REJECTED" | "CANCELLED";

export interface WorkerResult {
  outcome: Outcome;
  summary: string;
  artifacts: Array<ArtifactSpec>;
  evidence: Array<AcceptanceEvidence>;
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

export type WorkspaceIsolation = "shared" | "worktree";

export interface WorkspaceRef {
  path: string;
  branch: string;
}
