import type { LeadTranscriptEntry, ProjectLead } from "../domain";
import { api, json } from "../api";

export interface LeadChatResponse {
  project_id: string;
  message: LeadTranscriptEntry;
  queued?: boolean;
  lead: ProjectLead | null;
}

export async function getLeadChat(projectId: string): Promise<LeadTranscriptEntry[]> {
  const result = await api<{ transcript: LeadTranscriptEntry[] }>(
    `/api/projects/${encodeURIComponent(projectId)}/lead/chat`,
  );
  return result.transcript;
}

export async function sendLeadMessage(
  projectId: string,
  message: string,
): Promise<LeadChatResponse> {
  return api<LeadChatResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/lead/chat`,
    json("POST", { message }),
  );
}

export async function waitLead(
  projectId: string,
  events: string[] = [],
): Promise<ProjectLead> {
  const result = await api<{ lead: ProjectLead }>(
    `/api/projects/${encodeURIComponent(projectId)}/lead/wait`,
    json("POST", { events }),
  );
  return result.lead;
}

export async function cancelLead(projectId: string): Promise<ProjectLead | null> {
  const result = await api<{ lead: ProjectLead | null }>(
    `/api/projects/${encodeURIComponent(projectId)}/lead/cancel`,
    json("POST", {}),
  );
  return result.lead;
}
