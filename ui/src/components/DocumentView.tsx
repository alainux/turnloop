import { useState, type ReactElement } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  ArchitectureDiagram,
  ArchitectureSection,
  ArchitectureSpec,
  Edge,
  GraphNode,
} from "../domain";

interface Props {
  nodes: GraphNode[];
  edges: Edge[];
  architectureSpec?: ArchitectureSpec | null;
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
  const siblings = nodes.filter((node) => node.parent_id === parentId);
  const siblingIds = new Set(siblings.map((node) => node.id));
  const position = new Map(siblings.map((node, index) => [node.id, index]));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map(siblings.map((node) => [node.id, 0]));
  for (const edge of edges) {
    if (
      edge.type !== "DEPENDS_ON" ||
      !siblingIds.has(edge.src) ||
      !siblingIds.has(edge.dst)
    ) {
      continue;
    }
    outgoing.set(edge.src, [...(outgoing.get(edge.src) ?? []), edge.dst]);
    indegree.set(edge.dst, (indegree.get(edge.dst) ?? 0) + 1);
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

function statusLabel(node: GraphNode): string {
  if (node.status === "RUNNING") {
    return node.agent_message?.trim()
      ? `${node.agent_state ?? "working"} — ${node.agent_message.trim()}`
      : node.agent_state ?? "working";
  }
  return node.ui_state.replaceAll("_", " ");
}

function dependencyLabels(node: GraphNode, nodes: GraphNode[], edges: Edge[]): string[] {
  const names = new Map(nodes.map((item) => [item.id, item.objective]));
  return edges
    .filter((edge) => edge.type === "DEPENDS_ON" && edge.dst === node.id)
    .map((edge) => names.get(edge.src))
    .filter((name): name is string => Boolean(name));
}

function wrapLabel(value: string, width = 20): string[] {
  const words = value.split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    if (line && `${line} ${word}`.length > width) {
      lines.push(line);
      line = word;
    } else {
      line = line ? `${line} ${word}` : word;
    }
  }
  if (line) lines.push(line);
  return lines.length ? lines.slice(0, 3) : [""];
}

function diagramPositions(diagram: ArchitectureDiagram) {
  const incoming = new Map(diagram.nodes.map((node) => [node.id, [] as string[]]));
  for (const edge of diagram.edges) {
    incoming.set(edge.dst, [...(incoming.get(edge.dst) ?? []), edge.src]);
  }
  const rankCache = new Map<string, number>();
  const rankOf = (id: string, visiting = new Set<string>()): number => {
    const cached = rankCache.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const rank = Math.max(
      0,
      ...(incoming.get(id) ?? []).map((parent) => rankOf(parent, visiting) + 1),
    );
    visiting.delete(id);
    rankCache.set(id, rank);
    return rank;
  };
  const rows = new Map<number, string[]>();
  for (const node of diagram.nodes) {
    const rank = rankOf(node.id);
    rows.set(rank, [...(rows.get(rank) ?? []), node.id]);
  }
  const maxRank = Math.max(0, ...rows.keys());
  const maxRow = Math.max(1, ...Array.from(rows.values()).map((row) => row.length));
  const positions = new Map<string, { x: number; y: number }>();
  for (const [rank, ids] of rows) {
    ids.forEach((id, row) => {
      const primary = diagram.direction === "LR" ? rank : row;
      const secondary = diagram.direction === "LR" ? row : rank;
      positions.set(id, { x: 30 + primary * 220, y: 24 + secondary * 92 });
    });
  }
  return {
    positions,
    width: (diagram.direction === "LR" ? maxRank + 1 : maxRow) * 220 + 40,
    height: (diagram.direction === "LR" ? maxRow : maxRank + 1) * 92 + 24,
  };
}

function ArchitectureDiagramView({ diagram }: { diagram: ArchitectureDiagram }) {
  const { positions, width, height } = diagramPositions(diagram);
  const markerId = `diagram-arrow-${diagram.id}`;
  return (
    <figure className="architecture-diagram">
      <figcaption>
        <strong>{diagram.title}</strong>
        {diagram.description && <span>{diagram.description}</span>}
      </figcaption>
      <div className="architecture-diagram-scroll">
        <svg
          className="architecture-diagram-svg"
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={diagram.title}
        >
          <defs>
            <marker
              id={markerId}
              markerWidth="8"
              markerHeight="8"
              refX="7"
              refY="4"
              orient="auto"
            >
              <path d="M0,0 L8,4 L0,8 Z" />
            </marker>
          </defs>
          {diagram.edges.map((edge, index) => {
            const source = positions.get(edge.src);
            const target = positions.get(edge.dst);
            if (!source || !target) return null;
            const horizontal = diagram.direction === "LR";
            const x1 = horizontal ? source.x + 154 : source.x + 77;
            const y1 = horizontal ? source.y + 28 : source.y + 56;
            const x2 = horizontal ? target.x : target.x + 77;
            const y2 = horizontal ? target.y + 28 : target.y;
            return (
              <g key={`${edge.src}-${edge.dst}-${index}`} className="architecture-diagram-edge">
                <line x1={x1} y1={y1} x2={x2} y2={y2} markerEnd={`url(#${markerId})`} />
                {edge.label && (
                  <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 6} textAnchor="middle">
                    {edge.label}
                  </text>
                )}
              </g>
            );
          })}
          {diagram.nodes.map((node) => {
            const position = positions.get(node.id);
            if (!position) return null;
            const lines = wrapLabel(node.label);
            return (
              <g key={node.id} className="architecture-diagram-node">
                <rect x={position.x} y={position.y} width="154" height="56" rx="8" />
                <text x={position.x + 77} y={position.y + 22} textAnchor="middle">
                  {lines.map((line, index) => (
                    <tspan key={line} x={position.x + 77} dy={index === 0 ? 0 : 14}>
                      {line}
                    </tspan>
                  ))}
                </text>
                <text className="architecture-diagram-kind" x={position.x + 77} y={position.y + 48} textAnchor="middle">
                  {node.kind}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
    </figure>
  );
}

function sectionEntries(sections: ArchitectureSection[], depth = 0): ReactElement[] {
  return sections.flatMap((section) => [
    <li key={section.id} style={{ marginLeft: `${depth * 14}px` }}>
      <a href={`#architecture-section-${section.id}`}>{section.title}</a>
    </li>,
    ...sectionEntries(section.subsections, depth + 1),
  ]);
}

function ArchitectureSectionView({
  section,
  diagrams,
  depth = 0,
}: {
  section: ArchitectureSection;
  diagrams: Map<string, ArchitectureDiagram>;
  depth?: number;
}) {
  return (
    <details
      id={`architecture-section-${section.id}`}
      className={`architecture-section depth-${Math.min(depth, 3)}`}
      open={depth === 0}
    >
      <summary>
        <span>{section.title}</span>
      </summary>
      <div className="architecture-section-body">
        {section.content && (
          <div className="document-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{section.content}</ReactMarkdown>
          </div>
        )}
        {section.diagram_ids.map((diagramId) => {
          const diagram = diagrams.get(diagramId);
          return diagram ? <ArchitectureDiagramView key={diagram.id} diagram={diagram} /> : null;
        })}
        {section.subsections.map((child) => (
          <ArchitectureSectionView key={child.id} section={child} diagrams={diagrams} depth={depth + 1} />
        ))}
      </div>
    </details>
  );
}

function MetadataList({ title, values }: { title: string; values: string[] }) {
  if (!values.length) return null;
  return (
    <section className="architecture-metadata-list">
      <h3>{title}</h3>
      <ul>{values.map((value) => <li key={value}>{value}</li>)}</ul>
    </section>
  );
}

function ArchitectureMetadata({ spec }: { spec: ArchitectureSpec }) {
  const diagrams = new Map(spec.diagrams.map((diagram) => [diagram.id, diagram]));
  const attachedDiagramIds = new Set<string>();
  const collectAttachedDiagrams = (sections: ArchitectureSection[]) => {
    sections.forEach((section) => {
      section.diagram_ids.forEach((diagramId) => attachedDiagramIds.add(diagramId));
      collectAttachedDiagrams(section.subsections);
    });
  };
  collectAttachedDiagrams(spec.sections);
  const unplacedDiagrams = spec.diagrams.filter((diagram) => !attachedDiagramIds.has(diagram.id));
  return (
    <div className="architecture-metadata">
      <div className="document-kicker">Architecture metadata</div>
      <div className="architecture-summary document-markdown">
        <h2>Executive summary</h2>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{spec.executive_summary}</ReactMarkdown>
      </div>
      <nav className="architecture-toc" aria-label="Table of contents">
        <div className="architecture-toc-heading">Contents</div>
        <ol>{sectionEntries(spec.sections)}</ol>
      </nav>
      <div className="architecture-two-column">
        {spec.approach && <div className="architecture-callout document-markdown"><h3>Approach</h3><ReactMarkdown remarkPlugins={[remarkGfm]}>{spec.approach}</ReactMarkdown></div>}
        {spec.strategy && <div className="architecture-callout document-markdown"><h3>Strategy</h3><ReactMarkdown remarkPlugins={[remarkGfm]}>{spec.strategy}</ReactMarkdown></div>}
      </div>
      <MetadataList title="Architecture principles" values={spec.architecture_principles} />
      <MetadataList title="Requirements" values={spec.requirements} />
      <MetadataList title="Constraints" values={spec.constraints} />
      {spec.decisions.length > 0 && (
        <section className="architecture-cards">
          <h3>Decisions</h3>
          {spec.decisions.map((decision) => (
            <article key={decision.id}>
              <h4>{decision.title}</h4>
              <p>{decision.decision}</p>
              <small>Rationale: {decision.rationale}</small>
              {decision.consequences.length > 0 && <ul>{decision.consequences.map((item) => <li key={item}>{item}</li>)}</ul>}
            </article>
          ))}
        </section>
      )}
      {spec.risks.length > 0 && (
        <section className="architecture-cards">
          <h3>Risks</h3>
          {spec.risks.map((risk) => (
            <article key={risk.id}>
              <h4>{risk.title}</h4>
              <p>{risk.description}</p>
              {risk.mitigation && <small>Mitigation: {risk.mitigation}</small>}
            </article>
          ))}
        </section>
      )}
      <section className="architecture-sections">
        {spec.sections.map((section) => <ArchitectureSectionView key={section.id} section={section} diagrams={diagrams} />)}
      </section>
      {unplacedDiagrams.length > 0 && (
        <section className="architecture-sections architecture-diagrams">
          <h3>Diagrams</h3>
          {unplacedDiagrams.map((diagram) => <ArchitectureDiagramView key={diagram.id} diagram={diagram} />)}
        </section>
      )}
      <MetadataList title="Acceptance criteria" values={spec.acceptance_criteria} />
    </div>
  );
}

function DocumentNode({
  node,
  nodes,
  edges,
  path,
}: {
  node: GraphNode;
  nodes: GraphNode[];
  edges: Edge[];
  path: number[];
}) {
  const [open, setOpen] = useState(path.length < 2);
  const children = orderDocumentNodes(nodes, edges, node.id);
  const dependencies = dependencyLabels(node, nodes, edges);
  const prompt = node.generated_prompt?.trim();
  return (
    <details
      className="document-node"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="document-node-summary">
        <span className="document-sequence">{path.join(".")}</span>
        <span className="document-node-copy">
          <strong>{node.objective}</strong>
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
                path={[...path, index + 1]}
              />
            ))}
          </div>
        )}
      </div>
    </details>
  );
}

export function DocumentView({ nodes, edges, architectureSpec }: Props) {
  const root = nodes.find((node) => node.parent_id === null);
  if (!root) {
    return <div className="document-empty">No specification is available.</div>;
  }
  const children = orderDocumentNodes(nodes, edges, root.id);
  return (
    <div className="document-view" aria-label="Read-only specification">
      <article className="document-spec">
        <div className="document-kicker">Read-only graph specification</div>
        <h1>{architectureSpec?.title ?? root.architecture_spec?.title ?? root.objective}</h1>
        <div className="document-meta">
          <span className={`badge ${root.ui_state}`}>{statusLabel(root)}</span>
          <span>{nodes.length} work items</span>
          <span>Sequence follows dependencies</span>
        </div>
        {(architectureSpec ?? root.architecture_spec) ? (
          <ArchitectureMetadata spec={(architectureSpec ?? root.architecture_spec)!} />
        ) : root.generated_prompt?.trim() ? (
          <div className="document-intent document-markdown">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {root.generated_prompt.trim()}
            </ReactMarkdown>
          </div>
        ) : null}
        <div className="document-flow">
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
              path={[index + 1]}
            />
          ))}
        </div>
      </article>
    </div>
  );
}
