import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  AgentConfig,
  GraphNode,
  HarnessCapability,
  NodeDetail as Detail,
  Permission,
  Reasoning,
} from "../domain";
import { api, json } from "../api";
import { DiffView } from "./DiffView";
import { Icon } from "./Icon";
import { ModelControl } from "./ModelControl";
const TerminalView = lazy(() =>
  import("./TerminalView").then((module) => ({ default: module.TerminalView })),
);

type Tab = "overview" | "diff" | "terminal" | "history";
interface Props {
  nodeId: string;
  refreshKey: string;
  capabilities: HarnessCapability[];
  onClose: () => void;
  onChanged: () => Promise<void>;
  notify: (text: string) => void;
}
export function Inspector({
  nodeId,
  refreshKey,
  capabilities,
  onClose,
  onChanged,
  notify,
}: Props) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [tab, setTab] = useState<Tab>("overview");
  const [error, setError] = useState("");
  const dirty = useRef(false);
  const loadVersion = useRef(0);
  const load = async () => {
    const version = ++loadVersion.current;
    try {
      const result = await api<Detail>(`/api/nodes/${nodeId}`);
      if (version !== loadVersion.current) return;
      setDetail(result);
      setError("");
    } catch (cause) {
      if (version !== loadVersion.current) return;
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };
  useEffect(() => {
    dirty.current = false;
    setDetail(null);
    void load();
  }, [nodeId]);
  useEffect(() => {
    if (!dirty.current) void load();
  }, [refreshKey]);
  const mutate = async (path: string, init?: RequestInit, message?: string) => {
    try {
      await api(path, init);
      if (message) notify(message);
      await onChanged();
      await load();
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : String(cause));
    }
  };
  return (
    <aside className="inspector" id="inspector">
      <div className="panel-heading">
        <span>Inspector</span>
        <button
          className="quiet-icon"
          onClick={onClose}
          aria-label="Close inspector"
        >
          <Icon name="panel-right-close" />
        </button>
      </div>
      <div className="inspector-tabs" role="tablist">
        {(["overview", "diff", "terminal", "history"] as Tab[]).map((item) => (
          <button
            key={item}
            role="tab"
            aria-selected={tab === item}
            className={tab === item ? "active" : ""}
            onClick={() => setTab(item)}
          >
            {item[0].toUpperCase() + item.slice(1)}
          </button>
        ))}
      </div>
      <div className="detail">
        {error ? (
          <p className="detail-error">{error}</p>
        ) : !detail ? (
          <p className="detail-loading">Loading node…</p>
        ) : tab === "terminal" ? (
          <Suspense
            fallback={<p className="detail-loading">Loading terminal…</p>}
          >
            <TerminalView
              node={detail.node}
              artifacts={detail.artifacts}
              runs={detail.runs}
            />
          </Suspense>
        ) : tab === "diff" ? (
          <DiffView artifacts={detail.artifacts} />
        ) : tab === "history" ? (
          <History detail={detail} />
        ) : (
          <Overview
            detail={detail}
            capabilities={capabilities}
            mutate={mutate}
            onDirtyChange={(value) => {
              dirty.current = value;
            }}
          />
        )}
      </div>
    </aside>
  );
}

function Overview({
  detail,
  capabilities,
  mutate,
  onDirtyChange,
}: {
  detail: Detail;
  capabilities: HarnessCapability[];
  mutate: (path: string, init?: RequestInit, message?: string) => Promise<void>;
  onDirtyChange: (value: boolean) => void;
}) {
  const node = detail.node;
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [prompt, setPrompt] = useState(node.generated_prompt ?? "");
  const [objective, setObjective] = useState(node.objective);
  const [agent, setAgent] = useState<AgentConfig | null>(node.agent ?? null);
  const [cascade, setCascade] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const [forking, setForking] = useState(false);
  const [forkObjective, setForkObjective] = useState(node.objective);
  const [forkPrompt, setForkPrompt] = useState(node.generated_prompt ?? "");
  useEffect(() => {
    setPrompt(node.generated_prompt ?? "");
    setObjective(node.objective);
    setAgent(node.agent ?? null);
    setEditingPrompt(false);
  }, [node.id, node.revision]);
  const scopeDirty =
    objective !== node.objective || prompt !== (node.generated_prompt ?? "");
  const agentDirty = JSON.stringify(agent) !== JSON.stringify(node.agent);
  useEffect(() => {
    onDirtyChange(
      editingPrompt ||
        scopeDirty ||
        agentDirty ||
        forking ||
        Boolean(feedback.trim()) ||
        Object.values(inputs).some(Boolean),
    );
    return () => onDirtyChange(false);
  }, [
    editingPrompt,
    scopeDirty,
    agentDirty,
    forking,
    feedback,
    inputs,
    onDirtyChange,
  ]);
  return (
    <>
      <h2 className="detail-title">{node.objective}</h2>
      <div className="detail-meta">
        <span className={`badge ${node.ui_state}`}>
          {node.generation_active
            ? "generating"
            : node.ui_state.replaceAll("_", " ")}
        </span>
        <span className="badge">Revision {node.revision}</span>
      </div>
      <section className="section instructions-section">
        <div className="section-heading">
          <span>Agent instructions</span>
          <button
            className="quiet-icon"
            onClick={() => setEditingPrompt((value) => !value)}
            aria-label="Edit agent instructions"
          >
            <Icon name="pencil" />
          </button>
        </div>
        {editingPrompt ? (
          <>
            <label className="field">
              <span>Objective</span>
              <input
                value={objective}
                maxLength={160}
                onChange={(event) => setObjective(event.target.value)}
              />
            </label>
            <textarea
              className="instruction-editor"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
            />
            <button
              className="button accent"
              disabled={!scopeDirty}
              onClick={() =>
                mutate(
                  `/api/nodes/${node.id}/edit`,
                  json("POST", {
                    objective: objective.trim(),
                    generated_prompt: prompt.trim() || null,
                  }),
                  "Instructions updated",
                )
              }
            >
              Save instructions
            </button>
          </>
        ) : (
          <div className="markdown-body">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {prompt || "_No additional instructions._"}
            </ReactMarkdown>
          </div>
        )}
      </section>
      {agent && (
        <section className="section">
          <div className="section-heading">
            <span>Agent configuration</span>
          </div>
          <div className="form-grid agent-config-grid">
            <ModelControl
              harness={agent.harness}
              model={agent.model ?? ""}
              reasoning={agent.reasoning}
              capabilities={capabilities}
              onHarness={(harness) => {
                const model = capabilities.find((item) => item.id === harness)
                  ?.models[0];
                setAgent({
                  ...agent,
                  harness,
                  model: model?.id ?? null,
                  reasoning: model?.reasoning?.[0] ?? "default",
                  session_id:
                    harness === agent.harness ? agent.session_id : null,
                });
              }}
              onModel={(model) => setAgent({ ...agent, model: model || null })}
              onReasoning={(reasoning: Reasoning) =>
                setAgent({ ...agent, reasoning })
              }
            />
            <label className="field">
              <span>Permissions</span>
              <select
                value={agent.permission}
                onChange={(event) =>
                  setAgent({
                    ...agent,
                    permission: event.target.value as Permission,
                  })
                }
              >
                <option value="ask">Ask</option>
                <option value="workspace">Workspace</option>
                <option value="full">Full access</option>
              </select>
            </label>
          </div>
          <div className="agent-resources">
            <span>{agent.skills.length} skills</span>
            <span>{agent.tools.length} tools</span>
            <span>{agent.mcp_servers.length} MCP</span>
          </div>
          <label className="check cascade-option">
            <input
              type="checkbox"
              checked={cascade}
              onChange={(event) => setCascade(event.target.checked)}
            />
            Apply to active descendants
          </label>
          <button
            className="button accent"
            disabled={!agentDirty}
            onClick={() =>
              mutate(
                `/api/nodes/${node.id}/edit`,
                json("POST", { agent, cascade_agent: cascade }),
                "Agent configuration updated",
              )
            }
          >
            Save agent
          </button>
        </section>
      )}
      {node.required_inputs.some((input) => !input.satisfied_by) && (
        <section className="section">
          <div className="section-heading">
            <span>Human input</span>
          </div>
          {node.required_inputs
            .filter((input) => !input.satisfied_by)
            .map((input) => (
              <div className="input-card" key={input.id}>
                <h3>{input.label}</h3>
                <p>
                  {input.description ||
                    "This node needs a human decision before it can continue."}
                </p>
                <textarea
                  aria-label={input.label}
                  value={inputs[input.id] ?? ""}
                  onChange={(event) =>
                    setInputs({ ...inputs, [input.id]: event.target.value })
                  }
                />
                <button
                  className="button accent"
                  disabled={!inputs[input.id]?.trim()}
                  onClick={() =>
                    mutate(
                      `/api/nodes/${node.id}/provide-input`,
                      json("POST", {
                        input_id: input.id,
                        value: inputs[input.id].trim(),
                      }),
                      "Input supplied",
                    )
                  }
                >
                  Provide input
                </button>
              </div>
            ))}
        </section>
      )}
      {(node.needs_review || node.verification_status) && (
        <Review
          node={node}
          feedback={feedback}
          setFeedback={setFeedback}
          mutate={mutate}
        />
      )}
      <section className="section">
        <div className="section-heading">
          <span>Artifacts</span>
        </div>
        <div className="artifact-list">
          {detail.artifacts
            .filter(
              (item) => item.name !== "transcript" && item.kind !== "code_diff",
            )
            .map((item) => (
              <details key={item.id} className="artifact-row">
                <summary>
                  <Icon name={item.kind === "file" ? "file" : "braces"} />
                  <span>{item.name}</span>
                </summary>
                {item.content ? (
                  <pre>
                    {typeof item.content === "string"
                      ? item.content
                      : JSON.stringify(item.content, null, 2)}
                  </pre>
                ) : null}
              </details>
            ))}
        </div>
      </section>
      <section className="section">
        <div className="section-heading">
          <span>Actions</span>
        </div>
        <div className="action-groups">
          <div className="action-group">
            <span>Node</span>
            <div className="action-row">
              {node.allowed_actions
                .filter((action) =>
                  ["run", "pause", "resume", "retry", "cancel"].includes(
                    action,
                  ),
                )
                .map((action) => (
                  <button
                    key={action}
                    className={`button compact ${action === "run" ? "accent" : action === "cancel" ? "danger" : ""}`}
                    onClick={() =>
                      mutate(
                        `/api/nodes/${node.id}/${action}`,
                        { method: "POST" },
                        `${action} requested`,
                      )
                    }
                  >
                    {action[0].toUpperCase() + action.slice(1)}
                  </button>
                ))}
              {!node.allowed_actions.some((action) =>
                ["run", "pause", "resume", "retry", "cancel"].includes(action),
              ) && <span className="empty-action">No direct action</span>}
            </div>
          </div>
          <div className="action-group">
            <span>Branch</span>
            <div className="action-row">
              <button
                className="button compact"
                onClick={() => setForking((value) => !value)}
              >
                Fork alternative
              </button>
              <button
                className="button compact"
                onClick={async () => {
                  if (
                    confirm(
                      "Restart this branch and supersede its active descendants?",
                    )
                  )
                    await mutate(
                      `/api/nodes/${node.id}/regenerate`,
                      { method: "POST" },
                      "Branch restarted",
                    );
                }}
              >
                Restart branch
              </button>
              <details className="branch-action-menu">
                <summary className="button compact">More</summary>
                <div className="popover branch-popover">
                  {["pause", "resume", "cancel"].map((action) => (
                    <button
                      key={`branch-${action}`}
                      onClick={() =>
                        mutate(
                          `/api/nodes/${node.id}/branch`,
                          json("POST", { action }),
                          `Branch ${action} requested`,
                        )
                      }
                    >
                      {action[0].toUpperCase() + action.slice(1)} branch
                    </button>
                  ))}
                </div>
              </details>
            </div>
          </div>
        </div>
        {forking && (
          <div className="inline-editor">
            <label className="field">
              <span>Alternative objective</span>
              <input
                value={forkObjective}
                onChange={(event) => setForkObjective(event.target.value)}
              />
            </label>
            <label className="field">
              <span>Planning instructions</span>
              <textarea
                value={forkPrompt}
                onChange={(event) => setForkPrompt(event.target.value)}
              />
            </label>
            <div className="action-row">
              <button
                className="button accent"
                disabled={!forkObjective.trim()}
                onClick={() =>
                  mutate(
                    `/api/nodes/${node.id}/fork`,
                    json("POST", {
                      objective: forkObjective.trim(),
                      generated_prompt: forkPrompt.trim() || null,
                    }),
                    "Alternative branch created",
                  )
                }
              >
                Create fork
              </button>
              <button className="button" onClick={() => setForking(false)}>
                Cancel
              </button>
            </div>
          </div>
        )}
      </section>
    </>
  );
}

function Review({
  node,
  feedback,
  setFeedback,
  mutate,
}: {
  node: GraphNode;
  feedback: string;
  setFeedback: (v: string) => void;
  mutate: (path: string, init?: RequestInit, message?: string) => Promise<void>;
}) {
  const parent = node.review_owner === "parent";
  const status = node.verification_status;
  if (parent && status === "accepted")
    return (
      <section className="section">
        <div className="section-heading">
          <span>Auto verification</span>
        </div>
        <div className="review-card verification-accepted">
          <h3>Accepted by parent</h3>
          <p>
            {node.verification_summary &&
            !/\b(awaiting|waiting)\b/i.test(node.verification_summary)
              ? node.verification_summary
              : "The parent verified and accepted this result."}
          </p>
        </div>
      </section>
    );
  if (!parent && node.merge_accepted) return null;
  const title = parent
    ? status === "running"
      ? "Parent is verifying"
      : status === "rejected"
        ? "Changes requested by parent"
        : "Awaiting parent verification"
    : "Review result";
  const summary =
    !parent &&
    /\b(parent|awaiting|waiting)\b/i.test(node.verification_summary ?? "")
      ? null
      : node.verification_summary;
  return (
    <section className="section">
      <div className="section-heading">
        <span>{parent ? "Auto verification" : "Review"}</span>
      </div>
      <div className={`review-card verification-${status ?? "pending"}`}>
        <h3>{title}</h3>
        <p>
          {summary ||
            (parent
              ? "The parent will inspect the result and focused checks."
              : "Accept the result or return feedback to the same agent session.")}
        </p>
        {!parent && node.allowed_actions.includes("accept") && (
          <div className="review-feedback">
            <textarea
              value={feedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="Feedback if changes are required…"
            />
            <div className="action-row">
              <button
                className="button accent"
                onClick={() =>
                  mutate(
                    `/api/nodes/${node.id}/accept`,
                    { method: "POST" },
                    "Result accepted",
                  )
                }
              >
                Accept result
              </button>
              <button
                className="button"
                disabled={!feedback.trim()}
                onClick={() =>
                  mutate(
                    `/api/nodes/${node.id}/reject`,
                    json("POST", { feedback }),
                    "Feedback returned to the same session",
                  )
                }
              >
                Request changes
              </button>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
function History({ detail }: { detail: Detail }) {
  return (
    <section className="section">
      <div className="section-heading">
        <span>Run history</span>
      </div>
      {[...detail.runs].reverse().map((run) => (
        <article className="history-item" key={run.id}>
          <strong>
            {run.worker} · attempt {run.attempt ?? 1}
          </strong>
          <small>{run.status}</small>
          {run.summary && <p>{run.summary}</p>}
        </article>
      ))}
    </section>
  );
}
