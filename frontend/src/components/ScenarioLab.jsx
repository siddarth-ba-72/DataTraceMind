// Scenario Lab: inject one known fault, or reset to the clean baseline.
import { api, useAction, usePoll } from "../api.js";
import { dateTime, money, signedMoney } from "../format.js";
import { Badge, Button, Card, Empty, ErrorNote } from "./common.jsx";

function MetricsTable({ baseline, after }) {
  const currencies = Object.keys(baseline.by_currency);
  return (
    <table className="table numeric">
      <thead>
        <tr>
          <th>Currency</th>
          <th>Source-derived net</th>
          <th>Analytics net before</th>
          <th>Analytics net after</th>
          <th>Analytics vs source</th>
        </tr>
      </thead>
      <tbody>
        {currencies.map((currency) => {
          const before = baseline.by_currency[currency];
          const now = after?.by_currency[currency];
          return (
            <tr key={currency}>
              <td>{currency}</td>
              <td>{money((now || before).source.net, currency)}</td>
              <td>{money(before.analytics.net, currency)}</td>
              <td>{now ? money(now.analytics.net, currency) : "–"}</td>
              <td>
                {!now ? "–" : now.within_tolerance ? "matches" : signedMoney(now.net_difference, currency)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

export default function ScenarioLab({ onChanged, onOpenIncident, activeIncident }) {
  const { data, refresh } = usePoll("/scenarios", 4000);
  const action = useAction();
  if (!data) return <Empty>Loading scenarios…</Empty>;
  const active = data.active?.scenario_id;

  async function inject(scenarioId) {
    const report = await action.run(() => api(`/scenarios/${scenarioId}/inject`, { method: "POST" }));
    await refresh();
    onChanged();
    if (report?.incident_id) onOpenIncident(report.incident_id);
  }

  return (
    <div className="stack">
      <Card
        title="Scenario lab"
        subtitle="Each scenario introduces one deterministic fault into the demo environment. DataTraceMind is not told which one: it has to find the cause from the same evidence an engineer would have."
      >
        <ErrorNote message={action.error} onDismiss={action.clearError} />
        <div className="scenarios">
          {data.scenarios.map((scenario) => (
            <article key={scenario.scenario_id} className={`scenario ${active === scenario.scenario_id ? "scenario-active" : ""}`}>
              <header>
                <span className="scenario-id">{scenario.scenario_id}</span>
                <span className="muted">{scenario.failure_type}</span>
              </header>
              <h3>{scenario.name}</h3>
              <p>{scenario.description}</p>
              {active === scenario.scenario_id ? (
                <Badge tone="serious">Injected {dateTime(data.active.injected_at)}</Badge>
              ) : (
                <Button
                  onClick={() => inject(scenario.scenario_id)}
                  busy={action.busy}
                  disabled={Boolean(active)}
                  title={active ? "Reset the demo before injecting another scenario" : undefined}
                >
                  Inject fault
                </Button>
              )}
            </article>
          ))}
        </div>
        {active && (
          <p className="muted">
            {active} is injected. Reset the demo (top right) to return to the clean baseline before injecting another scenario.
            {activeIncident && (
              <>
                {" "}
                <button type="button" className="link" onClick={() => onOpenIncident(activeIncident.incident_id)}>
                  Open {activeIncident.incident_id}
                </button>
              </>
            )}
          </p>
        )}
      </Card>

      {data.active?.baseline_metrics && (
        <Card title="Baseline versus post-injection" subtitle="Net revenue per currency, before the fault and after the first pipeline run with it.">
          <MetricsTable baseline={data.active.baseline_metrics} after={data.active.post_injection_metrics} />
        </Card>
      )}
    </div>
  );
}
