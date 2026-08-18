import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type {
  Agent,
  HarnessCapability,
  NodeDetail as Detail,
  Reasoning,
} from "../domain";
import {
  displayNodeTitle,
  capabilityCatalogHref,
  capabilityDeploymentLabel,
  primaryNodeAction,
  primaryNodeActionIcon,
  primaryNodeActionLabel,
} from "../domain";
import { editNode, getNodeDetail, provideNodeInput, runNodeAction } from "../api/nodes";
import { Icon } from "./Icon";
import { ModelControl } from "./ModelControl";
const TerminalView = lazy(() =>
  import("./TerminalView").then((module) => ({ default: module.TerminalView })),
);

type Tab = "overview" | "terminal";
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
  const [terminalVisited, setTerminalVisited] = useState(false);
  const [error, setError] = useState("");
  const dirty = useRef(false);
  const loadVersion = useRef(0);
  const load = async () => {
    const version = ++loadVersion.current;
    try {
      const result = await getNodeDetail(nodeId);
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
  const mutate = async (
    operation: () => Promise<void>,
    message?: string,
  ) => {
    try {
      await operation();
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
        {(["overview", "terminal"] as Tab[]).map((item) => (
          <button
            key={item}
            role="tab"
            aria-selected={tab === item}
            className={tab === item ? "active" : ""}
            onClick={() => {
              if (item === "terminal") setTerminalVisited(true);
              setTab(item);
            }}
          >
            {item[0].toUpperCase() + item.slice(1)}
          </button>
        ))}
      </div>
      <div className={`detail ${tab === "terminal" ? "terminal-detail" : ""}`}>
        {error ? (
          <p className="detail-error">{error}</p>
        ) : !detail ? (
          <p className="detail-loading">Loading node…</p>
        ) : tab === "overview" ? (
          <Overview
            detail={detail}
            capabilities={capabilities}
            mutate={mutate}
            onDirtyChange={(value) => {
              dirty.current = value;
            }}
          />
        ) : null}
        {detail && terminalVisited && (
          <div hidden={tab !== "terminal"} className="terminal-tab-panel">
            <Suspense fallback={<p className="detail-loading">Loading terminal…</p>}>
              <TerminalView node={detail.node} runs={detail.runs} />
            </Suspense>
          </div>
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
  mutate: (operation: () => Promise<void>, message?: string) => Promise<void>;
  onDirtyChange: (value: boolean) => void;
}) {
  const node = detail.node;
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [prompt, setPrompt] = useState(node.generated_prompt ?? "");
  const [objective, setObjective] = useState(node.objective);
  const [agent, setAgent] = useState<Agent | null>(node.agent ?? null);
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const primaryAction = primaryNodeAction(node);
  const freshRun = node.ui_state === "cancelled";
  const capabilityStatus = node.capability_status ?? [];
  useEffect(() => {
    setPrompt(node.generated_prompt ?? "");
    setObjective(node.objective);
    setAgent(node.agent ?? null);
    setEditingPrompt(false);
  }, [node.id]);
  const scopeDirty =
    objective !== node.objective || prompt !== (node.generated_prompt ?? "");
  const agentDirty = JSON.stringify(agent) !== JSON.stringify(node.agent);
  useEffect(() => {
    onDirtyChange(
      editingPrompt ||
        scopeDirty ||
        agentDirty ||
        Object.values(inputs).some(Boolean),
    );
    return () => onDirtyChange(false);
  }, [
    editingPrompt,
    scopeDirty,
    agentDirty,
    inputs,
    onDirtyChange,
  ]);
  return (
    <>
      <h2 className="detail-title">{displayNodeTitle(node)}</h2>
      <div className="detail-meta">
        <span className={`badge ${node.ui_state}`}>
          {node.ui_state === "preparing"
            ? "preparing"
            : node.generation_active
              ? "generating"
              : node.ui_state.replaceAll("_", " ")}
        </span>
      </div>
      {node.verification && (
        <section className="section verification-section">
          <div className="section-heading">
            <span>Verification</span>
            <span className={`badge ${node.verification.decision === "APPROVE" ? "complete" : "failed"}`}>
              {node.verification.decision.toLowerCase()}
            </span>
          </div>
          <p className="verification-summary">{node.verification.summary}</p>
          {node.verification.findings.length > 0 && (
            <ul className="verification-findings">
              {node.verification.findings.map((finding) => <li key={finding}>{finding}</li>)}
            </ul>
          )}
          {node.verification.required_changes.length > 0 && (
            <ul className="verification-findings">
              {node.verification.required_changes.map((change) => <li key={change}>{change}</li>)}
            </ul>
          )}
        </section>
      )}
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
            <label className="field instruction-field">
              <span>Prompt</span>
              <textarea
                className="instruction-editor"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
              />
            </label>
            <button
              className="button accent"
              disabled={!scopeDirty}
              onClick={() =>
                mutate(
                  () => editNode(node.id, {
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
          </div>
          <div className="agent-resources">
            <span>{agent.tools.length} tools</span>
          </div>
          <section className="capability-section" aria-label="Capabilities">
            <div className="section-heading"><span>Capabilities</span></div>
            {capabilityStatus.length > 0 ? (
              <div className="capability-table" role="table">
                <div className="capability-row capability-header" role="row">
                  <span role="columnheader">Plugin</span>
                  <span role="columnheader">Skills</span>
                  <span role="columnheader">MCP</span>
                </div>
                {capabilityStatus.map((item) => {
                  const state = capabilityDeploymentLabel(item);
                  return (
                    <a
                      className={`capability-row capability-${state.replace(" ", "-")}`}
                      href={capabilityCatalogHref(item.capability_id)}
                      target="_blank"
                      rel="noreferrer"
                      key={item.capability_id}
                      role="row"
                      title={`${item.capability_id}: ${state}`}
                    >
                      <span role="cell" className="capability-plugin">
                        <Icon name="plug" />
                        <span>{item.capability_id}</span>
                        <span
                          className="capability-state"
                          title={state}
                          aria-label={state}
                        >
                          <Icon name={item.installed ? "check" : item.loaded ? "loader" : "x"} />
                        </span>
                      </span>
                      <span role="cell">{item.skills}</span>
                      <span role="cell">{item.mcps}</span>
                    </a>
                  );
                })}
              </div>
            ) : <p className="detail-empty">No capabilities assigned.</p>}
          </section>
          <button
            className="button accent capability-save"
            disabled={!agentDirty}
            onClick={() =>
              mutate(
                () => editNode(node.id, { agent: agent ?? undefined }),
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
                      () => provideNodeInput(
                        node.id,
                        input.id,
                        inputs[input.id].trim(),
                      ),
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
      <section className="section">
        <div className="section-heading">
          <span>Artifacts</span>
        </div>
        <div className="artifact-list">
          {detail.artifacts
            .filter(
              (item) =>
                item.name !== "transcript" &&
                item.kind !== "code_diff" &&
                !item.name.startsWith("revision-"),
            )
            .map((item) => {
              const hasContent =
                item.content !== null &&
                item.content !== undefined &&
                item.content !== "";
              // Older runs called this artifact a transcript. When the
              // persisted node has a structured verdict, show that submitted
              // result rather than exposing the unreadable PTY capture.
              const isVerification =
                item.name === "verification-result" ||
                item.name === "verification-transcript";
              const content =
                isVerification && detail.node.verification
                  ? JSON.stringify(detail.node.verification, null, 2)
                  : typeof item.content === "string"
                    ? item.content
                    : JSON.stringify(item.content, null, 2);
              const name = isVerification ? "verification-result" : item.name;
              const summary = (
                <div className="artifact-summary">
                  <Icon name={item.kind === "file" ? "file" : "braces"} />
                  <span>{name}</span>
                </div>
              );
              return hasContent ? (
                <details key={item.id} className="artifact-row">
                  <summary>{summary}</summary>
                  <pre>{content}</pre>
                </details>
              ) : (
                <div key={item.id} className="artifact-row artifact-row-static">
                  {summary}
                </div>
              );
            })}
        </div>
      </section>
      <section className="section">
        <div className="section-heading">
          <span>Actions</span>
        </div>
        <div className="action-groups">
          <div className="action-group">
            <div className="action-row">
              {primaryAction ? (
                <button
                  className={`button compact ${primaryAction === "cancel" ? "danger stop-action" : "accent"}`}
                  onClick={async () => {
                    if (
                      primaryAction === "regenerate" &&
                      !confirm(
                        "Run this planner again and replace its entire descendant tree? This cannot be undone.",
                      )
                    )
                      return;
                    await mutate(
                      () => runNodeAction(node.id, primaryAction),
                      primaryAction === "cancel"
                        ? "Node stopped"
                        : `${primaryNodeActionLabel(primaryAction, freshRun)} started`,
                    );
                  }}
                >
                  <Icon name={primaryNodeActionIcon(primaryAction, freshRun)} />
                  {primaryNodeActionLabel(primaryAction, freshRun)}
                </button>
              ) : (
                <span className="empty-action">No direct action</span>
              )}
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
