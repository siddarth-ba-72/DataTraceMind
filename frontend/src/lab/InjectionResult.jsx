// What the injection did: the pipeline runs it caused, which checks failed
// against which were expected to, and the revenue figures before and after.
import { dateTime, money, number, signedMoney, titleCase } from "../format.js";
import { Badge, Card } from "../components/common.jsx";
import { Icon } from "../components/icons.jsx";

function MetricsTable({ baseline, after }) {
  const currencies = Object.keys(baseline.by_currency);
  return (
    <div className="table-scroll">
      <table className="table table-inset numeric">
        <thead>
          <tr>
            <th>Currency</th>
            <th className="right">Source-derived</th>
            <th className="right">Analytics before</th>
            <th className="right">Analytics after</th>
            <th>After vs source</th>
          </tr>
        </thead>
        <tbody>
          {currencies.map((currency) => {
            const before = baseline.by_currency[currency];
            const now = after?.by_currency[currency];
            return (
              <tr key={currency}>
                <td>{currency}</td>
                <td className="right">{money((now || before).source.net, currency)}</td>
                <td className="right">{money(before.analytics.net, currency)}</td>
                <td className="right">{now ? money(now.analytics.net, currency) : "–"}</td>
                <td>
                  {!now ? (
                    "–"
                  ) : now.within_tolerance ? (
                    <Badge tone="good">Matches</Badge>
                  ) : (
                    <Badge tone="critical">{signedMoney(now.net_difference, currency)}</Badge>
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

function describeRun(run) {
  const parts = [run.full_refresh ? "full rebuild" : "incremental"];
  parts.push(`${number(run.loaded)} rows loaded`);
  if (run.rejected) parts.push(`${number(run.rejected)} rejected`);
  parts.push(`transformation v${run.transform_version}`);
  return parts.join(" · ");
}

export default function InjectionResult({ scenario, active, incident, incidentUrl, consoleTab }) {
  const report = active.injection_report;
  const runs = report?.runs || [];
  const failed = new Set(runs.at(-1)?.failed_checks || []);
  const expected = scenario.expected_failed_checks;
  const unexpected = [...failed].filter((name) => !expected.includes(name));
  const after = active.post_injection_metrics;

  return (
    <Card
      id="result"
      title={`Injection report · ${scenario.scenario_id}`}
      subtitle={`${scenario.name}. Injected ${dateTime(active.injected_at)}.`}
      actions={
        incident && (
          <a className="button" href={incidentUrl(incident.incident_id)} target={consoleTab}>
            {incident.incident_id} · {titleCase(incident.status)} <Icon name="external" size={13} />
          </a>
        )
      }
    >
      <div className="result-grid">
        <div>
          <h4>Pipeline runs caused by the injection</h4>
          <ol className="run-list">
            {runs.map((run) => (
              <li key={run.run_id}>
                <span className="id">#{run.run_id}</span>
                <Badge value={run.status} />
                <span>
                  {describeRun(run)}
                  <span className="muted">
                    {" "}
                    ·{" "}
                    {run.failed_checks.length === 0
                      ? "every check passed"
                      : `${run.failed_checks.length} checks failed`}
                  </span>
                  {run.error_summary && <span className="run-error">{run.error_summary}</span>}
                </span>
              </li>
            ))}
          </ol>
          {runs.length > 1 && (
            <p className="muted">
              This fault needs two runs: the first looks healthy, the second is where it bites.
            </p>
          )}

          <h4>Quality checks: expected against observed</h4>
          <ul className="check-list">
            {expected.map((name) => (
              <li key={name}>
                {failed.has(name) ? <Badge tone="good">Failed as expected</Badge> : <Badge tone="warning">Did not fail</Badge>}
                <code>{name}</code>
              </li>
            ))}
            {unexpected.map((name) => (
              <li key={name}>
                <Badge tone="neutral">Also failed</Badge>
                <code>{name}</code>
              </li>
            ))}
          </ul>
        </div>

        <div>
          <h4>Net revenue before and after</h4>
          {active.baseline_metrics && <MetricsTable baseline={active.baseline_metrics} after={after} />}
          {after && (
            <p className="muted result-note">
              {after.reconciled
                ? "Revenue still reconciles for every currency and day. This fault shows in row counts, not in revenue."
                : after.mismatched_day_count > 0 && Object.values(after.by_currency).every((c) => c.within_tolerance)
                  ? `Currency totals reconcile, but ${after.mismatched_day_count} days do not.`
                  : `${after.mismatched_day_count} days do not reconcile with the source.`}
            </p>
          )}
          <p className="note note-info">
            <span>
              {incident ? (
                <>
                  The monitor opened <strong>{incident.incident_id}</strong>. It carries the failed checks and nothing about
                  this scenario.
                </>
              ) : (
                "The monitor opened no incident for this injection."
              )}
            </span>
          </p>
        </div>
      </div>
    </Card>
  );
}
