import { useEffect, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  Artifact,
  DocumentRef,
  Edge,
  GraphNode,
  SubgraphRef,
} from "../domain";
import {
  displayNodeTitle,
  documentReferenceContentHref,
  documentReferenceHref,
  documentReferenceLabel,
  subgraphReferenceHref,
  subgraphReferenceLabel,
  isExternalDocumentReference,
  capabilityCatalogHref,
  capabilityDeploymentLabel,
  stripMarkdown,
} from "../domain";
import { Icon } from "./Icon";

interface Props {
  nodes: GraphNode[];
  edges: Edge[];
  artifacts: Artifact[];
  projectId?: string;
  refreshKey?: string | number;
}

export type DocumentTarget = DocumentRef & {
  kind?: "document" | "graph";
};

type GraphSourceArtifact = {
  kind: string;
  name: string;
  ref: string | null;
  content: unknown | null;
};

export interface GraphSourceNode {
  key: string;
  objective: string;
  generated_prompt: string | null;
  executor: string | null;
  agent_type: string | null;
  capabilities: string[];
  document_refs: DocumentRef[];
  subgraph_refs: SubgraphRef[];
  artifacts: GraphSourceArtifact[];
  parent_key: string | null;
  follows: string[];
  plan: boolean;
}

export interface GraphSource {
  project_name: string | null;
  notes: string | null;
  nodes: GraphSourceNode[];
  document_refs: DocumentRef[];
  subgraph_refs: SubgraphRef[];
  artifacts: GraphSourceArtifact[];
  edges: Array<{ type: string; src: string; dst: string }>;
}

type ArtifactLike = {
  kind: string;
  name: string;
  ref: string | null;
  content?: unknown | null;
  created_at?: string;
};

function objectRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function parseDocumentReferences(value: unknown): DocumentRef[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === "string" && item.trim()) {
      return [{ ref: item, title: null, media_type: null, imports: [] }];
    }
    const record = objectRecord(item);
    const ref = stringValue(record?.ref);
    if (!ref) return [];
    return [{
      ref,
      title: stringValue(record?.title),
      media_type: stringValue(record?.media_type),
      imports: parseDocumentReferences(record?.imports),
    }];
  });
}

function parseSubgraphReferences(value: unknown): SubgraphRef[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === "string" && item.trim()) {
      return [{ ref: item, title: null, media_type: "application/json", managed: false }];
    }
    const record = objectRecord(item);
    const ref = stringValue(record?.ref);
    if (!ref) return [];
    return [{
      ref,
      title: stringValue(record?.title),
      media_type: stringValue(record?.media_type) ?? "application/json",
      managed: record?.managed === true,
    }];
  });
}

function parseGraphArtifacts(value: unknown): GraphSourceArtifact[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === "string" && item.trim()) {
      return [{
        kind: "file",
        name: item.split("/").pop() || item,
        ref: item,
        content: null,
      }];
    }
    const record = objectRecord(item);
    const name = stringValue(record?.name) ?? stringValue(record?.ref);
    if (!name) return [];
    return [{
      kind: stringValue(record?.kind) ?? "file",
      name,
      ref: stringValue(record?.ref),
      content: record?.content ?? null,
    }];
  });
}

function sourceNode(value: unknown, index: number): GraphSourceNode {
  const record = objectRecord(value);
  const key = stringValue(record?.key) ?? `node-${index + 1}`;
  return {
    key,
    objective: stringValue(record?.objective) ?? key,
    generated_prompt: stringValue(record?.generated_prompt),
    executor: stringValue(record?.executor),
    agent_type: stringValue(record?.agent_type),
    capabilities: Array.isArray(record?.capabilities)
      ? record.capabilities.filter((item): item is string => typeof item === "string")
      : [],
    document_refs: parseDocumentReferences(record?.document_refs),
    subgraph_refs: parseSubgraphReferences(record?.subgraph_refs),
    artifacts: parseGraphArtifacts(record?.artifacts),
    parent_key: stringValue(record?.parent_key),
    follows: Array.isArray(record?.follows)
      ? record.follows.filter((item): item is string => typeof item === "string")
      : [],
    plan: record?.plan === true,
  };
}

export function parseGraphSource(value: unknown): GraphSource {
  const record = objectRecord(value);
  if (!record || !Array.isArray(record.nodes)) {
    throw new Error("graph source must contain a nodes array");
  }
  const edges = Array.isArray(record.edges)
    ? record.edges.flatMap((item) => {
        const edge = objectRecord(item);
        const type = stringValue(edge?.type);
        const src = stringValue(edge?.src);
        const dst = stringValue(edge?.dst);
        return type && src && dst ? [{ type, src, dst }] : [];
      })
    : [];
  return {
    project_name: stringValue(record.project_name),
    notes: stringValue(record.notes),
    nodes: record.nodes.map(sourceNode),
    document_refs: parseDocumentReferences(record.document_refs),
    subgraph_refs: parseSubgraphReferences(record.subgraph_refs),
    artifacts: parseGraphArtifacts(record.artifacts),
    edges,
  };
}

export function orderGraphSourceNodes(
  nodes: GraphSourceNode[],
  edges: GraphSource["edges"],
  parentKey: string | null,
): GraphSourceNode[] {
  const parentOf = new Map(nodes.map((node) => [node.key, node.parent_key]));
  const siblings = nodes.filter((node) => (node.parent_key ?? null) === parentKey);
  const siblingIds = new Set(siblings.map((node) => node.key));
  const position = new Map(siblings.map((node, index) => [node.key, index]));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map(siblings.map((node) => [node.key, 0]));
  const predecessors = new Map<string, string[]>();
  for (const node of nodes) predecessors.set(node.key, [...node.follows]);
  for (const edge of edges) {
    if (edge.type !== "FOLLOWS") continue;
    predecessors.set(edge.dst, [...(predecessors.get(edge.dst) ?? []), edge.src]);
  }
  const directChild = (key: string): string | null => {
    let current = key;
    const visited = new Set<string>();
    while (parentOf.has(current) && parentOf.get(current) !== parentKey) {
      if (visited.has(current)) return null;
      visited.add(current);
      const parent = parentOf.get(current);
      if (!parent) return null;
      current = parent;
    }
    return siblingIds.has(current) ? current : null;
  };
  for (const [successor, predecessorsForNode] of predecessors) {
    const target = directChild(successor);
    if (!target) continue;
    for (const predecessor of predecessorsForNode) {
      const source = directChild(predecessor);
      if (!source || source === target) continue;
      if (!outgoing.get(source)?.includes(target)) {
        outgoing.set(source, [...(outgoing.get(source) ?? []), target]);
        indegree.set(target, (indegree.get(target) ?? 0) + 1);
      }
    }
  }
  const compare = (a: string, b: string) =>
    (position.get(a) ?? 0) - (position.get(b) ?? 0);
  const ready = siblings
    .filter((node) => indegree.get(node.key) === 0)
    .map((node) => node.key)
    .sort(compare);
  const ordered: string[] = [];
  while (ready.length) {
    const key = ready.shift()!;
    ordered.push(key);
    for (const dependent of outgoing.get(key) ?? []) {
      const next = (indegree.get(dependent) ?? 0) - 1;
      indegree.set(dependent, next);
      if (next === 0) ready.push(dependent);
    }
    ready.sort(compare);
  }
  const keys = ordered.length === siblings.length
    ? ordered
    : siblings.map((node) => node.key);
  const byKey = new Map(siblings.map((node) => [node.key, node]));
  return keys.map((key) => byKey.get(key)!).filter(Boolean);
}

/**
 * Order nodes within one composition section by their explicit sequence.
 * The document is a projection of the graph: it never invents workflow order
 * when the graph does not provide one.
 */
export function orderDocumentNodes(
  nodes: GraphNode[],
  edges: Edge[],
  parentId: string | null,
): GraphNode[] {
  const parentOf = documentParentMap(nodes, edges);
  const siblings = nodes.filter((node) => parentOf.get(node.id) === parentId);
  const siblingIds = new Set(siblings.map((node) => node.id));
  const position = new Map(siblings.map((node, index) => [node.id, index]));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map(siblings.map((node) => [node.id, 0]));
  for (const edge of edges) {
    if (edge.type !== "FOLLOWS") continue;

    // Sequence can be projected through a child boundary. Compare the direct
    // children of this document section so a reintegration remains after the
    // complete nested workflow.
    const directChild = (id: string): string | null => {
      let current = id;
      const visited = new Set<string>();
      while (parentOf.has(current) && parentOf.get(current) !== parentId) {
        if (visited.has(current)) return null;
        visited.add(current);
        const parent = parentOf.get(current);
        if (!parent) return null;
        current = parent;
      }
      return siblingIds.has(current) ? current : null;
    };
    const source = directChild(edge.src);
    const target = directChild(edge.dst);
    if (!source || !target || source === target) continue;
    outgoing.set(source, [...(outgoing.get(source) ?? []), target]);
    indegree.set(target, (indegree.get(target) ?? 0) + 1);
  }
  const compare = (a: string, b: string) =>
    (position.get(a) ?? 0) - (position.get(b) ?? 0);
  const ready = siblings
    .filter((node) => indegree.get(node.id) === 0)
    .map((node) => node.id)
    .sort(compare);
  const ordered: string[] = [];
  while (ready.length) {
    const id = ready.shift()!;
    ordered.push(id);
    for (const dependent of outgoing.get(id) ?? []) {
      const next = (indegree.get(dependent) ?? 0) - 1;
      indegree.set(dependent, next);
      if (next === 0) ready.push(dependent);
    }
    ready.sort(compare);
  }
  const ids = ordered.length === siblings.length ? ordered : siblings.map((node) => node.id);
  const byId = new Map(siblings.map((node) => [node.id, node]));
  return ids.map((id) => byId.get(id)!).filter(Boolean);
}

/**
 * Project verification into the document hierarchy without changing the
 * graph. A verifier is still an ordinary FOLLOWS node for scheduling; in
 * the read-only specification it is shown beneath the implementation it
 * inspects so the sequence is legible.
 */
export function documentParentMap(nodes: GraphNode[], edges: Edge[]): Map<string, string | null> {
  const parentOf = new Map<string, string | null>();
  for (const node of nodes) {
    parentOf.set(node.id, node.parent_id);
  }
  // A verifier is a normal sequence item in the graph, never a special graph
  // relation. The document projection nests it below the work item it
  // verifies so the specification reads as implementation → verification.
  // This changes presentation only; scheduling and graph edges stay intact.
  for (const node of nodes) {
    if (node.agent?.type_id !== "verifier") continue;
    const target = edges.find(
      (edge) => edge.type === "FOLLOWS" && edge.dst === node.id,
    )?.src;
    if (target && parentOf.has(target)) parentOf.set(node.id, target);
  }
  return parentOf;
}

function statusLabel(node: GraphNode): string {
  if (node.runtime_guard) {
    return `${node.runtime_guard.code} — ${node.runtime_guard.message}`;
  }
  if (node.status === "RUNNING" || node.generation_active) {
    return node.agent_message?.trim()
      ? `${node.agent_state ?? "working"} — ${node.agent_message.trim()}`
      : node.agent_state ?? "working";
  }
  return node.ui_state.replaceAll("_", " ");
}

function sequenceLabels(node: GraphNode, nodes: GraphNode[], edges: Edge[]): string[] {
  const names = new Map(nodes.map((item) => [item.id, displayNodeTitle(item)]));
  return edges
    .filter((edge) => edge.type === "FOLLOWS" && edge.dst === node.id)
    .map((edge) => names.get(edge.src))
    .filter((name): name is string => Boolean(name));
}

export function DocumentLinks({
  refs,
  projectId,
  onOpenDocument,
}: {
  refs: DocumentRef[];
  projectId?: string;
  onOpenDocument?: (reference: DocumentTarget) => void;
}) {
  if (!refs.length || !projectId) return null;
  const render = (reference: DocumentRef) => {
    const external = isExternalDocumentReference(reference);
    const href = documentReferenceHref(reference, projectId);
    return (
    <li key={`${reference.ref}:${reference.title ?? ""}`}>
      <a
        href={href}
        target={external ? "_blank" : undefined}
        rel={external ? "noreferrer" : undefined}
        onClick={external || !onOpenDocument ? undefined : (event) => {
          event.preventDefault();
          onOpenDocument(reference);
        }}
      >
        {documentReferenceLabel(reference)}
      </a>
      {reference.imports.length > 0 && (
        <ul>{reference.imports.map((child) => render(child))}</ul>
      )}
    </li>
    );
  };
  return (
    <section className="document-links" aria-label="Project documents">
      <h3>Project documents</h3>
      <ul>{refs.map((reference) => render(reference))}</ul>
    </section>
  );
}

export function SubgraphLinks({
  refs,
  projectId,
  onOpenDocument,
}: {
  refs: SubgraphRef[];
  projectId?: string;
  onOpenDocument?: (reference: DocumentTarget) => void;
}) {
  if (!refs.length || !projectId) return null;
  return (
    <section className="document-links document-subgraph-links" aria-label="Workflow source">
      <h3>Workflow source</h3>
      <ul>
        {refs.map((reference) => {
          const href = subgraphReferenceHref(reference, projectId);
          const documentReference: DocumentTarget = {
            ref: reference.ref,
            title: subgraphReferenceLabel(reference),
            media_type: reference.media_type,
            imports: [],
            kind: "graph",
          };
          return (
            <li className="document-reference-row document-subgraph-row" key={`${reference.ref}:${reference.title ?? ""}`}>
              <Icon name="workflow" className="document-reference-icon" />
              <span className="document-reference-copy">
                <span className="document-reference-main">
                  <a
                    href={href}
                    onClick={!onOpenDocument ? undefined : (event) => {
                      event.preventDefault();
                      onOpenDocument(documentReference);
                    }}
                  >
                    {subgraphReferenceLabel(reference)}
                  </a>
                  <small className="document-reference-kind">graph source</small>
                </span>
                <small className="document-reference-hint">Open work breakdown</small>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function WorkflowSourceLinks({
  refs,
  projectId,
  onOpenDocument,
}: {
  refs: SubgraphRef[];
  projectId?: string;
  onOpenDocument?: (reference: DocumentTarget) => void;
}) {
  if (!refs.length || !projectId) return null;
  return (
    <details className="document-advanced document-source-panel">
      <summary>
        <Icon name="workflow" className="document-reference-icon" />
        <span>Explore workflow source</span>
        <small>{refs.length} {refs.length === 1 ? "source" : "sources"}</small>
      </summary>
      <SubgraphLinks refs={refs} projectId={projectId} onOpenDocument={onOpenDocument} />
    </details>
  );
}

export function ArtifactLinks({
  artifacts,
  projectId,
  onOpenDocument,
}: {
  artifacts: ArtifactLike[];
  projectId?: string;
  onOpenDocument?: (reference: DocumentTarget) => void;
}) {
  const visibleArtifacts = artifacts.filter((artifact) => !isSubmissionArtifact(artifact));
  if (!visibleArtifacts.length || !projectId) return null;
  return (
    <section className="document-links document-artifact-links" aria-label="Generated files">
      <div className="document-section-heading">
        <h3>Generated files</h3>
        <small>{visibleArtifacts.length} {visibleArtifacts.length === 1 ? "file" : "files"}</small>
      </div>
      <ul className="document-artifact-grid">
        {visibleArtifacts.map((artifact, index) => {
          const reference = artifact.ref ? {
            ref: artifact.ref,
            title: artifact.name,
            media_type: null,
            imports: [],
            kind: isGraphSourceReference(artifact.ref)
              ? "graph" as const
              : "document" as const,
          } satisfies DocumentTarget : null;
          const label = artifact.name || artifact.ref || `artifact-${index + 1}`;
          const graphSource = Boolean(artifact.ref && isGraphSourceReference(artifact.ref));
          const documentArtifact = Boolean(artifact.ref && isDocumentArtifact(artifact.ref));
          const imageArtifact = Boolean(artifact.ref && isImageReference(artifact.ref));
          const inReader = graphSource || documentArtifact || imageArtifact;
          const external = reference ? isExternalDocumentReference(reference) : false;
          const pathLabel = artifact.ref ? artifactPathLabel(artifact.ref) : null;
          const iconName = graphSource
            ? "workflow"
            : imageArtifact
              ? "image"
              : artifact.kind === "file"
              ? "file"
              : "braces";
          return (
            <li
              className="document-artifact-card"
              data-artifact-ref={artifact.ref ?? undefined}
              key={`${artifact.name}:${artifact.ref ?? index}`}
            >
              {imageArtifact && reference ? (
                <details className="document-image-preview">
                  <summary aria-label={`Preview ${label}`} title={`Preview ${label}`}>
                    <img
                      className="document-artifact-thumbnail"
                      src={documentReferenceContentHref(reference, projectId)}
                      alt=""
                    />
                  </summary>
                  <img
                    className="document-artifact-image"
                    src={documentReferenceContentHref(reference, projectId)}
                    alt={label}
                  />
                </details>
              ) : (
                <div className="document-artifact-icon" aria-hidden="true">
                  <Icon name={iconName} className="document-reference-icon" />
                </div>
              )}
              <span className="document-artifact-card-copy">
                <span className="document-artifact-card-title">
                  {reference ? (
                    <a
                      href={documentReferenceHref(reference, projectId)}
                      target={external || !inReader ? "_blank" : undefined}
                      rel={external || !inReader ? "noreferrer" : undefined}
                      onClick={external || !inReader || !onOpenDocument ? undefined : (event) => {
                        event.preventDefault();
                        onOpenDocument(reference);
                      }}
                    >
                      {label}
                    </a>
                  ) : (
                    <span>{label}</span>
                  )}
                  <small className="document-reference-kind">{artifactKindLabel(artifact, imageArtifact)}</small>
                </span>
                {pathLabel && <small className="document-artifact-path" title={pathLabel}>{pathLabel}</small>}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function isSubmissionArtifact(artifact: ArtifactLike): boolean {
  return artifact.name.trim().toLowerCase().includes("submission");
}

function artifactKindLabel(artifact: ArtifactLike, image: boolean): string {
  if (image) return "image";
  if (artifact.kind === "json") return "JSON";
  if (artifact.kind === "file") return "file";
  return artifact.kind;
}

function artifactPathLabel(reference: string): string {
  const path = splitDocumentReference(reference).path;
  const parts = path.split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function submissionOutcomeLabel(artifact: ArtifactLike): string {
  const record = objectRecord(artifact.content);
  const outcome = stringValue(record?.outcome);
  if (outcome) return outcome.toLowerCase();
  if (artifact.name.toLowerCase().includes("plan")) return "ready";
  return "complete";
}

function submissionSummary(artifact: ArtifactLike): string {
  const record = objectRecord(artifact.content);
  const summary = stringValue(record?.summary);
  if (summary) return summary;
  const nodeCount = typeof record?.node_count === "number" ? record.node_count : null;
  if (nodeCount !== null) {
    return `Created a workflow with ${nodeCount} ${nodeCount === 1 ? "work item" : "work items"}.`;
  }
  return "This step completed successfully.";
}

function submissionDetails(artifact: ArtifactLike): string | null {
  const record = objectRecord(artifact.content);
  const details: string[] = [];
  const nodeCount = typeof record?.node_count === "number" ? record.node_count : null;
  const edgeCount = typeof record?.edge_count === "number" ? record.edge_count : null;
  const outputs = Array.isArray(record?.artifacts)
    ? record.artifacts.filter((item): item is string => typeof item === "string")
    : [];
  const missing = Array.isArray(record?.missing_inputs) ? record.missing_inputs.length : 0;
  if (nodeCount !== null) details.push(`${nodeCount} work items`);
  if (edgeCount !== null) details.push(`${edgeCount} workflow links`);
  if (outputs.length) details.push(`${outputs.length} output ${outputs.length === 1 ? "file" : "files"}`);
  if (missing) details.push(`${missing} missing input${missing === 1 ? "" : "s"}`);
  return details.length ? details.join(" · ") : null;
}

function submissionOutputPaths(artifact: ArtifactLike): string[] {
  const record = objectRecord(artifact.content);
  if (!Array.isArray(record?.artifacts)) return [];
  return record.artifacts
    .filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    .map(artifactPathLabel);
}

export function SubmissionSummary({ artifacts }: { artifacts: ArtifactLike[] }) {
  const submissions = artifacts.filter(isSubmissionArtifact);
  if (!submissions.length) return null;
  return (
    <section className="document-submission-summary" aria-label="Step result">
      <div className="document-section-heading">
        <h3>Result</h3>
        <small>{submissions.length === 1 ? "Latest handoff" : `${submissions.length} handoffs`}</small>
      </div>
      <ul className="document-submission-list">
        {submissions.map((artifact, index) => {
          const outcome = submissionOutcomeLabel(artifact);
          const detail = submissionDetails(artifact);
          const outputs = submissionOutputPaths(artifact);
          return (
            <li className="document-submission-card" key={`${artifact.name}:${artifact.created_at ?? index}`}>
              <div className="document-submission-heading">
                <span className={`document-result-badge result-${outcome}`}>{outcome}</span>
                <strong>{submissionSummary(artifact)}</strong>
              </div>
              {detail && <small>{detail}</small>}
              {outputs.length > 0 && (
                <div className="document-submission-outputs">
                  {outputs.map((output) => <span key={output}>{output}</span>)}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function markdownText(value: ReactNode): string {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(markdownText).join("");
  return "";
}

function headingId(value: string): string {
  return stripMarkdown(value)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") || "section";
}

function splitDocumentReference(value: string): { path: string; suffix: string } {
  const match = /^([^?#]*)([?#].*)?$/.exec(value);
  return { path: match?.[1] ?? value, suffix: match?.[2] ?? "" };
}

function isGraphSourceReference(value: string): boolean {
  const { path } = splitDocumentReference(value);
  return /(?:^|\/)\.turn\/graphs\/[^/]+\.json$/i.test(path);
}

function isDocumentArtifact(value: string): boolean {
  const { path } = splitDocumentReference(value);
  return /\.(?:md|markdown|mdown|txt|rst)$/i.test(path);
}

function isImageReference(value: string): boolean {
  const { path } = splitDocumentReference(value);
  return /\.(?:png|jpe?g|gif|webp|avif|svg|bmp)$/i.test(path);
}

function resolveDocumentPath(value: string, baseReference: string): string {
  if (/^(?:https?:|data:|#|\/)/i.test(value)) return value;
  const { path, suffix } = splitDocumentReference(value);
  const base = splitDocumentReference(baseReference).path.split("/").slice(0, -1);
  const parts = [...base, ...path.split("/")];
  const normalized: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (normalized.length) normalized.pop();
      continue;
    }
    normalized.push(part);
  }
  return `${normalized.join("/")}${suffix}`;
}

function projectPathHref(path: string, projectId: string): string {
  return documentReferenceHref({ ref: path }, projectId);
}

export function DocumentCapabilities({ node }: { node: GraphNode }) {
  const capabilities = node.capability_status ?? [];
  if (!capabilities.length) return null;

  return (
    <section className="document-capabilities" aria-label="Agent capabilities">
      <div className="document-capability-group">
        <span className="document-capability-label">Capabilities</span>
        <div className="document-capability-list">
          {capabilities.map((item) => (
            <a
              className="document-capability-link"
              href={capabilityCatalogHref(item.capability_id)}
              target="_blank"
              rel="noreferrer"
              key={item.capability_id}
              title={`${item.skills} skills · ${item.mcps} MCP · ${capabilityDeploymentLabel(item)}`}
            >
              {item.capability_id} ({item.skills}/{item.mcps})
            </a>
          ))}
        </div>
      </div>
    </section>
  );
}

function MarkdownContent({
  content,
  baseReference,
  projectId,
  onOpenDocument,
}: {
  content: string;
  baseReference: string;
  projectId: string;
  onOpenDocument?: (reference: DocumentTarget) => void;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        h1: ({ children }) => <h1 id={headingId(markdownText(children))}>{children}</h1>,
        h2: ({ children }) => <h2 id={headingId(markdownText(children))}>{children}</h2>,
        h3: ({ children }) => <h3 id={headingId(markdownText(children))}>{children}</h3>,
        a: ({ href, children, ...props }) => {
          const value = href ?? "";
          const resolved = resolveDocumentPath(value, baseReference);
          const external = isExternalDocumentReference({
            ref: resolved,
            title: null,
            media_type: null,
            imports: [],
          });
          if (external || value.startsWith("#") || !onOpenDocument) {
            return <a href={external ? resolved : value} {...props}>{children}</a>;
          }
          const reference: DocumentTarget = {
            ref: resolved,
            title: markdownText(children) || resolved,
            media_type: null,
            imports: [],
            kind: isGraphSourceReference(resolved) ? "graph" : "document",
          };
          return (
            <a
              href={projectPathHref(resolved, projectId)}
              {...props}
              onClick={(event) => {
                event.preventDefault();
                onOpenDocument(reference);
              }}
            >
              {children}
            </a>
          );
        },
        img: ({ src, alt, ...props }) => {
          const value = src ?? "";
          const resolved = resolveDocumentPath(value, baseReference);
          const imageSrc = /^(?:https?:|data:|\/)/i.test(resolved)
            ? resolved
            : projectPathHref(resolved, projectId);
          return <img src={imageSrc} alt={alt ?? ""} {...props} />;
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

function useProjectDocument(
  projectId: string,
  reference: DocumentRef,
  refreshKey?: string | number,
) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setContent(null);
    setError(null);
    if (!reference.ref || isImageReference(reference.ref)) {
      return () => { cancelled = true; };
    }
    void fetch(documentReferenceContentHref(reference, projectId))
      .then(async (response) => {
        if (!response.ok) throw new Error(`Unable to read ${reference.ref}`);
        return response.text();
      })
      .then((value) => { if (!cancelled) setContent(value); })
      .catch((reason: unknown) => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [projectId, reference.ref, refreshKey]);
  return { content, error };
}

function uniqueReferences<T extends { ref: string }>(references: T[]): T[] {
  const seen = new Set<string>();
  return references.filter((reference) => {
    if (seen.has(reference.ref)) return false;
    seen.add(reference.ref);
    return true;
  });
}

function uniqueArtifacts(artifacts: ArtifactLike[]): ArtifactLike[] {
  const seen = new Set<string>();
  return artifacts.filter((artifact) => {
    const key = `${artifact.name}:${artifact.ref ?? ""}:${artifact.kind}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/** Build the graph document from the live graph response, not a stale receipt. */
export function liveGraphSource(
  nodes: GraphNode[],
  edges: Edge[],
  artifacts: Artifact[],
): GraphSource {
  const root = nodes.find((node) => node.parent_id === null);
  const sourceEdges = edges
    .filter((edge) => edge.type === "FOLLOWS")
    .map((edge) => ({ type: edge.type, src: edge.src, dst: edge.dst }));
  const sourceArtifacts = (node: GraphNode): GraphSourceArtifact[] =>
    artifacts
      .filter((artifact) => node.artifact_refs.includes(artifact.id))
      .map((artifact) => ({
        kind: artifact.kind,
        name: artifact.name,
        ref: artifact.ref,
        content: artifact.content,
      }));
  return {
    project_name: root?.project_name ?? root?.objective ?? null,
    notes: null,
    nodes: nodes.map((node) => ({
      key: node.id,
      objective: node.objective,
      // The composed root is a graph boundary; its old planner receipt should
      // not shadow the live child instructions shown below.
      generated_prompt: node.parent_id === null ? null : node.generated_prompt,
      executor: node.executor,
      agent_type: node.agent?.type_id ?? null,
      capabilities: (node.capability_status ?? []).map((item) => item.capability_id),
      document_refs: node.document_refs,
      subgraph_refs: node.subgraph_refs ?? [],
      artifacts: sourceArtifacts(node),
      parent_key: node.parent_id,
      follows: sourceEdges.filter((edge) => edge.dst === node.id).map((edge) => edge.src),
      plan: node.agent?.type_id === "planner",
    })),
    document_refs: root?.document_refs ?? [],
    subgraph_refs: root?.subgraph_refs ?? [],
    artifacts: root ? sourceArtifacts(root) : [],
    edges: sourceEdges,
  };
}

function sourceNodePredecessors(
  node: GraphSourceNode,
  edges: GraphSource["edges"],
): string[] {
  return uniqueReferences([
    ...node.follows.map((key) => ({ ref: key })),
    ...edges
      .filter((edge) => edge.type === "FOLLOWS" && edge.dst === node.key)
      .map((edge) => ({ ref: edge.src })),
  ].map((item) => ({ ref: item.ref }))).map((item) => item.ref);
}

function sourceArtifactState(
  node: GraphNode | undefined,
  artifacts: Artifact[],
): Artifact[] {
  if (!node) return [];
  const ids = new Set(node.artifact_refs);
  return artifacts.filter((artifact) => ids.has(artifact.id));
}

function GraphSourceNodeView({
  node,
  nodes,
  edges,
  path,
  projectId,
  onOpenDocument,
  contextNodes,
  stateArtifacts,
}: {
  node: GraphSourceNode;
  nodes: GraphSourceNode[];
  edges: GraphSource["edges"];
  path: number[];
  projectId: string;
  onOpenDocument: (reference: DocumentTarget) => void;
  contextNodes: GraphNode[];
  stateArtifacts: Artifact[];
}) {
  const [open, setOpen] = useState(false);
  const contextNode = contextNodes.find(
    (item) => stripMarkdown(item.objective) === stripMarkdown(node.objective),
  );
  const children = orderGraphSourceNodes(nodes, edges, node.key);
  const names = new Map(nodes.map((item) => [item.key, item.objective]));
  const predecessors = sourceNodePredecessors(node, edges)
    .map((key) => names.get(key) ?? key);
  const documentRefs = uniqueReferences([
    ...node.document_refs,
    ...(contextNode?.document_refs ?? []),
  ]);
  const subgraphRefs = uniqueReferences([
    ...node.subgraph_refs,
    ...(contextNode?.subgraph_refs ?? []),
  ]);
  const artifacts = uniqueArtifacts([
    ...node.artifacts,
    ...sourceArtifactState(contextNode, stateArtifacts),
  ]);
  return (
    <details
      className={`document-node depth-${Math.min(Math.max(path.length - 1, 0), 4)}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="document-node-summary">
        <span className="document-sequence">{path.join(".")}</span>
        <span
          className="document-node-copy"
          role="heading"
          aria-level={Math.min(path.length + 1, 6)}
        >
          <strong>{stripMarkdown(node.objective)}</strong>
          <small>{node.plan ? "planner" : "planned"}</small>
        </span>
        <span className="document-agent">
          {node.agent_type ?? node.executor ?? "agent"}
        </span>
      </summary>
      <div className="document-node-body">
        {predecessors.length > 0 && (
          <p className="document-sequence-predecessors">
            <span>Follows</span> {predecessors.join(" · ")}
          </p>
        )}
        <DocumentLinks refs={documentRefs} projectId={projectId} onOpenDocument={onOpenDocument} />
        <SubgraphLinks refs={subgraphRefs} projectId={projectId} onOpenDocument={onOpenDocument} />
        <ArtifactLinks
          artifacts={artifacts}
          projectId={projectId}
          onOpenDocument={onOpenDocument}
        />
        {node.generated_prompt?.trim() && (
          <div className="document-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{node.generated_prompt.trim()}</ReactMarkdown>
          </div>
        )}
        {children.length > 0 && (
          <div className="document-children">
            {children.map((child, index) => (
              <GraphSourceNodeView
                key={child.key}
                node={child}
                nodes={nodes}
                edges={edges}
                projectId={projectId}
                onOpenDocument={onOpenDocument}
                contextNodes={contextNodes}
                stateArtifacts={stateArtifacts}
                path={[...path, index + 1]}
              />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

export function GraphSourceDocument({
  source,
  reference,
  projectId,
  contextNodes,
  stateArtifacts,
  onOpenDocument,
  onBack,
}: {
  source: GraphSource;
  reference: DocumentTarget;
  projectId: string;
  contextNodes: GraphNode[];
  stateArtifacts: Artifact[];
  onOpenDocument: (reference: DocumentTarget) => void;
  onBack: () => void;
}) {
  const contextRoot = contextNodes.find((node) =>
    node.subgraph_refs?.some((linked) => linked.ref === reference.ref),
  ) ?? contextNodes.find((node) => node.parent_id === null);
  const documentRefs = uniqueReferences([
    ...source.document_refs,
    ...(contextRoot?.document_refs ?? []),
  ]);
  const subgraphRefs = uniqueReferences([
    ...source.subgraph_refs,
    ...(contextRoot?.subgraph_refs ?? []).filter((linked) => linked.ref !== reference.ref),
  ]);
  const artifacts = uniqueArtifacts([
    ...source.artifacts,
    ...sourceArtifactState(contextRoot, stateArtifacts),
  ]);
  const children = orderGraphSourceNodes(source.nodes, source.edges, null);
  return (
    <article className="document-reader document-graph-source">
      <button className="document-reader-back" type="button" onClick={onBack}>← Back to previous document</button>
      <div className="document-kicker">Graph work breakdown</div>
      <h1>{stripMarkdown(source.project_name ?? documentReferenceLabel(reference))}</h1>
      <p className="document-reader-path">{reference.ref}</p>
      <div className="document-meta">
        <span>{source.nodes.length} work items</span>
        <span>Sequence is explicit</span>
      </div>
      {source.notes?.trim() && (
        <div className="document-intent document-markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{source.notes.trim()}</ReactMarkdown>
        </div>
      )}
      <DocumentLinks refs={documentRefs} projectId={projectId} onOpenDocument={onOpenDocument} />
      <SubgraphLinks refs={subgraphRefs} projectId={projectId} onOpenDocument={onOpenDocument} />
      <ArtifactLinks
        artifacts={artifacts}
        projectId={projectId}
        onOpenDocument={onOpenDocument}
      />
      <div id="work-specification" className="document-flow">
        <div className="document-flow-heading">
          <span>Work breakdown</span>
          <small>Every linked graph is another navigable document boundary.</small>
        </div>
        {children.map((child, index) => (
          <GraphSourceNodeView
            key={child.key}
            node={child}
            nodes={source.nodes}
            edges={source.edges}
            projectId={projectId}
            onOpenDocument={onOpenDocument}
            contextNodes={contextNodes}
            stateArtifacts={stateArtifacts}
            path={[index + 1]}
          />
        ))}
      </div>
    </article>
  );
}

function GraphSourceReader({
  projectId,
  reference,
  contextNodes,
  stateArtifacts,
  onBack,
  onOpenDocument,
  refreshKey,
}: {
  projectId: string;
  reference: DocumentTarget;
  contextNodes: GraphNode[];
  stateArtifacts: Artifact[];
  onBack: () => void;
  onOpenDocument: (reference: DocumentTarget) => void;
  refreshKey?: string | number;
}) {
  const { content, error } = useProjectDocument(projectId, reference, refreshKey);
  let source: GraphSource | null = null;
  let sourceError = error;
  if (!sourceError && content !== null) {
    try {
      source = parseGraphSource(JSON.parse(content) as unknown);
    } catch (reason: unknown) {
      sourceError = `Unable to read graph source: ${String(reason)}`;
    }
  }
  return (
    <div className="document-view" aria-label="Graph work breakdown">
      {sourceError && <p className="document-reader-error">{sourceError}</p>}
      {!sourceError && content === null && <p className="document-reader-loading">Loading graph…</p>}
      {!sourceError && source && (
        <GraphSourceDocument
          source={source}
          reference={reference}
          projectId={projectId}
          contextNodes={contextNodes}
          stateArtifacts={stateArtifacts}
          onOpenDocument={onOpenDocument}
          onBack={onBack}
        />
      )}
    </div>
  );
}

function DocumentReader({
  projectId,
  reference,
  onBack,
  onOpenDocument,
  refreshKey,
}: {
  projectId: string;
  reference: DocumentTarget;
  onBack: () => void;
  onOpenDocument?: (reference: DocumentTarget) => void;
  refreshKey?: string | number;
}) {
  const { content, error } = useProjectDocument(projectId, reference, refreshKey);

  const markdown = /\.(?:md|markdown|mdown)$/i.test(reference.ref.split(/[?#]/, 1)[0]);
  const image = isImageReference(reference.ref);
  return (
    <article className="document-reader">
      <button className="document-reader-back" type="button" onClick={onBack}>← Back to specification</button>
      <div className="document-kicker">Project document</div>
      <h1>{documentReferenceLabel(reference)}</h1>
      <p className="document-reader-path">{reference.ref}</p>
      {error && <p className="document-reader-error">{error}</p>}
      {!error && !image && content === null && <p className="document-reader-loading">Loading document…</p>}
      {!error && image && (
        <img
          className="document-reader-image"
          src={documentReferenceContentHref(reference, projectId)}
          alt={documentReferenceLabel(reference)}
        />
      )}
      {!error && content !== null && !image && (markdown ? (
        <div className="document-markdown document-reader-content">
          <MarkdownContent
            content={content}
            baseReference={reference.ref}
            projectId={projectId}
            onOpenDocument={onOpenDocument}
          />
        </div>
      ) : (
        <pre className="document-reader-content document-reader-plain">{content}</pre>
      ))}
    </article>
  );
}

function DocumentNode({
  node,
  nodes,
  edges,
  artifacts,
  path,
  projectId,
  onOpenDocument,
}: {
  node: GraphNode;
  nodes: GraphNode[];
  edges: Edge[];
  artifacts: Artifact[];
  path: number[];
  projectId?: string;
  onOpenDocument?: (reference: DocumentTarget) => void;
}) {
  const [open, setOpen] = useState(false);
  // Nested work remains in the readable specification even when a node also
  // links to a source graph. The source link is an alternate workflow view,
  // not a reason to hide live graph state.
  const children = orderDocumentNodes(nodes, edges, node.id);
  const predecessors = sequenceLabels(node, nodes, edges);
  const prompt = node.generated_prompt?.trim();
  const nodeArtifacts = artifacts.filter((artifact) => node.artifact_refs.includes(artifact.id));
  const hasAdvancedDetails = Boolean(
    predecessors.length ||
      node.capability_status?.length ||
      node.document_refs.length ||
      node.subgraph_refs?.length,
  );
  return (
    <details
      className={`document-node depth-${Math.min(Math.max(path.length - 1, 0), 4)}`}
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="document-node-summary">
        <span className="document-sequence">{path.join(".")}</span>
        <span
          className="document-node-copy"
          role="heading"
          aria-level={Math.min(path.length + 1, 6)}
        >
          <strong>{displayNodeTitle(node)}</strong>
          <small>{statusLabel(node)}</small>
        </span>
        <span className="document-agent">
          {node.agent?.type_id ?? "agent"}
          {node.agent?.harness ? ` · ${node.agent.harness}` : ""}
        </span>
      </summary>
      <div className="document-node-body">
        {prompt && (
          <section className="document-node-intent">
            <div className="document-section-heading">
              <h3>Instructions</h3>
            </div>
            <div className="document-markdown">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{prompt}</ReactMarkdown>
            </div>
          </section>
        )}
        <SubmissionSummary artifacts={nodeArtifacts} />
        <ArtifactLinks
          artifacts={nodeArtifacts}
          projectId={projectId}
          onOpenDocument={onOpenDocument}
        />
        {hasAdvancedDetails && (
          <details className="document-advanced document-node-details">
            <summary>Step details</summary>
            {predecessors.length > 0 && (
              <p className="document-sequence-predecessors">
                <span>Follows</span> {predecessors.join(" · ")}
              </p>
            )}
            <DocumentCapabilities node={node} />
            <DocumentLinks refs={node.document_refs} projectId={projectId} onOpenDocument={onOpenDocument} />
            <WorkflowSourceLinks refs={node.subgraph_refs ?? []} projectId={projectId} onOpenDocument={onOpenDocument} />
          </details>
        )}
        {children.length > 0 && (
          <div className="document-children">
            {children.map((child, index) => (
              <DocumentNode
                key={child.id}
                node={child}
                nodes={nodes}
                edges={edges}
                artifacts={artifacts}
                projectId={projectId}
                onOpenDocument={onOpenDocument}
                path={[...path, index + 1]}
              />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

function documentTargetFromHistory(state: unknown): DocumentTarget | null {
  const record = objectRecord(state);
  const target = objectRecord(record?.document);
  if (typeof target?.ref !== "string") return null;
  return {
    ref: target.ref,
    title: stringValue(target.title),
    media_type: stringValue(target.media_type),
    imports: [],
    kind: target.kind === "graph" ? "graph" : "document",
  };
}

function documentTargetFromLocation(): DocumentTarget | null {
  if (typeof window === "undefined") return null;
  const marker = "#document=";
  if (!window.location.hash.startsWith(marker)) return null;
  const ref = decodeURIComponent(window.location.hash.slice(marker.length));
  if (!ref) return null;
  return {
    ref,
    title: null,
    media_type: null,
    imports: [],
    kind: isGraphSourceReference(ref) ? "graph" : "document",
  };
}

export function DocumentView({ nodes, edges, artifacts, projectId, refreshKey }: Props) {
  const [documentReference, setDocumentReference] = useState<DocumentTarget | null>(() =>
    documentTargetFromHistory(typeof window === "undefined" ? null : window.history.state)
      ?? documentTargetFromLocation(),
  );
  useEffect(() => {
    const onPopState = (event: PopStateEvent) => {
      setDocumentReference(documentTargetFromHistory(event.state));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  const root = nodes.find((node) => node.parent_id === null);
  if (!root) {
    return <div className="document-empty">No specification is available.</div>;
  }
  const children = orderDocumentNodes(nodes, edges, root.id);
  const openDocument = (reference: DocumentTarget) => {
    setDocumentReference(reference);
    window.history.pushState({ document: reference }, "", `#document=${encodeURIComponent(reference.ref)}`);
  };
  if (documentReference) {
    const onBack = () => window.history.back();
    if (documentReference.kind === "graph") {
      const liveSource = splitDocumentReference(documentReference.ref).path === "workflow.graph.json"
        ? liveGraphSource(nodes, edges, artifacts)
        : null;
      if (liveSource) {
        return (
          <GraphSourceDocument
            source={liveSource}
            reference={documentReference}
            projectId={projectId ?? root.project_id}
            contextNodes={nodes}
            stateArtifacts={artifacts}
            onOpenDocument={openDocument}
            onBack={onBack}
          />
        );
      }
      return (
        <GraphSourceReader
          projectId={projectId ?? root.project_id}
          reference={documentReference}
          contextNodes={nodes}
          stateArtifacts={artifacts}
          onOpenDocument={openDocument}
          onBack={onBack}
          refreshKey={refreshKey}
        />
      );
    }
    return (
      <div className="document-view" aria-label="Project document">
        <DocumentReader
          projectId={projectId ?? root.project_id}
          reference={documentReference}
          onOpenDocument={openDocument}
          onBack={onBack}
          refreshKey={refreshKey}
        />
      </div>
    );
  }
  const visibleChildren = children;
  const rootArtifacts = artifacts.filter((artifact) => root.artifact_refs.includes(artifact.id));
  return (
    <div className="document-view" aria-label="Read-only specification">
      <article className="document-spec">
        <div className="document-kicker">Project overview</div>
        <h1>{stripMarkdown(root.project_name ?? root.objective)}</h1>
        <div className="document-meta">
          <span className={`badge ${root.ui_state}`}>{statusLabel(root)}</span>
          <span>{Math.max(nodes.length - 1, 0)} workflow steps</span>
          <span>Ordered workflow</span>
        </div>
        {root.generated_prompt?.trim() && (
          <div className="document-intent document-markdown">
            <div className="document-section-heading">
              <h3>Project goal</h3>
            </div>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {root.generated_prompt.trim()}
            </ReactMarkdown>
          </div>
        )}
        <DocumentLinks
          refs={root.document_refs}
          projectId={projectId ?? root.project_id}
          onOpenDocument={openDocument}
        />
        <SubmissionSummary artifacts={rootArtifacts} />
        <ArtifactLinks
          artifacts={rootArtifacts}
          projectId={projectId ?? root.project_id}
          onOpenDocument={openDocument}
        />
        <WorkflowSourceLinks
          refs={root.subgraph_refs ?? []}
          projectId={projectId ?? root.project_id}
          onOpenDocument={openDocument}
        />
        {visibleChildren.length > 0 && (
          <div id="work-specification" className="document-flow">
            <div className="document-flow-heading">
              <span>Work plan</span>
              <small>Open a step to read its instructions, result, and generated files.</small>
            </div>
            {visibleChildren.map((child, index) => (
              <DocumentNode
                key={child.id}
                node={child}
                nodes={nodes}
                edges={edges}
                artifacts={artifacts}
                projectId={projectId ?? root.project_id}
                onOpenDocument={openDocument}
                path={[index + 1]}
              />
            ))}
          </div>
        )}
      </article>
    </div>
  );
}
