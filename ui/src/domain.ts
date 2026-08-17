import type {
  Agent,
  AgentType,
  Artifact,
  DocumentRef,
  Edge,
  FlowEdge,
  GraphNodeView,
  GraphView,
  HarnessKind,
  MCPServerAccess,
  Node,
  NodeUIState,
  ReasoningLevel,
  Run,
  RunPolicy,
  Usage,
} from "./generated/domain";

export type {
  Agent,
  AgentType,
  Artifact,
  DocumentRef,
  Edge,
  FlowEdge,
  GraphNodeView,
  GraphView,
  HarnessKind,
  MCPServerAccess,
  InputSpec,
  Node,
  NodeAction,
  NodeStatus,
  NodeUIState,
  ReasoningLevel,
  Run,
  RunPolicy,
  Usage,
} from "./generated/domain";

/** UI aliases are generated contract types, kept short at call sites. */
export type GraphNode = GraphNodeView;
export type Graph = GraphView;
export type Project = Node;
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

/** Keep skill references readable without hiding their source in a tooltip. */
export function skillReferenceLabel(reference: string): string {
  if (!/^https?:\/\//i.test(reference)) return reference;
  const path = reference.split(/[?#]/, 1)[0];
  const segments = path.split("/").filter(Boolean);
  const segment = segments.pop();
  const displaySegment = segment?.toLowerCase() === "skill.md"
    ? segments.pop()
    : segment;
  return displaySegment ? decodeURIComponent(displaySegment) : reference;
}

export function skillTooltip(references: string[]): string {
  return `Skills (${references.length})\n${references
    .map(skillReferenceLabel)
    .join("\n")}`;
}

/** Resolve skill references to an inspectable source.
 *
 * Built-in Turn skills are served from their actual local files. Only a
 * planner-selected HTTP(S) skill reference points outside the application.
 */
export function skillSourceHref(reference: string): string | null {
  if (/^https?:\/\//i.test(reference)) return reference;
  if (
    reference === "imagegen" ||
    reference === "find-skills" ||
    reference === "find-mcps" ||
    reference.startsWith("turn-")
  ) {
    return `/api/skills/${encodeURIComponent(reference)}`;
  }
  return null;
}

/** Keep MCP references readable while preserving their researched source. */
export function mcpServerLabel(server: MCPServerAccess): string {
  return server.name;
}

export function mcpTooltip(servers: MCPServerAccess[]): string {
  return `MCP servers (${servers.length})\n${servers
    .map(mcpServerLabel)
    .join("\n")}`;
}

/** Render a document reference as a project-scoped link without reading it. */
export function documentReferenceHref(reference: DocumentRef, projectId: string): string {
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
    (usage?.cached_input_tokens ?? 0) +
    (usage?.output_tokens ?? 0)
  );
}
