// Incidents: the list, and one incident's page. The page reads top to bottom:
// anomaly -> evidence -> root cause -> patch -> staging proof -> human approval
// -> verified recovery.
import { useState } from "react";
import { api, useAction, usePoll } from "../api.js";
import { between, clock, dateTime, money, number, signedMoney, since, titleCase } from "../format.js";
import { Badge, Button, Card, Empty, ErrorNote, Facts, Json, PageHead, Segmented } from "./common.jsx";
import { Icon } from "./icons.jsx";
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
  const position = LIFECYCLE.findIndex(([status]) => status === incident.status);
  return (
    <div className="lifecycle-scroll">
      <ol className="lifecycle" aria-label="Incident lifecycle">
        {LIFECYCLE.map(([status, label]) => {
          const state = incident.status === status ? "current" : visited.has(status) ? "done" : "todo";
          return (
            <li key={status} className={`step step-${state}`} aria-current={state === "current" ? "step" : undefined}>
              <span className="step-dot" aria-hidden="true">
                {state === "done" ? "✓" : ""}
              </span>
              <span className="step-label">{label}</span>
            </li>
          );
        })}
      </ol>
      {/* Shown only where the row is too narrow to label every step. */}
      {position >= 0 && (
        <p className="lifecycle-caption" aria-hidden="true">
          {LIFECYCLE[position][1]}{" "}
          <span>
            · step {position + 1} of {LIFECYCLE.length}
          </span>
        </p>
      )}
    </div>
  );
}

const MEASURES = [
  ["gross", "Gross paid"],
  ["refunds", "Refunds subtracted"],
  ["net", "Net revenue"],
];

// Source against analytics as the monitor recorded them, one measure per row.
function TriggerMetrics({ incident }) {
  const currencies = Object.keys(incident.source_metrics || {});
  if (currencies.length === 0) return null;
  return (
    <div className="table-scroll">
      <table className="table table-inset numeric">
        <thead>
          <tr>
            <th>Currency</th>
            <th>Measure</th>
            <th className="right">Source-derived</th>
            <th className="right">Analytics</th>
            <th className="right">Difference</th>
          </tr>
        </thead>
        <tbody>
          {currencies.flatMap((currency) =>
            MEASURES.map(([key, label], index) => {
              const source = incident.source_metrics[currency][key];
              const target = incident.target_metrics[currency][key];
              const gap = target - source;
              const differs = Math.abs(gap) > 0.005;
              return (
                <tr key={`${currency}-${key}`} className={key === "net" ? "row-strong" : ""}>
                  <td>{index === 0 ? currency : ""}</td>
                  <td>{label}</td>
                  <td className="right">{money(source, currency)}</td>
                  <td className="right">{money(target, currency)}</td>
                  <td className="right">
                    {differs ? <span className="differs">{signedMoney(gap, currency)}</span> : <span className="muted">matches</span>}
                  </td>
                </tr>
              );
            }),
          )}
        </tbody>
      </table>
    </div>
  );
}

function Trigger({ incident }) {
  return (
    <Card
      id="detection"
      title="What the monitor found"
      subtitle={`Deterministic checks after ETL run ${incident.trigger.etl_run_id ?? "–"} (${incident.trigger.etl_status ?? "freshness monitor"}). No model decides that something is wrong.`}
    >
      <TriggerMetrics incident={incident} />
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
            {event.payload?.evidence_id && <span className="tag tag-strong">{event.payload.evidence_id}</span>} {event.summary}
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
      id="root-cause"
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
                <a
                  key={id}
                  className="tag tag-strong"
                  href={`#${id}`}
                  onClick={(e) => {
                    e.preventDefault();
                    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "center" });
                  }}
                >
                  {id}
                </a>
              ))}
              {h.contradicting_evidence.length > 0 && <span className="muted"> Contradicted by </span>}
              {h.contradicting_evidence.map((id) => (
                <span key={id} className="tag tag-strong">
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
    <Card
      id="runs"
      title="Investigation runs"
      subtitle="One row per run; each is one neatlogs trace. Cost is an estimate at API list prices (the model runs on a flat-rate plan)."
      flush
    >
      <div className="table-scroll">
        <table className="table numeric">
          <thead>
            <tr>
              <th>Run</th>
              <th>Outcome</th>
              <th>Workflow</th>
              <th>Model</th>
              <th className="right">Duration</th>
              <th className="right">Role turns</th>
              <th className="right">Tool calls</th>
              <th className="right">Failed / retries</th>
              <th className="right">Input tokens</th>
              <th className="right">Output tokens</th>
              <th className="right">Est. cost</th>
              <th>Trace ID</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td>
                  <span className="id">#{run.run_id}</span>
                </td>
                <td>
                  <Badge value={run.status} />
                </td>
                <td>{run.workflow_version}</td>
                <td>{run.model}</td>
                <td className="right">{run.duration_s ? `${Number(run.duration_s).toFixed(0)}s` : "–"}</td>
                <td className="right">{run.role_turns}</td>
                <td className="right">{run.tool_calls}</td>
                <td className="right">
                  {run.failed_tool_calls} / {run.retries}
                </td>
                <td className="right" title={`${number(run.cached_input_tokens)} cached`}>
                  {number(run.input_tokens)}
                </td>
                <td className="right">{number(run.output_tokens)}</td>
                <td className="right">${Number(run.est_cost_usd).toFixed(3)}</td>
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

// Facts about the incident and its latest investigation, beside the narrative.
function Aside({ incident }) {
  const trigger = incident.trigger;
  const run = incident.investigation_runs.at(-1);
  const sections = [
    ["detection", "Detection", `${trigger.failed_checks.length} failed checks`],
    (incident.state.hypotheses || []).length > 0 && ["root-cause", "Root cause", `${incident.state.hypotheses.length} hypotheses`],
    incident.proposal && ["repair", "Repair review", titleCase(incident.proposal.status)],
    ["workflow", "Agent workflow", `${incident.events.length} events`],
    ["evidence", "Evidence", `${incident.evidence.length} items`],
    incident.investigation_runs.length > 0 && ["runs", "Investigation runs", String(incident.investigation_runs.length)],
  ].filter(Boolean);
  return (
    <aside className="aside">
      <Card title="Incident details">
        <Facts
          rows={[
            ["Type", titleCase(incident.incident_type)],
            ["Severity", <Badge key="s" value={incident.severity} />],
            ["Detected", dateTime(incident.detected_at)],
            ["Detected by", titleCase(trigger.detected_by)],
            ["ETL run", trigger.etl_run_id ? `#${trigger.etl_run_id} · ${trigger.etl_status}` : null],
            ["Transformation", trigger.transform_version ? `Version ${trigger.transform_version}` : null],
            ["Resolved", incident.resolved_at ? dateTime(incident.resolved_at) : null],
            ["Time to resolve", incident.resolved_at ? between(incident.detected_at, incident.resolved_at) : null],
          ]}
        />
      </Card>
      {run && (
        <Card title="Latest investigation">
          <Facts
            rows={[
              // A finished run is labelled by where it stopped; a running one has not stopped.
              [run.status === "running" ? "Status" : "Ended at", <Badge key="o" value={run.status} />],
              ["Model", run.model],
              ["Workflow", run.workflow_version],
              ["Duration", run.duration_s ? `${Number(run.duration_s).toFixed(0)}s` : null],
              ["Role turns", run.role_turns],
              ["Tool calls", run.tool_calls],
              ["Est. cost", `$${Number(run.est_cost_usd).toFixed(3)}`],
            ]}
          />
        </Card>
      )}
      <Card title="On this page">
        <ul className="section-nav">
          {sections.map(([id, label, detail]) => (
            <li key={id}>
              <a
                href={`#${id}`}
                onClick={(event) => {
                  // The hash holds the route, so scroll instead of navigating.
                  event.preventDefault();
                  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
              >
                {label} <span className="muted">{detail}</span>
              </a>
            </li>
          ))}
        </ul>
      </Card>
    </aside>
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
      <section className="card incident-hero">
        <div className="incident-head">
          <div>
            <h1>
              <span className="id">{incident.incident_id}</span> <Badge value={incident.severity} /> <Badge value={incident.status} />
            </h1>
            <p className="incident-summary">{incident.summary}</p>
            <p className="incident-meta">
              <span>{titleCase(incident.incident_type)}</span>
              <span>Detected {dateTime(incident.detected_at)}</span>
              <span>
                {incident.resolved_at
                  ? `Resolved in ${between(incident.detected_at, incident.resolved_at)}`
                  : `Open for ${since(incident.detected_at).replace(" ago", "")}`}
              </span>
            </p>
          </div>
          {canInvestigate && (
            <div className="incident-actions">
              <Button
                kind="primary"
                size="large"
                icon="zap"
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
            <span>
              <strong>{titleCase(incident.status)}.</strong>{" "}
              {incident.status === "escalated" && escalation
                ? `A human needs to take this: ${escalation.reason}`
                : incident.events.at(-1)?.summary}
            </span>
          </p>
        )}
      </section>

      <div className="detail-grid">
        <div className="stack">
          <Trigger incident={incident} />
          <Hypotheses state={incident.state} />
          <RepairReview incident={incident} onChanged={changed} />

          <Card
            id="workflow"
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

          <Card
            id="evidence"
            title={`Evidence · ${incident.evidence.length} items`}
            subtitle="Each item is the recorded result of a tool call, a contract, or code as deployed. Open one to see exactly what was returned."
          >
            <Evidence evidence={incident.evidence} cited={cited} />
          </Card>

          <Runs runs={incident.investigation_runs} />
        </div>
        <Aside incident={incident} />
      </div>
    </div>
  );
}

const FILTERS = {
  all: () => true,
  open: (incident) => incident.status !== "resolved",
  resolved: (incident) => incident.status === "resolved",
};

function IncidentList({ incidents, onSelect }) {
  const [filter, setFilter] = useState("all");
  const shown = incidents.filter(FILTERS[filter]);
  const count = (name) => incidents.filter(FILTERS[name]).length;
  return (
    <div className="stack">
      <PageHead
        title="Incidents"
        subtitle="Opened by the monitor when a pipeline run fails a quality check. One incident stays open per unresolved problem."
        actions={
          <Segmented
            label="Filter incidents"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: "All", count: count("all") },
              { value: "open", label: "Open", count: count("open") },
              { value: "resolved", label: "Resolved", count: count("resolved") },
            ]}
          />
        }
      />
      <Card flush>
        {shown.length === 0 ? (
          <div className="blank">
            <div className="blank-icon">
              <Icon name="check" size={20} />
            </div>
            <h2>{incidents.length === 0 ? "No incidents" : `No ${filter} incidents`}</h2>
            <p>
              {incidents.length === 0
                ? "Every quality check on the latest pipeline run passed. To see the workflow, inject a fault from the Scenario Lab."
                : "Nothing matches this filter."}
            </p>
            {incidents.length === 0 && (
              <a className="button" href="/lab/" target="_blank" rel="noreferrer">
                <Icon name="flask" size={14} /> Open the Scenario Lab
              </a>
            )}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="table table-cards clickable">
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Type</th>
                  <th>Summary</th>
                  <th>Detected</th>
                  <th className="right">Duration</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((incident) => (
                  <tr key={incident.incident_id} onClick={() => onSelect(incident.incident_id)}>
                    <td className="nowrap">
                      <button type="button" className="link id" onClick={() => onSelect(incident.incident_id)}>
                        {incident.incident_id}
                      </button>
                    </td>
                    <td>
                      <Badge value={incident.severity} />
                    </td>
                    <td>
                      <Badge value={incident.status} />
                    </td>
                    <td className="cell-meta nowrap">{titleCase(incident.incident_type)}</td>
                    <td className="cell-wide">
                      <span className="cell-clip">{incident.summary}</span>
                    </td>
                    <td className="cell-meta nowrap muted">{dateTime(incident.detected_at)}</td>
                    <td className="cell-meta right nowrap muted">
                      {incident.resolved_at
                        ? between(incident.detected_at, incident.resolved_at)
                        : `${since(incident.detected_at).replace(" ago", "")} open`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

export default function Incidents({ selectedId, onSelect, agent, onChanged }) {
  const { data: incidents } = usePoll("/incidents", 3000);
  if (selectedId) return <IncidentDetail key={selectedId} incidentId={selectedId} agent={agent} onChanged={onChanged} />;
  if (!incidents) return <Empty>Loading incidents…</Empty>;
  return <IncidentList incidents={incidents} onSelect={onSelect} />;
}
