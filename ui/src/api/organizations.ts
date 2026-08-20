import type { OrganizationMetrics } from "../generated/domain";
import { api } from "../api";

export interface OrganizationDashboardResponse {
  project_id: string;
  organizations: Array<Record<string, unknown>>;
  budget_requests: Array<Record<string, unknown>>;
  metrics: OrganizationMetrics;
}

export function getProjectOrganizations(
  projectId: string,
): Promise<OrganizationDashboardResponse> {
  return api<OrganizationDashboardResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/organizations`,
  );
}
