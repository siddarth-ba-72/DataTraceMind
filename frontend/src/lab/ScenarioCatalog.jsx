// The scenario catalogue: every fault the lab can inject, explained before you inject it.
import { useState } from "react";
import { dateTime } from "../format.js";
import { Badge, Button, Card, ErrorNote, Segmented } from "../components/common.jsx";
import { Icon } from "../components/icons.jsx";

const FILTERS = {
  all: { label: "All", test: () => true },
  silent: { label: "Job stays green", test: (s) => s.etl_reports === "SUCCESS" },
  failing: { label: "Job fails", test: (s) => s.etl_reports !== "SUCCESS" },
  repair: { label: "Ends in a repair", test: (s) => s.resolution === "repair" },
  escalation: { label: "Ends in an escalation", test: (s) => s.resolution === "escalation" },
};

function JobBadge({ scenario }) {
  return scenario.etl_reports === "SUCCESS" ? (
    <Badge tone="good" title="The ETL job keeps reporting SUCCESS, so nothing looks wrong from the outside">
      Job reports SUCCESS
    </Badge>
  ) : (
    <Badge tone="critical" title="The ETL job fails outright">
      Job reports FAILED
    </Badge>
  );
}

function OutcomeBadge({ scenario }) {
  return scenario.resolution === "repair" ? (
    <Badge tone="neutral" title="A repair is proposed, proven in staging, and applied once you approve it">
      Repair for approval
    </Badge>
  ) : (
    <Badge tone="warning" title="No automatic fix is right; the workflow hands the incident to a person">
      Escalates to a human
    </Badge>
  );
}

function ScenarioRow({ scenario, selected, injected, onSelect }) {
  return (
    <li>
      <button
        type="button"
        className={`scenario-row ${selected ? "selected" : ""}`}
        aria-pressed={selected}
        onClick={() => onSelect(scenario.scenario_id)}
      >
        <span className="scenario-row-id id">{scenario.scenario_id}</span>
        <span className="scenario-row-text">
          <span className="scenario-row-name">{scenario.name}</span>
          <span className="scenario-row-meta">
            {scenario.layer} · {scenario.failure_type}
          </span>
        </span>
        {injected ? <Badge tone="serious">Injected</Badge> : <Icon name="chevronRight" size={14} />}
      </button>
    </li>
  );
}

function Brief({ title, children }) {
  return (
    <div className="brief">
      <dt>{title}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function ScenarioDetail({ scenario, active, incident, blockedBy, incidentUrl, consoleTab, onInject, injecting }) {
  const activeId = active?.scenario_id;
  const isActive = activeId === scenario.scenario_id;
  return (
    <article id="scenario-detail" className="scenario-detail" aria-live="polite">
      <header className="scenario-detail-head">
        <div className="row">
          <span className="id">{scenario.scenario_id}</span>
          <span className="tag">{scenario.priority}</span>
          <span className="tag">{scenario.failure_type}</span>
        </div>
        <h2>{scenario.name}</h2>
        <p>{scenario.description}</p>
      </header>

      <div className="scenario-facts">
        <div>
          <div className="eyebrow">Where the fault lives</div>
          <div className="scenario-fact">{scenario.layer}</div>
        </div>
        <div>
          <div className="eyebrow">What the job says</div>
          <JobBadge scenario={scenario} />
        </div>
        <div>
          <div className="eyebrow">How it should end</div>
          <OutcomeBadge scenario={scenario} />
        </div>
      </div>

      <dl className="briefs">
        <Brief title="What the lab changes">{scenario.injection}</Brief>
        <Brief title="What you will see">
          <ul className="bullets">
            {scenario.symptoms.map((symptom) => (
              <li key={symptom}>{symptom}</li>
            ))}
          </ul>
        </Brief>
        <Brief title="Checks that should fail">
          <span className="chips">
            {scenario.expected_failed_checks.map((check) => (
              <code key={check}>{check}</code>
            ))}
          </span>
        </Brief>
        <Brief title="Why it matters">{scenario.impact}</Brief>
        <Brief title="Expected resolution">{scenario.resolution_note}</Brief>
      </dl>

      <footer className="scenario-action">
        {isActive ? (
          <>
            <Badge tone="serious">Injected {dateTime(active.injected_at)}</Badge>
            {incident && (
              <a className="button button-primary" href={incidentUrl(incident.incident_id)} target={consoleTab}>
                Open {incident.incident_id} in the console <Icon name="arrowRight" size={14} />
              </a>
            )}
          </>
        ) : (
          <>
            <Button
              kind="primary"
              size="large"
              icon="zap"
              onClick={() => onInject(scenario.scenario_id)}
              busy={injecting}
              disabled={Boolean(activeId || blockedBy)}
            >
              Inject fault
            </Button>
            <span className="muted">
              {activeId
                ? `${activeId} is injected. Reset to the baseline before injecting another scenario.`
                : blockedBy
                  ? `${blockedBy} is still open. Reset to the baseline first: the monitor keeps one incident open at a time.`
                  : "Applies the fault and runs the pipeline so the monitor can react. Takes a few seconds."}
            </span>
          </>
        )}
      </footer>
    </article>
  );
}

export default function ScenarioCatalog({
  scenarios, active, incident, blockedBy, incidentUrl, consoleTab, onInject, injecting, error, onDismissError,
}) {
  const [filter, setFilter] = useState("all");
  const [chosen, setChosen] = useState(null);
  const shown = scenarios.filter(FILTERS[filter].test);
  // Follow the injected scenario until the operator picks another one to read.
  const selectedId = chosen ?? active?.scenario_id ?? scenarios[0].scenario_id;
  const selected = shown.find((s) => s.scenario_id === selectedId) ?? shown[0];
  const choose = (scenarioId) => {
    setChosen(scenarioId);
    // On a narrow screen the detail sits under the list, out of view.
    if (window.matchMedia("(max-width: 960px)").matches)
      document.getElementById("scenario-detail")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <Card
      id="scenarios"
      title={`Fault scenarios · ${scenarios.length}`}
      subtitle="Each one introduces a single deterministic fault. Select a scenario to read what it changes, what you should see, and how the incident is expected to end."
      actions={
        <Segmented
          label="Filter scenarios"
          value={filter}
          onChange={setFilter}
          options={Object.entries(FILTERS).map(([value, { label, test }]) => ({
            value,
            label,
            count: scenarios.filter(test).length,
          }))}
        />
      }
    >
      <ErrorNote message={error} onDismiss={onDismissError} />
      <div className="catalog">
        <ul className="scenario-list" aria-label="Scenarios">
          {shown.map((scenario) => (
            <ScenarioRow
              key={scenario.scenario_id}
              scenario={scenario}
              selected={scenario.scenario_id === selected?.scenario_id}
              injected={scenario.scenario_id === active?.scenario_id}
              onSelect={choose}
            />
          ))}
        </ul>
        {selected && (
          <ScenarioDetail
            scenario={selected}
            active={active}
            incident={incident}
            blockedBy={blockedBy}
            incidentUrl={incidentUrl}
            consoleTab={consoleTab}
            onInject={onInject}
            injecting={injecting}
          />
        )}
      </div>
    </Card>
  );
}
