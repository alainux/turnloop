import type { Trigger } from "../domain";
import { api, json } from "../api";

export async function updateTrigger(
  triggerId: string,
  body: Partial<Pick<Trigger, "event_name" | "kind" | "schedule" | "data" | "enabled">>,
): Promise<Trigger> {
  const response = await api<{ trigger: Trigger }>(
    `/api/triggers/${encodeURIComponent(triggerId)}`,
    json("PATCH", body),
  );
  return response.trigger;
}

export async function deleteTrigger(triggerId: string): Promise<void> {
  await api(`/api/triggers/${encodeURIComponent(triggerId)}`, { method: "DELETE" });
}
