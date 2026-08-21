import { api, json } from "../api";

export interface LeadMessageResponse {
  ok: boolean;
  reply: string;
}

export async function sendLeadMessage(
  projectId: string,
  message: string,
): Promise<LeadMessageResponse> {
  return api<LeadMessageResponse>(
    `/api/projects/${encodeURIComponent(projectId)}/lead/message`,
    json("POST", { message }),
  );
}
