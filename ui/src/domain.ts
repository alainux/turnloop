import type {
  Agent,
  AgentType,
  ArchitectureDecision,
  ArchitectureDiagram,
  ArchitectureDiagramEdge,
  ArchitectureDiagramNode,
  ArchitectureRisk,
  ArchitectureSection,
  ArchitectureSpec,
  Artifact,
  Edge,
  GraphNodeView,
  GraphView,
  HarnessKind,
  Node,
  NodeUIState,
  PermissionMode,
  ReasoningLevel,
  Run,
  RunPolicy,
  Usage,
} from "./generated/domain";

export type {
  Agent,
  AgentType,
  ArchitectureDecision,
  ArchitectureDiagram,
  ArchitectureDiagramEdge,
  ArchitectureDiagramNode,
  ArchitectureRisk,
  ArchitectureSection,
  ArchitectureSpec,
  Artifact,
  Edge,
  GraphNodeView,
  GraphView,
  HarnessKind,
  InputSpec,
  Node,
  NodeAction,
  NodeStatus,
  NodeUIState,
  PermissionMode,
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
export type Permission = PermissionMode;
export type Reasoning = ReasoningLevel;
export type UIState = NodeUIState;

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

export function primaryNodeActionLabel(action: PrimaryNodeAction): string {
  if (action === "cancel") return "Stop";
  if (action === "retry" || action === "regenerate") return "Run again";
  return "Run";
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
