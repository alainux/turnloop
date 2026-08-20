import type {
  Agent,
  AgentType,
  Artifact,
  CapabilityStatus,
  DocumentRef,
  Edge,
  FlowEdge,
  GraphNodeView,
  GraphView,
  HarnessKind,
  Node,
  NodeUIState,
  ReasoningLevel,
  Run,
  RunPolicy,
  SubgraphRef,
  Trigger,
  TriggerContext,
  TriggerKind,
  Usage,
} from "./generated/domain";

export type {
  Agent,
  AgentType,
  Artifact,
  CapabilityStatus,
  DocumentRef,
  Edge,
  FlowEdge,
  GraphNodeView,
  GraphView,
  HarnessKind,
  InputSpec,
  Node,
  NodeAction,
  NodeStatus,
  NodeUIState,
  ReasoningLevel,
  Run,
  RunPolicy,
  SubgraphRef,
  Usage,
  Trigger,
  TriggerContext,
  TriggerKind,
} from "./generated/domain";

/** UI aliases are generated contract types, kept short at call sites. */
export type GraphNode = Omit<GraphNodeView, "subgraph_refs" | "trigger_context"> & {
  subgraph_refs?: SubgraphRef[];
  trigger_context?: TriggerContext | null;
};
export type Graph = GraphView;
export type Project = Omit<Node, "subgraph_refs" | "trigger_context"> & {
  subgraph_refs?: SubgraphRef[];
  trigger_context?: TriggerContext | null;
};
export type HarnessId = HarnessKind;
export type Reasoning = ReasoningLevel;
export type UIState = NodeUIState;

/** Remove lightweight Markdown syntax from labels while preserving intent. */
export function stripMarkdown(value: string): string {
  return value
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`{1,3}([^`]+)`{1,3}/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*[-+*]\s+/gm, "")
    .replace(/(?:\*\*|__|~~|\*|_)/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

/** The project tile follows an explicit name, then the scoped graph title. */
export function displayProjectTitle(node: Project): string {
  return stripMarkdown(
    node.project_name?.trim() ||
      node.objective,
  );
}

/** Node cards never expose Markdown punctuation in their title label. */
export function displayNodeTitle(node: Project): string {
  return stripMarkdown(node.objective);
}

export function capabilityCatalogHref(capabilityId: string): string {
  return `/api/capability-catalog/${encodeURIComponent(capabilityId)}`;
}

export function capabilityTooltip(capabilities: CapabilityStatus[]): string {
  return capabilities
    .map((item) => `${item.capability_id} · ${item.skills} skills · ${item.mcps} MCP`)
    .join("\n");
}

export function capabilityDeploymentLabel(item: CapabilityStatus): string {
  if (!item.loaded) return "not loaded";
  return item.installed ? "installed" : "loaded";
}

/** Render a document reference as a project-scoped link without reading it. */
export function documentReferenceHref(reference: { ref: string }, projectId: string): string {
  if (/^https?:\/\//i.test(reference.ref)) return reference.ref;
  const match = /^([^?#]*)([?#].*)?$/.exec(reference.ref);
  const path = match?.[1] ?? reference.ref;
  const suffix = match?.[2] ?? "";
  const encoded = path.split("/").map((part) => encodeURIComponent(part)).join("/");
  return `/api/projects/${encodeURIComponent(projectId)}/documents/${encoded}${suffix}`;
}

export function isExternalDocumentReference(reference: DocumentRef): boolean {
  return /^https?:\/\//i.test(reference.ref);
}

export function documentReferenceContentHref(reference: DocumentRef, projectId: string): string {
  return documentReferenceHref(reference, projectId).split(/[?#]/, 1)[0];
}

export function documentReferenceLabel(reference: DocumentRef): string {
  return reference.title?.trim() || reference.ref;
}

/** Render a composed graph source as a project-scoped link without ingesting it. */
export function subgraphReferenceHref(reference: SubgraphRef, projectId: string): string {
  return documentReferenceHref(reference, projectId);
}

export function subgraphReferenceLabel(reference: SubgraphRef): string {
  return reference.title?.trim() || reference.ref;
}

/** Show local absolute paths with a portable home-directory prefix. */
export function displayPath(path: string | null | undefined): string {
  if (!path) return "Current directory";
  const normalized = path.replaceAll("\\", "/");
  const home = normalized.match(/^(?:\/Users|\/home)\/[^/]+(\/.*)?$/);
  if (home) return `~${home[1] ?? ""}`;
  const windowsHome = normalized.match(/^[A-Za-z]:\/Users\/[^/]+(\/.*)?$/i);
  return windowsHome ? `~${windowsHome[1] ?? ""}` : normalized;
}

export type PrimaryNodeAction = "run" | "retry" | "regenerate" | "cancel";

/**
 * Select the server-authorized primary action for presentation.
 *
 * The browser does not derive workflow policy from status or agent type. The
 * API's allowed_actions projection is the only action authority; this helper
 * merely applies a stable presentation priority.
 */
export function primaryNodeAction(node: GraphNode): PrimaryNodeAction | null {
  const priority: PrimaryNodeAction[] = ["cancel", "run", "retry", "regenerate"];
  return priority.find((action) => node.allowed_actions.includes(action)) ?? null;
}

export function primaryNodeActionLabel(action: PrimaryNodeAction, freshRun = false): string {
  if (action === "cancel") return "Stop";
  if (freshRun || action === "retry" || action === "regenerate") return "Run again";
  return "Run";
}

export function primaryNodeActionIcon(
  action: PrimaryNodeAction,
  freshRun = false,
): string {
  if (action === "cancel") return "stop";
  if (freshRun || action === "retry" || action === "regenerate") return "rotate-cw";
  return "play";
}

export interface NodeDetail {
  node: GraphNode;
  runs: Run[];
  artifacts: Artifact[];
}

export interface ModelCapability {
  id: string;
  label: string;
  reasoning?: Reasoning[];
  source?: string;
}

export interface HarnessCapability {
  id: HarnessId;
  label: string;
  available: boolean;
  capabilities: string[];
  models: ModelCapability[];
  reasoning: Reasoning[];
  accepts_custom_models: boolean;
}

export interface Capabilities {
  harnesses: HarnessCapability[];
}

export interface ProjectsResponse {
  projects: Project[];
}

export interface UsageResponse {
  totals: Usage;
  by_node: Record<string, Usage>;
  by_branch: Record<string, Usage>;
}

/** Keep run history focused on the human-facing summary, not the protocol envelope. */
export function cleanRunSummary(summary?: string | null): string {
  if (!summary) return "";
  const value = summary.trim();
  const marker = /```turn-result\s*([\s\S]*?)```/i.exec(value);
  if (!marker) return value;

  const prefix = value.slice(0, marker.index).trim();
  const payload = JSON.parse(marker[1].trim()) as { summary?: unknown };
  if (prefix) return prefix;
  if (typeof payload.summary !== "string" || !payload.summary.trim()) {
    throw new Error("run result envelope has no summary");
  }
  return payload.summary.trim();
}

export function isGraph(value: unknown): value is Graph {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.project_id === "string" &&
    Array.isArray(record.nodes) &&
    Array.isArray(record.edges)
  );
}

export function tokens(usage?: Usage): number {
  return (
    (usage?.input_tokens ?? 0) +
    (usage?.output_tokens ?? 0)
  );
}
