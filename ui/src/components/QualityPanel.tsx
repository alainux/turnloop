import { useEffect, useMemo, useState } from "react";
import {
  getBehaviorDashboard,
  getProjectBehavior,
  type BehaviorDashboardResponse,
  type BehaviorMetrics,
  type BehaviorRunMetrics,
  type ProjectBehaviorResponse,
} from "../api/behavior";
import { Icon } from "./Icon";

function count(metrics: BehaviorMetrics, numerator: keyof BehaviorMetrics, denominator: keyof BehaviorMetrics) {
  const top = Number(metrics[numerator] ?? 0);
  const bottom = Number(metrics[denominator] ?? 0);
  return bottom ? `${Math.round((top / bottom) * 100)}%` : "—";
}

function compact(value: number, digits = 0) {
  return value ? value.toLocaleString(undefined, { maximumFractionDigits: digits }) : "0";
}

function toolCalls(metrics: BehaviorMetrics) {
  return Object.entries(metrics.dynamic_usage)
    .filter(([key]) => key.startsWith("tool:"))
    .reduce((total, [, value]) => total + Number(value || 0), 0);
}

function Trend({ label, value }: { label: string; value: string }) {
  return <div className="quality-trend"><span>{label}</span><strong>{value}</strong></div>;
}

function runTime(value: string | null) {
  if (!value) return "Live";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function runLabel(run: BehaviorRunMetrics) {
  return `${run.role ?? "agent"} · attempt ${run.attempt ?? "—"}`;
}

export function QualityPanel({
  projectId,
  onClose,
  onOpenLogs,
  onSelectNode,
}: {
  projectId: string;
  onClose: () => void;
  onOpenLogs: () => void;
  onSelectNode: (nodeId: string) => void;
}) {
  const [project, setProject] = useState<ProjectBehaviorResponse | null>(null);
  const [dashboard, setDashboard] = useState<BehaviorDashboardResponse | null>(null);
  const [role, setRole] = useState("");
  const [harness, setHarness] = useState("");
  const [model, setModel] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  useEffect(() => {
    let active = true;
    const load = () => {
      void getProjectBehavior(projectId)
        .then((value) => { if (active) setProject(value); })
        .catch(() => { if (active) setProject(null); });
    };
    load();
    // Metrics are a projection of the existing structured event/log stream.
    // A short refresh makes a running attempt legible without introducing a
    // second live-observability channel.
    const interval = window.setInterval(load, 1200);
    return () => { active = false; window.clearInterval(interval); };
  }, [projectId]);
  useEffect(() => {
    void getBehaviorDashboard({ role, harness, model, date_from: dateFrom, date_to: dateTo })
      .then(setDashboard)
      .catch(() => setDashboard(null));
  }, [role, harness, model, dateFrom, dateTo]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    addEventListener("keydown", key);
    return () => removeEventListener("keydown", key);
  }, [onClose]);

  const metrics = project?.project;
  const rows = useMemo(() => Object.entries(project?.by_node ?? {}), [project]);
  const runs = useMemo(
    () => Object.values(project?.by_run ?? {}).sort((a, b) =>
      String(b.started_at ?? b.first_observed_at ?? "").localeCompare(String(a.started_at ?? a.first_observed_at ?? "")),
    ),
    [project],
  );
  const recent = dashboard?.projects ?? [];
  return (
    <section className="logs-panel quality-panel" role="dialog" aria-modal="true" aria-labelledby="quality-title">
      <header className="logs-head">
        <div><p className="eyebrow">Observed behavior</p><h2 id="quality-title">Quality / Behavior</h2></div>
        <div className="logs-head-actions">
          <button className="quiet-icon" onClick={onClose} aria-label="Close quality dashboard" title="Close"><Icon name="x" /></button>
        </div>
      </header>
      <div className="quality-filter" aria-label="Behavior filters">
        <select value={role} onChange={(event) => setRole(event.target.value)}><option value="">All roles</option><option value="setup">Setup</option><option value="planner">Planner</option><option value="executor">Executor</option><option value="integrator">Integrator</option><option value="verifier">Verifier</option></select>
        <input value={harness} onChange={(event) => setHarness(event.target.value)} placeholder="Harness" aria-label="Filter harness" />
        <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="Model" aria-label="Filter model" />
        <input value={dateFrom} onChange={(event) => setDateFrom(event.target.value)} type="date" aria-label="From date" />
        <input value={dateTo} onChange={(event) => setDateTo(event.target.value)} type="date" aria-label="To date" />
      </div>
      <div className="quality-content">
      {!metrics ? <div className="logs-empty">No behavior evidence has been recorded yet.</div> : <>
        <div className="quality-trends">
          <Trend label="Docs before action" value={count(metrics, "docs_before_action_successes", "docs_before_action_runs")} />
          <Trend label="Skills" value={compact(metrics.skills_accessed)} />
          <Trend label="Tool / MCP / web" value={`${compact(toolCalls(metrics))} / ${compact(metrics.mcp_calls)} / ${compact(metrics.web_searches)}`} />
          <Trend label="Verify after change" value={count(metrics, "verification_after_change_successes", "verification_after_change_runs")} />
          <Trend label="Errors / retries / recovery" value={`${compact(metrics.errors)} / ${compact(metrics.retries)} / ${compact(metrics.recovery_actions)}`} />
          <Trend label="Repeated failures" value={compact(metrics.repeated_failed_actions)} />
          <Trend label="Graph churn" value={compact(metrics.graph_changes)} />
          <Trend label="Verifier accept / reject" value={`${compact(metrics.role_metrics.accepts ?? 0)} / ${compact(metrics.role_metrics.rejections ?? 0)}`} />
          <Trend label="Harness failures" value={compact(metrics.harness_failures)} />
          <Trend label="Duration" value={`${compact(metrics.duration_seconds / 60, 1)}m`} />
          <Trend label="Tokens (cache)" value={`${compact(metrics.input_tokens + metrics.output_tokens)} (${compact(metrics.cached_input_tokens)})`} />
          <Trend label="Cost" value={`$${compact(metrics.cost_usd, 2)}`} />
          <Trend label="Actions" value={compact(metrics.actions)} />
        </div>
        {Object.keys(project?.expectations ?? {}).length > 0 && <div className="quality-expectations">
          Expectations: {Object.entries(project!.expectations).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value ? "met" : "not met"}`).join(" · ")}
        </div>}
        <div className="quality-section-head"><span>Current project agents</span><button onClick={onOpenLogs}>Evidence in logs</button></div>
        <div className="quality-agent-list">
          {rows.length === 0 && <div className="logs-empty">Structured harness telemetry will appear here as agents run.</div>}
          {rows.map(([nodeId, row]) => <details key={nodeId} className="quality-agent">
            <summary><span>{row.role ?? "agent"}</span><span>{row.harness ?? "—"}</span><span>{compact(row.actions)} actions</span><span>{compact(row.errors)} errors</span><span>{compact(row.input_tokens + row.output_tokens)} tokens</span></summary>
            <div className="quality-agent-actions"><button onClick={() => onSelectNode(nodeId)}>Graph / runs</button><button onClick={onOpenLogs}>Logs</button></div>
            <pre>{JSON.stringify(row, null, 2)}</pre>
          </details>)}
        </div>
        <div className="quality-section-head"><span>Individual runs</span><small>{runs.length} recorded</small></div>
        <div className="quality-run-list">
          {runs.length === 0 && <div className="logs-empty">A row appears as soon as Turn launches a harness run.</div>}
          {runs.map((run) => <details key={run.run_id} className="quality-run">
            <summary>
              <span>{runLabel(run)}</span>
              <span>{run.status ?? "RUNNING"}{run.outcome ? ` · ${run.outcome}` : ""}</span>
              <span>{compact(run.actions)} actions</span>
              <span>{compact(run.errors)} errors</span>
              <span>{compact(run.input_tokens + run.output_tokens)} tokens</span>
            </summary>
            <div className="quality-run-meta">
              <span>Started {runTime(run.started_at ?? run.first_observed_at)}</span>
              <span>{run.ended_at ? `Finished ${runTime(run.ended_at)}` : "Still running"}</span>
              <span>{compact(run.duration_seconds, 1)}s · ${compact(run.cost_usd, 2)}</span>
            </div>
            <div className="quality-agent-actions">
              {run.node_id && <button onClick={() => onSelectNode(run.node_id!)}>Graph / terminal</button>}
              <button onClick={onOpenLogs}>Evidence in logs</button>
            </div>
            {run.qualitative_assessments.length > 0 && <div className="quality-assessments">
              Qualitative assessments: {run.qualitative_assessments.map((item) => String(item.name ?? "assessment")).join(" · ")}
            </div>}
            <pre>{JSON.stringify(run, null, 2)}</pre>
          </details>)}
        </div>
        <div className="quality-section-head"><span>Recent projects</span><small>{recent.length} matching</small></div>
        <div className="quality-recent">{recent.map((entry) => <div key={entry.project_id}><span>{entry.project_name}</span><span>{compact(entry.metrics.actions)} actions</span><span>{compact(entry.metrics.errors)} errors</span><span>${compact(entry.metrics.cost_usd, 2)}</span></div>)}</div>
      </>}
      </div>
    </section>
  );
}
