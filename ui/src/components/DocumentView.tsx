import { useEffect, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  DocumentRef,
  Edge,
  GraphNode,
} from "../domain";
import {
  displayNodeTitle,
  documentReferenceContentHref,
  documentReferenceHref,
  documentReferenceLabel,
  isExternalDocumentReference,
  stripMarkdown,
} from "../domain";

interface Props {
  nodes: GraphNode[];
  edges: Edge[];
  projectId?: string;
}

/**
 * Order nodes within one containment section by their explicit dependencies.
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
    if (edge.type !== "DEPENDS_ON") continue;

    // Dependencies can cross a projected child boundary. Compare the direct
    // children of this document section so an integrator remains after the
    // complete implementation → verifier subtree.
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
 * graph. A verifier is still an ordinary DEPENDS_ON node for scheduling; in
 * the read-only specification it is shown beneath the implementation it
 * inspects so the sequence is legible.
 */
export function documentParentMap(nodes: GraphNode[], edges: Edge[]): Map<string, string | null> {
  const parentOf = new Map<string, string | null>();
  for (const node of nodes) {
    parentOf.set(node.id, node.parent_id);
  }
  // A verifier is a normal dependency in the graph, never a special graph
  // relation. The document projection nests it below the work item it
  // verifies so the specification reads as implementation → verification.
  // This changes presentation only; scheduling and graph edges stay intact.
  for (const node of nodes) {
    if (node.agent?.type_id !== "verifier") continue;
    const target = edges.find(
      (edge) => edge.type === "DEPENDS_ON" && edge.dst === node.id,
    )?.src;
    if (target && parentOf.has(target)) parentOf.set(node.id, target);
  }
  return parentOf;
}

function statusLabel(node: GraphNode): string {
  if (node.status === "RUNNING") {
    return node.agent_message?.trim()
      ? `${node.agent_state ?? "working"} — ${node.agent_message.trim()}`
      : node.agent_state ?? "working";
  }
  return node.ui_state.replaceAll("_", " ");
}

function dependencyLabels(node: GraphNode, nodes: GraphNode[], edges: Edge[]): string[] {
  const names = new Map(nodes.map((item) => [item.id, displayNodeTitle(item)]));
  return edges
    .filter((edge) => edge.type === "DEPENDS_ON" && edge.dst === node.id)
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
  onOpenDocument?: (reference: DocumentRef) => void;
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
    <section className="document-links">
      <h3>Document references</h3>
      <ul>{refs.map((reference) => render(reference))}</ul>
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
  return documentReferenceHref(
    { ref: path, title: null, media_type: null, imports: [] },
    projectId,
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
  onOpenDocument?: (reference: DocumentRef) => void;
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
          const reference: DocumentRef = {
            ref: resolved,
            title: markdownText(children) || resolved,
            media_type: null,
            imports: [],
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

function useProjectDocument(projectId: string, reference: DocumentRef) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    setContent(null);
    setError(null);
    if (!reference.ref) return () => { cancelled = true; };
    void fetch(documentReferenceContentHref(reference, projectId))
      .then(async (response) => {
        if (!response.ok) throw new Error(`Unable to read ${reference.ref}`);
        return response.text();
      })
      .then((value) => { if (!cancelled) setContent(value); })
      .catch((reason: unknown) => { if (!cancelled) setError(String(reason)); });
    return () => { cancelled = true; };
  }, [projectId, reference.ref]);
  return { content, error };
}

function DocumentReader({
  projectId,
  reference,
  onBack,
  onOpenDocument,
}: {
  projectId: string;
  reference: DocumentRef;
  onBack: () => void;
  onOpenDocument?: (reference: DocumentRef) => void;
}) {
  const { content, error } = useProjectDocument(projectId, reference);

  const markdown = /\.(?:md|markdown|mdown)$/i.test(reference.ref.split(/[?#]/, 1)[0]);
  return (
    <article className="document-reader">
      <button className="document-reader-back" type="button" onClick={onBack}>← Back to specification</button>
      <div className="document-kicker">Project document</div>
      <h1>{documentReferenceLabel(reference)}</h1>
      <p className="document-reader-path">{reference.ref}</p>
      {error && <p className="document-reader-error">{error}</p>}
      {!error && content === null && <p className="document-reader-loading">Loading document…</p>}
      {!error && content !== null && (markdown ? (
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
  path,
  projectId,
  onOpenDocument,
}: {
  node: GraphNode;
  nodes: GraphNode[];
  edges: Edge[];
  path: number[];
  projectId?: string;
  onOpenDocument?: (reference: DocumentRef) => void;
}) {
  const [open, setOpen] = useState(path.length < 2);
  const children = orderDocumentNodes(nodes, edges, node.id);
  const dependencies = dependencyLabels(node, nodes, edges);
  const prompt = node.generated_prompt?.trim();
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
        {dependencies.length > 0 && (
          <p className="document-dependencies">
            <span>Depends on</span> {dependencies.join(" · ")}
          </p>
        )}
        <DocumentLinks refs={node.document_refs} projectId={projectId} onOpenDocument={onOpenDocument} />
        {prompt && (
          <div className="document-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{prompt}</ReactMarkdown>
          </div>
        )}
        {children.length > 0 && (
          <div className="document-children">
            {children.map((child, index) => (
              <DocumentNode
                key={child.id}
                node={child}
                nodes={nodes}
                edges={edges}
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

export function DocumentView({ nodes, edges, projectId }: Props) {
  const [documentReference, setDocumentReference] = useState<DocumentRef | null>(null);
  useEffect(() => {
    const onPopState = () => setDocumentReference(null);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  const root = nodes.find((node) => node.parent_id === null);
  if (!root) {
    return <div className="document-empty">No specification is available.</div>;
  }
  const children = orderDocumentNodes(nodes, edges, root.id);
  const openDocument = (reference: DocumentRef) => {
    setDocumentReference(reference);
    window.history.pushState({ document: reference.ref }, "", `#document=${encodeURIComponent(reference.ref)}`);
  };
  if (documentReference) {
    return (
      <div className="document-view" aria-label="Project document">
        <DocumentReader projectId={projectId ?? root.project_id} reference={documentReference} onOpenDocument={openDocument} onBack={() => {
          window.history.back();
          setDocumentReference(null);
        }} />
      </div>
    );
  }
  return (
    <div className="document-view" aria-label="Read-only specification">
      <article className="document-spec">
        <div className="document-kicker">Read-only graph specification</div>
        <h1>{stripMarkdown(root.project_name ?? root.objective)}</h1>
        <div className="document-meta">
          <span className={`badge ${root.ui_state}`}>{statusLabel(root)}</span>
          <span>{nodes.length} work items</span>
          <span>Sequence follows dependencies</span>
        </div>
        {root.generated_prompt?.trim() && (
          <div className="document-intent document-markdown">
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
        <div id="work-specification" className="document-flow">
          <div className="document-flow-heading">
            <span>Work specification</span>
            <small>Nested sections are collapsible. This view is read-only.</small>
          </div>
          {children.map((child, index) => (
            <DocumentNode
              key={child.id}
              node={child}
              nodes={nodes}
              edges={edges}
              projectId={projectId ?? root.project_id}
              onOpenDocument={openDocument}
              path={[index + 1]}
            />
          ))}
        </div>
      </article>
    </div>
  );
}
