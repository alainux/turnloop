import type { Agent, NodeAction, NodeDetail } from "../domain";
import { api, json } from "../api";

export async function getNodeDetail(nodeId: string): Promise<NodeDetail> {
  return api<NodeDetail>(`/api/nodes/${encodeURIComponent(nodeId)}`);
}

export async function runNodeAction(
  nodeId: string,
  action: NodeAction,
): Promise<void> {
  await api(
    `/api/nodes/${encodeURIComponent(nodeId)}/${action}`,
    { method: "POST" },
  );
}

export async function editNode(
  nodeId: string,
  body: { objective?: string; generated_prompt?: string | null; agent?: Agent },
): Promise<void> {
  await api(`/api/nodes/${encodeURIComponent(nodeId)}/edit`, json("POST", body));
}

export async function provideNodeInput(
  nodeId: string,
  inputId: string,
  value: string,
): Promise<void> {
  await api(
    `/api/nodes/${encodeURIComponent(nodeId)}/provide-input`,
    json("POST", { input_id: inputId, value }),
  );
}
