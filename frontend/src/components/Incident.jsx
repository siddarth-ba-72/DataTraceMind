// Incident page: anomaly -> evidence -> root cause -> patch -> staging proof ->
// human approval -> verified recovery, top to bottom.
import { useState } from "react";
import { api, useAction, usePoll } from "../api.js";
import { clock, dateTime, money, number, titleCase } from "../format.js";
import { Badge, Button, Card, Empty, ErrorNote, Json } from "./common.jsx";
import RepairReview from "./RepairReview.jsx";

const LIFECYCLE = [
  ["detected", "Detected"],
  ["investigating", "Investigating"],
  ["diagnosed", "Diagnosed"],
  ["repair_proposed", "Repair proposed"],
  ["validating", "Validating"],
  ["awaiting_approval", "Awaiting approval"],
  ["applying", "Applying"],
  ["verifying", "Verifying"],
  ["resolved", "Resolved"],
];
const RESTARTABLE = new Set([
  "detected", "diagnosed", "repair_proposed", "validation_failed", "awaiting_approval", "application_failed",
  "rejected", "escalated",
]);
const OFF_PATH = new Set(["validation_failed", "application_failed", "rejected", "escalated"]);

function Lifecycle({ incident }) {
  const visited = new Set(incident.events.filter((e) => e.event_type === "lifecycle").map((e) => e.status));
  return (
    <ol className="lifecycle" aria-label="Incident lifecycle">
      {LIFECYCLE.map(([status, label]) => {
        const state = incident.status === status ? "current" : visited.has(status) ? "done" : "todo";
        return (
          <li key={status} className={`step step-${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="step-dot" aria-hidden="true">
              {state === "done" ? "✓" : ""}
            </span>
            {label}
          </li>
        );
      })}
    </ol>
  );
}

function Trigger({ incident }) {
  const currencies = Object.keys(incident.source_metrics || {});
  return (
    <Card title="What the monitor found" subtitle={`Deterministic checks after ETL run ${incident.trigger.etl_run_id ?? "–"} (${incident.trigger.etl_status ?? "freshness monitor"}). No model decides that something is wrong.`}>
      {currencies.length > 0 && (
        <table className="table numeric">
          <thead>
            <tr>
              <th>Currency</th>
              <th>Source gross</th>
              <th>Source refunds</th>
              <th>Source net</th>
              <th>Analytics gross</th>
              <th>Analytics refunds</th>
              <th>Analytics net</th>
            </tr>
          </thead>
          <tbody>
            {currencies.map((currency) => {
              const source = incident.source_metrics[currency];
              const target = incident.target_metrics[currency];
              return (
                <tr key={currency}>
                  <td>{currency}</td>
                  <td>{money(source.gross, currency)}</td>
                  <td>{money(source.refunds, currency)}</td>
                  <td>{money(source.net, currency)}</td>
                  <td>{money(target.gross, currency)}</td>
                  <td>{money(target.refunds, currency)}</td>
                  <td>{money(target.net, currency)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <ul className="checks">
        {incident.trigger.failed_checks.map((check) => (
          <li key={check.check_name}>
            <details>
              <summary>
                <Badge value={check.severity}>{`Failed · ${check.severity}`}</Badge> <code>{check.check_name}</code>
                <span className="muted"> — expected: {check.expected_condition}</span>
              </summary>
              <Json value={check.observed_value} />
            </details>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function Timeline({ events }) {
  return (
    <ol className="timeline">
      {events.map((event) => (
        <li key={event.event_id} className={`event event-${event.event_type}`}>
          <span className="event-time">{clock(event.created_at)}</span>
          <span className="event-agent">{titleCase(event.agent.replace(/^human:/, "human · "))}</span>
          <span className="event-body">
            <Badge value={event.status} />{" "}
            {event.event_type === "tool_call" && <span className="tag">tool</span>}
            {event.payload?.evidence_id && <span className="tag">{event.payload.evidence_id}</span>} {event.summary}
            {event.payload?.input_tokens !== undefined && (
              <span className="muted">
                {" "}
                · {number(event.payload.input_tokens)} in / {number(event.payload.output_tokens)} out tokens · est. $
                {event.payload.est_cost_usd}
              </span>
            )}
          </span>
        </li>
      ))}
    </ol>
  );
}

function Evidence({ evidence, cited }) {
  if (evidence.length === 0) return <Empty>No evidence collected yet.</Empty>;
  return (
    <ul className="evidence">
      {evidence.map((item) => (
        <li key={item.evidence_id} id={item.evidence_id} className={cited.has(item.evidence_id) ? "evidence-cited" : ""}>
          <details>
            <summary>
              <span className="tag tag-strong">{item.evidence_id}</span> {item.description}
              <span className="muted">
                {" "}
                · {item.query_or_tool} · {item.source} · by {titleCase(item.collected_by)}
              </span>
              {cited.has(item.evidence_id) && <span className="tag">cited in root cause</span>}
            </summary>
            {Object.keys(item.arguments).length > 0 && (
              <p className="muted">
                Arguments: <code>{JSON.stringify(item.arguments)}</code>
              </p>
            )}
            <Json value={item.result_summary?.content ?? item.result_summary?.text ?? item.result_summary} />
          </details>
        </li>
      ))}
    </ul>
  );
}

function Hypotheses({ state }) {
  const hypotheses = state.hypotheses || [];
  if (hypotheses.length === 0) return null;
  const selected = state.selected_root_cause;
  return (
    <Card
      title="Root cause"
      subtitle="Competing hypotheses ranked by the analyst. A cause counts as confirmed only when it cites evidence that exists and clears the confidence bar."
      actions={selected ? <Badge tone="good">Confirmed</Badge> : <Badge tone="warning">Not confirmed</Badge>}
    >
      {selected && (
        <div className="root-cause">
          <p>{selected.summary}</p>
          <p className="muted">
            Implicated: <code>{selected.implicated_component}</code>
          </p>
        </div>
      )}
      {state.analysis?.business_rule_ambiguity && (
        <p className="note note-error">Needs a human decision: {state.analysis.business_rule_ambiguity}</p>
      )}
      <ol className="hypotheses">
        {hypotheses.map((h) => (
          <li key={h.hypothesis_id} className={selected?.hypothesis_id === h.hypothesis_id ? "hypothesis-selected" : ""}>
            <div className="hypothesis-head">
              <span className="tag tag-strong">{h.hypothesis_id}</span>
              <span className="tag">{titleCase(h.category)}</span>
              <span className="meter" role="img" aria-label={`Confidence ${Math.round(h.confidence * 100)} percent`}>
                <span style={{ width: `${Math.round(h.confidence * 100)}%` }} />
              </span>
              <span className="muted">{Math.round(h.confidence * 100)}% confidence</span>
            </div>
            <p>{h.statement}</p>
            <p className="muted">{h.reasoning}</p>
            <p className="evidence-links">
              {h.supporting_evidence.length > 0 && <span className="muted">Supported by </span>}
              {h.supporting_evidence.map((id) => (
                <a key={id} className="tag" href={`#${id}`} onClick={(e) => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "center" }); }}>
                  {id}
                </a>
              ))}
              {h.contradicting_evidence.length > 0 && <span className="muted"> Contradicted by </span>}
              {h.contradicting_evidence.map((id) => (
                <span key={id} className="tag">
                  {id}
                </span>
              ))}
            </p>
          </li>
        ))}
      </ol>
    </Card>
  );
}

function Runs({ runs }) {
  if (runs.length === 0) return null;
  return (
    <Card title="Investigation runs" subtitle="One row per run; each is one neatlogs trace. Cost is an estimate at API list prices (the model runs on a flat-rate plan).">
      <div className="table-scroll">
        <table className="table numeric">
          <thead>
            <tr>
              <th>Run</th>
              <th>Outcome</th>
              <th>Workflow</th>
              <th>Model</th>
              <th>Duration</th>
              <th>Role turns</th>
              <th>Tool calls</th>
              <th>Failed / retries</th>
              <th>Input tokens</th>
              <th>Output tokens</th>
              <th>Est. cost</th>
              <th>Trace ID</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td>#{run.run_id}</td>
                <td>
                  <Badge value={run.status} />
                </td>
                <td>{run.workflow_version}</td>
                <td>{run.model}</td>
                <td>{run.duration_s ? `${Number(run.duration_s).toFixed(0)}s` : "–"}</td>
                <td>{run.role_turns}</td>
                <td>{run.tool_calls}</td>
                <td>
                  {run.failed_tool_calls} / {run.retries}
                </td>
                <td title={`${number(run.cached_input_tokens)} cached`}>{number(run.input_tokens)}</td>
                <td>{number(run.output_tokens)}</td>
                <td>${Number(run.est_cost_usd).toFixed(3)}</td>
                <td>
                  <code>{run.trace_id ? `${run.trace_id.slice(0, 12)}…` : "not traced"}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function IncidentDetail({ incidentId, agent, onChanged }) {
  const { data: incident, error, refresh } = usePoll(`/incidents/${incidentId}`, 2000);
  const action = useAction();
  const [showTools, setShowTools] = useState(true);
  if (error && !incident) return <ErrorNote message={error.message} />;
  if (!incident) return <Empty>Loading {incidentId}…</Empty>;

  const changed = async () => {
    await refresh();
    onChanged();
  };
  const investigate = () =>
    action.run(async () => {
      await api(`/incidents/${incidentId}/investigate`, { method: "POST" });
      await changed();
    });
  const cited = new Set(incident.state.selected_root_cause?.supporting_evidence || []);
  const events = showTools ? incident.events : incident.events.filter((e) => e.event_type !== "tool_call");
  const escalation = incident.state.errors?.at(-1);
  const canInvestigate = RESTARTABLE.has(incident.status);

  return (
    <div className="stack">
      <Card>
        <div className="incident-head">
          <div>
            <h1>
              {incident.incident_id} <Badge value={incident.severity} /> <Badge value={incident.status} />
            </h1>
            <p className="incident-summary">{incident.summary}</p>
            <p className="muted">
              {titleCase(incident.incident_type)} · detected {dateTime(incident.detected_at)}
              {incident.resolved_at ? ` · resolved ${dateTime(incident.resolved_at)}` : ""}
            </p>
          </div>
          {canInvestigate && (
            <div className="incident-actions">
              <Button
                kind="primary"
                onClick={investigate}
                busy={action.busy}
                disabled={!agent?.available || agent?.investigation_running}
                title={agent?.available ? undefined : agent?.detail}
              >
                {incident.status === "detected" ? "Investigate" : "Investigate again"}
              </Button>
              {!agent?.available && <p className="muted">Agent unavailable. {agent?.detail}</p>}
            </div>
          )}
        </div>
        <ErrorNote message={action.error} onDismiss={action.clearError} />
        <Lifecycle incident={incident} />
        {OFF_PATH.has(incident.status) && (
          <p className="note note-error">
            <strong>{titleCase(incident.status)}.</strong>{" "}
            {incident.status === "escalated" && escalation
              ? `A human needs to take this: ${escalation.reason}`
              : incident.events.at(-1)?.summary}
          </p>
        )}
      </Card>

      <Trigger incident={incident} />
      <Hypotheses state={incident.state} />
      <RepairReview incident={incident} onChanged={changed} />

      <Card
        title="Agent workflow"
        subtitle="Every step the supervisor and the agent roles took, in order."
        actions={
          <label className="field-inline">
            <input type="checkbox" checked={showTools} onChange={(event) => setShowTools(event.target.checked)} /> Show tool calls
          </label>
        }
      >
        <Timeline events={events} />
      </Card>

      <Card title={`Evidence · ${incident.evidence.length} items`} subtitle="Each item is the recorded result of a tool call, a contract, or code as deployed. Open one to see exactly what was returned.">
        <Evidence evidence={incident.evidence} cited={cited} />
      </Card>

      <Runs runs={incident.investigation_runs} />
    </div>
  );
}

export default function Incidents({ selectedId, onSelect, agent, onChanged }) {
  const { data: incidents } = usePoll("/incidents", 3000);
  if (!incidents) return <Empty>Loading incidents…</Empty>;
  if (incidents.length === 0)
    return (
      <Card title="Incidents">
        <Empty>No incidents. The pipeline is healthy; inject a fault from the Scenario Lab to see one.</Empty>
      </Card>
    );
  const current = selectedId && incidents.some((i) => i.incident_id === selectedId) ? selectedId : incidents[0].incident_id;
  return (
    <div className="stack">
      {incidents.length > 1 && (
        <nav className="incident-tabs" aria-label="Incidents">
          {incidents.map((incident) => (
            <button
              key={incident.incident_id}
              type="button"
              className={incident.incident_id === current ? "active" : ""}
              onClick={() => onSelect(incident.incident_id)}
            >
              {incident.incident_id} <Badge value={incident.status} />
            </button>
          ))}
        </nav>
      )}
      <IncidentDetail key={current} incidentId={current} agent={agent} onChanged={onChanged} />
    </div>
  );
}
