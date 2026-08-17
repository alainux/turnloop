import type { Capabilities } from "../domain";
import { api, json } from "../api";

export type WorkspaceSettings = Record<string, unknown>;

export async function getCapabilities(): Promise<Capabilities> {
  return api<Capabilities>("/api/capabilities");
}

export async function getSettings(): Promise<WorkspaceSettings> {
  return api<WorkspaceSettings>("/api/settings");
}

export async function saveSettings(settings: WorkspaceSettings): Promise<void> {
  await api("/api/settings", json("POST", settings));
}

export async function chooseDirectory(): Promise<{ path: string | null }> {
  return api<{ path: string | null }>("/api/system/pick-directory", {
    method: "POST",
  });
}
