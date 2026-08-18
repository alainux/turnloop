import { api } from "../api";

export type LogRecord = {
  timestamp: string;
  event_id: string;
  project_id: string | null;
  source: string;
  kind: string;
  status: string;
  message: string;
  action?: string;
  data?: unknown;
};

export type ProjectLogsResponse = {
  project_id: string;
  records: LogRecord[];
  max_records: number;
};

export async function getProjectLogs(projectId: string, search = ""): Promise<ProjectLogsResponse> {
  const query = search ? `?search=${encodeURIComponent(search)}` : "";
  return api<ProjectLogsResponse>(`/api/projects/${encodeURIComponent(projectId)}/logs${query}`);
}
