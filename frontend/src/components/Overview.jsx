// Overview: is the data trustworthy right now, and what is the pipeline doing?
import { useState } from "react";
import { usePoll } from "../api.js";
import { ago, clock, money, number, signedMoney, titleCase } from "../format.js";
import { Badge, Card, Empty } from "./common.jsx";
import RevenueChart from "./RevenueChart.jsx";

function RevenueTile({ currency, sides }) {
  const matches = sides.within_tolerance;
  return (
    <div className="tile">
      <div className="tile-label">Net revenue · {currency}</div>
      <div className="tile-value">{money(sides.analytics.net, currency)}</div>
      <div className="tile-foot">
        <Badge tone={matches ? "good" : "critical"}>
          {matches ? "Matches source" : `${signedMoney(sides.net_difference, currency)} vs source`}
        </Badge>
        <span className="muted">Source-derived {money(sides.source.net, currency)}</span>
      </div>
      <dl className="tile-breakdown">
        <div>
          <dt>Gross paid</dt>
          <dd>{money(sides.analytics.gross, currency)}</dd>
          <dd className="muted">source {money(sides.source.gross, currency)}</dd>
        </div>
        <div>
          <dt>Refunds subtracted</dt>
          <dd>{money(sides.analytics.refunds, currency)}</dd>
          <dd className="muted">source {money(sides.source.refunds, currency)}</dd>
        </div>
      </dl>
    </div>
  );
}

function PipelineTile({ pipeline, orderCounts }) {
  const run = pipeline.last_run;
  const orders = Object.values(orderCounts).reduce(
    (sum, c) => ({ source: sum.source + c.source, analytics: sum.analytics + c.analytics }),
    { source: 0, analytics: 0 },
  );
  return (
    <div className="tile">
      <div className="tile-label">Pipeline · order_revenue</div>
      <div className="tile-value tile-value-small">
        {run ? <Badge value={run.status}>{`Run ${run.run_id} ${run.status}`}</Badge> : "No runs yet"}
      </div>
      <div className="tile-foot">
        <Badge tone={pipeline.fresh ? "good" : "warning"}>
          {pipeline.fresh ? "Fresh" : "Stale"} · refreshed {ago(pipeline.freshness_seconds)}
        </Badge>
      </div>
      {run?.error_summary && <p className="tile-error">{run.error_summary}</p>}
      <dl className="tile-breakdown">
        <div>
          <dt>Orders</dt>
          <dd>{number(orders.analytics)} in analytics</dd>
          <dd className="muted">{number(orders.source)} in source</dd>
        </div>
        <div>
          <dt>Transformation</dt>
          <dd>{pipeline.transform ? `Version ${pipeline.transform.version}` : "–"}</dd>
          <dd className="muted">{pipeline.transform?.author}</dd>
        </div>
      </dl>
    </div>
  );
}

function LiveFeed() {
  const { data } = usePoll("/transactions/live?limit=14", 2000);
  if (!data) return <Empty>Loading…</Empty>;
  return (
    <>
      <p className="muted feed-totals">
        {number(data.totals.orders)} orders · {number(data.totals.payments)} payments · {number(data.totals.refunds)} refunds in the source
      </p>
      <table className="table">
        <tbody>
          {data.events.map((event) => (
            <tr key={`${event.kind}-${event.id}`}>
              <td className="muted nowrap">{clock(event.at)}</td>
              <td>
                {titleCase(event.kind)} #{event.id}
                {event.kind !== "order" && <span className="muted"> · order #{event.order_id}</span>}
              </td>
              <td>
                <span className="status-text">{event.status}</span>
              </td>
              <td className="right nowrap">{money(event.amount, event.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function RunHistory() {
  const { data } = usePoll("/pipeline/runs?limit=8", 3000);
  if (!data) return <Empty>Loading…</Empty>;
  if (data.length === 0) return <Empty>No pipeline runs yet.</Empty>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Run</th>
          <th>Status</th>
          <th>Trigger</th>
          <th className="right">Loaded</th>
          <th>Quality checks</th>
        </tr>
      </thead>
      <tbody>
        {data.map((run) => {
          const failed = run.checks.filter((c) => !c.passed);
          return (
            <tr key={run.run_id}>
              <td className="nowrap">
                #{run.run_id} <span className="muted">{clock(run.started_at)}</span>
              </td>
              <td>
                <Badge value={run.status} />
              </td>
              <td className="muted">{titleCase(run.trigger)}</td>
              <td className="right">{number(run.loaded_count)}</td>
              <td>
                {failed.length === 0 ? (
                  <span className="muted">{run.checks.length} passed</span>
                ) : (
                  <span title={failed.map((c) => c.check_name).join("\n")}>
                    <Badge tone="critical">{failed.length} failed</Badge>{" "}
                    <span className="muted">{failed[0].check_name}{failed.length > 1 ? ` +${failed.length - 1}` : ""}</span>
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default function Overview({ metrics, onOpenIncident }) {
  const currencies = metrics ? Object.keys(metrics.reconciliation.by_currency) : [];
  const [currency, setCurrency] = useState("INR");
  if (!metrics) return <Empty>Loading the dashboard…</Empty>;
  const selected = currencies.includes(currency) ? currency : currencies[0];
  const incident = metrics.active_incident;

  return (
    <div className="stack">
      {incident && (
        <div className="banner" role="status">
          <div>
            <Badge value={incident.severity} /> <strong>{incident.incident_id}</strong>{" "}
            <Badge value={incident.status} />
            <p>{incident.summary}</p>
          </div>
          <button type="button" className="button button-primary" onClick={() => onOpenIncident(incident.incident_id)}>
            Open incident
          </button>
        </div>
      )}

      <div className="tiles">
        {currencies.map((code) => (
          <RevenueTile key={code} currency={code} sides={metrics.reconciliation.by_currency[code]} />
        ))}
        <PipelineTile pipeline={metrics.pipeline} orderCounts={metrics.order_counts} />
      </div>

      <Card
        title="Daily net revenue: source versus analytics"
        subtitle="Source-derived revenue is computed from the operational tables by the approved net_revenue contract."
        actions={
          currencies.length > 1 && (
            <label className="field-inline">
              Currency
              <select value={selected} onChange={(event) => setCurrency(event.target.value)}>
                {currencies.map((code) => (
                  <option key={code}>{code}</option>
                ))}
              </select>
            </label>
          )
        }
      >
        <RevenueChart daily={metrics.daily} currency={selected} tolerance={metrics.reconciliation.tolerance} />
      </Card>

      <div className="columns">
        <Card title="Live source transactions">
          <LiveFeed />
        </Card>
        <Card title="Recent pipeline runs">
          <RunHistory />
        </Card>
      </div>
    </div>
  );
}
