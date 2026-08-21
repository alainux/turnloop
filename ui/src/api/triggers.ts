import type { Trigger } from "../domain";
import { api, json } from "../api";

export async function emitTrigger(trigger: Trigger): Promise<void> {
  if (trigger.kind !== "event" || !trigger.event_name) {
    throw new Error("Only event triggers can be emitted manually");
  }
  await api("/api/events", json("POST", {
    event_name: trigger.event_name,
    data: trigger.data,
    project_id: trigger.project_id,
    node_id: trigger.target_node_id,
  }));
}

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
