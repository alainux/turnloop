import type {
  Agent,
  Graph,
  ProjectsResponse,
  RunPolicy,
  UsageResponse,
} from "../domain";
import { api, json } from "../api";
import { isGraph } from "../domain";

export interface CreateProjectInput {
  prompt: string;
  name: string | null;
  working_dir: string | null;
  mode: "create";
  agent: Agent;
  run_policy: RunPolicy;
  attachments: Array<{
    name: string;
    mime: string;
    content_base64: string;
  }>;
}

export interface CreateProjectResponse {
  project_id: string;
}

export interface DeleteProjectOptions {
  delete_files: boolean;
  delete_conversations: boolean;
}

export async function listProjects(): Promise<ProjectsResponse> {
  return api<ProjectsResponse>("/api/projects");
}

export async function getProjectGraph(projectId: string): Promise<Graph> {
  const result = await api<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/graph`,
  );
  if (!isGraph(result)) throw new Error("Server returned an invalid graph schema");
  return result;
}

export async function getProjectUsage(projectId: string): Promise<UsageResponse> {
  return api<UsageResponse>(`/api/projects/${encodeURIComponent(projectId)}/usage`);
}

export async function createProject(
  input: CreateProjectInput,
): Promise<CreateProjectResponse> {
  return api<CreateProjectResponse>("/api/projects", json("POST", input));
}

export async function renameProject(projectId: string, name: string): Promise<void> {
  await api(`/api/projects/${encodeURIComponent(projectId)}`, json("PATCH", { name }));
}

export async function deleteProject(
  projectId: string,
  options: DeleteProjectOptions,
): Promise<void> {
  await api(
    `/api/projects/${encodeURIComponent(projectId)}`,
    json("DELETE", options),
  );
}

export async function stepProject(projectId: string): Promise<void> {
  await api(`/api/projects/${encodeURIComponent(projectId)}/step`, {
    method: "POST",
  });
}

export async function setProjectMode(
  projectId: string,
  autoRun: boolean,
): Promise<void> {
  await api(
    `/api/projects/${encodeURIComponent(projectId)}/mode`,
    json("POST", { auto_run: autoRun }),
  );
}

export async function setProjectPolicy(
  projectId: string,
  runPolicy: RunPolicy,
): Promise<void> {
  await api(
    `/api/projects/${encodeURIComponent(projectId)}/policy`,
    json("POST", { run_policy: runPolicy }),
  );
}
