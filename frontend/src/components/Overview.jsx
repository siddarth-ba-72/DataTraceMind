// Overview: is the data trustworthy right now, and what is the pipeline doing?
import { useState } from "react";
import { usePoll } from "../api.js";
import { ago, clock, money, number, signedMoney, titleCase } from "../format.js";
import { Badge, Card, Empty, PageHead, TRUST_TEXT } from "./common.jsx";
import { Icon } from "./icons.jsx";
import RevenueChart from "./RevenueChart.jsx";

const TRUST_DOT = { trustworthy: "good", under_investigation: "critical", stale: "warning", untrusted: "critical" };
const TRUST_NOTE = {
  trustworthy: "Analytics agrees with the source and is fresh.",
  under_investigation: "An incident is open. Treat the numbers below as suspect until it is resolved.",
  stale: "The analytics target has not refreshed within its objective.",
  untrusted: "Analytics disagrees with the source and no incident covers it yet.",
};

function TrustTile({ metrics, lastRun }) {
  const passed = lastRun ? lastRun.checks.filter((c) => c.passed).length : null;
  return (
    <div className="kpi">
      <div className="kpi-label">
        <Icon name="shield" size={14} /> Data trust
      </div>
      <div className="kpi-value kpi-value-text">
        <span className={`dot dot-${TRUST_DOT[metrics.trust]}`} aria-hidden="true" />
        {TRUST_TEXT[metrics.trust]}
      </div>
      <div className="kpi-foot muted">{TRUST_NOTE[metrics.trust]}</div>
      <dl className="kpi-breakdown">
        <div>
          <dt>Quality checks</dt>
          <dd>{lastRun ? `${passed} of ${lastRun.checks.length} passed` : "–"}</dd>
          <dd className="muted">{lastRun ? `on run #${lastRun.run_id}` : ""}</dd>
        </div>
        <div>
          <dt>Compared as of</dt>
          <dd>{clock(metrics.as_of)}</dd>
          <dd className="muted">last successful load</dd>
        </div>
      </dl>
    </div>
  );
}

// The source figure is spelled out only where analytics departs from it.
function SourceLine({ analytics, source, currency }) {
  return analytics === source ? (
    <dd className="muted">matches source</dd>
  ) : (
    <dd className="kpi-differs">source {money(source, currency)}</dd>
  );
}

function RevenueTile({ currency, sides }) {
  const matches = sides.within_tolerance;
  return (
    <div className="kpi">
      <div className="kpi-label">
        <Icon name="coins" size={14} /> Net revenue · {currency}
      </div>
      <div className="kpi-value">{money(sides.analytics.net, currency)}</div>
      <div className="kpi-foot">
        <Badge tone={matches ? "good" : "critical"}>
          {matches ? "Matches source" : `${signedMoney(sides.net_difference, currency)} vs source`}
        </Badge>
        <span className="muted">Source-derived {money(sides.source.net, currency)}</span>
      </div>
      <dl className="kpi-breakdown">
        <div>
          <dt>Gross paid</dt>
          <dd>{money(sides.analytics.gross, currency)}</dd>
          <SourceLine analytics={sides.analytics.gross} source={sides.source.gross} currency={currency} />
        </div>
        <div>
          <dt>Refunds subtracted</dt>
          <dd>{money(sides.analytics.refunds, currency)}</dd>
          <SourceLine analytics={sides.analytics.refunds} source={sides.source.refunds} currency={currency} />
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
    <div className="kpi">
      <div className="kpi-label">
        <Icon name="layers" size={14} /> Pipeline · order_revenue
      </div>
      <div className="kpi-value kpi-value-text">
        {run ? (
          <>
            <span className={`dot dot-${run.status === "SUCCESS" ? "good" : "critical"}`} aria-hidden="true" />
            Run #{run.run_id} {run.status === "SUCCESS" ? "succeeded" : run.status === "FAILED" ? "failed" : "running"}
          </>
        ) : (
          "No runs yet"
        )}
      </div>
      <div className="kpi-foot">
        <Badge tone={pipeline.fresh ? "good" : "warning"}>
          {pipeline.fresh ? "Fresh" : "Stale"} · refreshed {ago(pipeline.freshness_seconds)}
        </Badge>
      </div>
      {run?.error_summary && <p className="kpi-error">{run.error_summary}</p>}
      <dl className="kpi-breakdown">
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

function TrafficState({ traffic }) {
  if (!traffic) return null;
  return traffic.running ? (
    <span className="field-inline">
      <span className="dot dot-live" aria-hidden="true" /> Live · about {traffic.per_minute} per minute
    </span>
  ) : (
    <span className="field-inline muted" title="Live traffic is started from the Scenario Lab">
      <span className="dot" aria-hidden="true" /> No live traffic
    </span>
  );
}

function LiveFeed() {
  const { data } = usePoll("/transactions/live?limit=12", 5000);
  if (!data) return <Empty>Loading…</Empty>;
  return (
    <>
      <p className="muted feed-totals">
        {number(data.totals.orders)} orders · {number(data.totals.payments)} payments · {number(data.totals.refunds)} refunds in the source
      </p>
      <div className="table-scroll">
        <table className="table numeric">
          <thead>
            <tr>
              <th>Time</th>
              <th>Event</th>
              <th>Status</th>
              <th className="right">Amount</th>
            </tr>
          </thead>
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
      </div>
    </>
  );
}

function RunHistory({ runs }) {
  if (!runs) return <Empty>Loading…</Empty>;
  if (runs.length === 0) return <Empty>No pipeline runs yet.</Empty>;
  return (
    <div className="table-scroll">
      <table className="table numeric">
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
          {runs.map((run) => {
            const failed = run.checks.filter((c) => !c.passed);
            return (
              <tr key={run.run_id}>
                <td className="nowrap">
                  <span className="id">#{run.run_id}</span> <span className="muted">{clock(run.started_at)}</span>
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
                      <span className="muted">
                        {failed[0].check_name}
                        {failed.length > 1 ? ` +${failed.length - 1}` : ""}
                      </span>
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function Overview({ metrics, onOpenIncident }) {
  const currencies = metrics ? Object.keys(metrics.reconciliation.by_currency) : [];
  const [currency, setCurrency] = useState("INR");
  const runs = usePoll("/pipeline/runs?limit=8", 5000);
  const traffic = usePoll("/demo/traffic", 5000);
  if (!metrics) return <Empty>Loading the dashboard…</Empty>;
  const selected = currencies.includes(currency) ? currency : currencies[0];
  const incident = metrics.active_incident;
  const lastRun = runs.data?.find((run) => run.status !== "RUNNING");

  return (
    <div className="stack">
      <PageHead
        title="Overview"
        subtitle="Whether the analytics target agrees with the source of truth, checked after every pipeline run."
      />

      {incident && (
        <div className={`banner ${incident.severity === "critical" ? "banner-critical" : ""}`} role="status">
          <div>
            <div className="banner-title">
              <span className="id">{incident.incident_id}</span>
              <Badge value={incident.severity} />
              <Badge value={incident.status} />
            </div>
            <p>{incident.summary}</p>
          </div>
          <button type="button" className="button button-primary" onClick={() => onOpenIncident(incident.incident_id)}>
            Open incident <Icon name="arrowRight" size={14} />
          </button>
        </div>
      )}

      <div className="kpis">
        <TrustTile metrics={metrics} lastRun={lastRun} />
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
        <Card title="Source transactions" actions={<TrafficState traffic={traffic.data} />} flush>
          <LiveFeed />
        </Card>
        <Card title="Recent pipeline runs" flush>
          <RunHistory runs={runs.data} />
        </Card>
      </div>
    </div>
  );
}
