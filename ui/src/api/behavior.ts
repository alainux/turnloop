import { api } from "../api";

export interface BehaviorMetrics {
  node_id: string | null;
  role: string | null;
  harness: string | null;
  model: string | null;
  first_observed_at: string | null;
  last_observed_at: string | null;
  duration_seconds: number;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  actions: number;
  errors: number;
  retries: number;
  failed_commands: number;
  repeated_failed_actions: number;
  files_read: number;
  files_written: number;
  docs_accessed: number;
  skills_accessed: number;
  mcp_calls: number;
  web_searches: number;
  verification_commands: number;
  graph_changes: number;
  harness_failures: number;
  recovery_actions: number;
  docs_before_action_runs: number;
  docs_before_action_successes: number;
  verification_after_change_runs: number;
  verification_after_change_successes: number;
  dynamic_usage: Record<string, number>;
  role_metrics: Record<string, number>;
  qualitative_assessments: Array<Record<string, unknown>>;
}

export interface BehaviorRunMetrics extends BehaviorMetrics {
  run_id: string;
  attempt: number | null;
  status: string | null;
  outcome: string | null;
  started_at: string | null;
  ended_at: string | null;
}

export interface ProjectBehaviorResponse {
  project: BehaviorMetrics;
  by_node: Record<string, BehaviorMetrics>;
  by_run: Record<string, BehaviorRunMetrics>;
  expectations: Record<string, boolean | null>;
}

export interface BehaviorDashboardResponse {
  projects: Array<{
    project_id: string;
    project_name: string;
    metrics: BehaviorMetrics;
    nodes: Array<BehaviorMetrics & { node_id: string }>;
  }>;
}

export function getProjectBehavior(projectId: string): Promise<ProjectBehaviorResponse> {
  return api(`/api/projects/${encodeURIComponent(projectId)}/behavior`);
}

export function getBehaviorDashboard(filters: Record<string, string>): Promise<BehaviorDashboardResponse> {
  const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
  return api(`/api/behavior${query.size ? `?${query}` : ""}`);
}
