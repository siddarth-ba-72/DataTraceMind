// The Scenario Lab: break the demo pipeline on purpose, then watch DataTraceMind
// deal with it in the incident console. This page operates the demo
// environment (traffic, fault injection, reset); the console never does.
import { useState } from "react";
import { api, getAccessCode, setAccessCode, useAction, usePoll } from "../api.js";
import { ago, clock, money, number, since, titleCase } from "../format.js";
import { AccessCode, Badge, Button, Card, ErrorNote, Facts, PageHead, TRUST_TEXT } from "../components/common.jsx";
import { BrandMark, Icon } from "../components/icons.jsx";
import InjectionResult from "./InjectionResult.jsx";
import ScenarioCatalog from "./ScenarioCatalog.jsx";
import TrafficPanel from "./TrafficPanel.jsx";

const CONSOLE_URL = "/";
const incidentUrl = (id) => `${CONSOLE_URL}#/incidents/${id}`;
// One named tab, so every link from the lab lands in the same console window.
const CONSOLE_TAB = "dtm-console";

const STEPS = [
  ["baseline", "Start from the baseline", "Seeded data, the repository's transformation, no incidents."],
  ["traffic", "Run live traffic", "Optional. Keeps new orders, payments, and refunds arriving."],
  ["inject", "Inject one fault", "Pick a scenario. The lab breaks the pipeline in that one way."],
  ["resolve", "Resolve it in the console", "Investigate, review the repair, approve. Then reset."],
];

// Where the run stands, derived from what is injected and what became of its incident.
// A scenario that is meant to escalate has reached its end once it does.
function phaseOf(active, incident, scenario) {
  if (!active?.scenario_id) return "baseline";
  if (!incident) return "injected";
  if (incident.status === "resolved") return "resolved";
  if (incident.status === "escalated" && scenario?.resolution === "escalation") return "escalated";
  return "incident";
}

const FINISHED = new Set(["resolved", "escalated"]);

function Flow({ phase, trafficRunning }) {
  const current = phase === "baseline" ? "inject" : "resolve";
  const done = {
    baseline: true,
    traffic: trafficRunning,
    inject: phase !== "baseline",
    resolve: FINISHED.has(phase),
  };
  return (
    <ol className="flow" aria-label="How a lab run works">
      {STEPS.map(([key, title, caption], index) => {
        const state = done[key] ? "done" : key === current ? "current" : "todo";
        return (
          <li key={key} className={`flow-step flow-${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="flow-number" aria-hidden="true">
              {state === "done" ? <Icon name="check" size={13} /> : index + 1}
            </span>
            <div>
              <div className="flow-title">{title}</div>
              <div className="flow-caption">{caption}</div>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

// What to do next in the console, by where the incident stands.
const NEXT_STEP = {
  detected: "Open it in the incident console and start the investigation.",
  investigating: "The agent roles are gathering evidence. Follow them in the incident console.",
  diagnosed: "A root cause is confirmed and a repair is being prepared.",
  repair_proposed: "A repair is proposed and about to be validated in staging.",
  validating: "The proposed repair is being validated in staging.",
  awaiting_approval: "A validated repair is waiting for your decision in the incident console.",
  applying: "The approved repair is being applied.",
  verifying: "The pipeline is rerunning to verify the repair.",
  escalated: "The workflow handed it to a human. Read why in the incident console, or investigate again.",
  rejected: "The proposed repair was rejected. Investigate again from the incident console.",
  validation_failed: "The proposed repair failed staging validation. Investigate again from the incident console.",
  application_failed: "The repair failed verification and was rolled back. Investigate again from the incident console.",
};

function Headline({ phase, active, incident, strayIncident, onReset, confirming, resetting }) {
  // An incident left open from before, with nothing injected: the monitor
  // keeps one incident open at a time, so a new fault would open none.
  if (phase === "baseline" && strayIncident)
    return (
      <div className="headline headline-warning">
        <span className="dot dot-warning" aria-hidden="true" />
        <div>
          <strong>{strayIncident.incident_id} is still open</strong> although no scenario is injected. The monitor keeps
          one incident open at a time, so reset to the baseline before injecting a fault.
        </div>
        <a className="button" href={incidentUrl(strayIncident.incident_id)} target={CONSOLE_TAB}>
          Open {strayIncident.incident_id} <Icon name="external" size={13} />
        </a>
        <Button icon="reset" onClick={onReset} busy={resetting}>
          {confirming ? "Click again to reset" : "Reset to baseline"}
        </Button>
      </div>
    );
  if (phase === "baseline")
    return (
      <div className="headline">
        <span className="dot dot-good" aria-hidden="true" />
        <div>
          <strong>The environment is at the clean baseline.</strong> Choose a scenario below, read what it does, and
          inject it. Only one fault can be active at a time.
        </div>
      </div>
    );
  const scenario = active.scenario_id;
  if (phase === "injected")
    return (
      <div className="headline headline-warning">
        <span className="dot dot-warning" aria-hidden="true" />
        <div>
          <strong>{scenario} is injected</strong> and the monitor has not opened an incident for it. Check the
          injection report below.
        </div>
      </div>
    );
  if (FINISHED.has(phase))
    return (
      <div className="headline">
        <span className="dot dot-good" aria-hidden="true" />
        <div>
          {phase === "resolved" ? (
            <>
              <strong>{incident.incident_id} is resolved.</strong> The repair for {scenario} was approved, applied, and
              verified.
            </>
          ) : (
            <>
              <strong>{incident.incident_id} was escalated to a human</strong>, which is how {scenario} is meant to end:
              no transformation change is the right fix.
            </>
          )}{" "}
          Reset to the baseline to run another scenario.
        </div>
        <a className="button" href={incidentUrl(incident.incident_id)} target={CONSOLE_TAB}>
          Open {incident.incident_id} <Icon name="external" size={13} />
        </a>
        <Button icon="reset" onClick={onReset} busy={resetting}>
          Reset to baseline
        </Button>
      </div>
    );
  return (
    <div className="headline headline-critical">
      <span className="dot dot-critical" aria-hidden="true" />
      <div>
        <strong>{scenario} is injected.</strong> The monitor opened {incident.incident_id}, now{" "}
        <Badge value={incident.status} />. {NEXT_STEP[incident.status]}
      </div>
      <a className="button button-primary" href={incidentUrl(incident.incident_id)} target={CONSOLE_TAB}>
        Open {incident.incident_id} <Icon name="arrowRight" size={14} />
      </a>
    </div>
  );
}

function Environment({ lab, metrics, live, phase, openIncident, onReset, confirming, resetting }) {
  const pipeline = metrics?.pipeline;
  const run = pipeline?.last_run;
  const env = lab.environment;
  return (
    <Card
      title="Environment"
      subtitle="The demo stack this lab operates: a source database, the order_revenue pipeline, and its analytics target."
      actions={
        <Button
          kind="danger"
          icon="reset"
          onClick={onReset}
          busy={resetting}
          title="Restores the seeded data and the baseline transformation, clears every incident, and stops traffic"
        >
          {confirming ? "Click again to reset" : "Reset to baseline"}
        </Button>
      }
    >
      <Facts
        rows={[
          [
            "State",
            phase === "baseline" ? (
              openIncident ? (
                <Badge key="s" tone="warning">
                  No scenario injected, incident open
                </Badge>
              ) : (
                <Badge key="b" tone="good">
                  Clean baseline
                </Badge>
              )
            ) : (
              <span key="f">
                {phase === "resolved" ? (
                  <Badge tone="good">{lab.active.scenario_id} repaired</Badge>
                ) : phase === "escalated" ? (
                  <Badge tone="warning">{lab.active.scenario_id} escalated</Badge>
                ) : (
                  <Badge tone="serious">{lab.active.scenario_id} injected</Badge>
                )}{" "}
                <span className="muted">injected {since(lab.active.injected_at)}</span>
              </span>
            ),
          ],
          ["Data trust", metrics ? <Badge key="t" value={metrics.trust}>{TRUST_TEXT[metrics.trust]}</Badge> : null],
          [
            "Open incident",
            openIncident ? (
              <a key="i" href={incidentUrl(openIncident.incident_id)} target={CONSOLE_TAB}>
                {openIncident.incident_id} · {titleCase(openIncident.status)}
              </a>
            ) : (
              "None"
            ),
          ],
          [
            "Last pipeline run",
            run ? `#${run.run_id} ${run.status} · refreshed ${ago(pipeline.freshness_seconds)}` : null,
          ],
          ["Pipeline schedule", `Every ${number(env.etl_interval_seconds)} seconds`],
          [
            "Transformation",
            pipeline?.transform ? `Version ${pipeline.transform.version} · ${pipeline.transform.author}` : null,
          ],
          [
            "Source rows",
            live
              ? `${number(live.totals.orders)} orders · ${number(live.totals.payments)} payments · ${number(live.totals.refunds)} refunds`
              : null,
          ],
          ["Baseline seed", `${env.seed} · ${number(env.seed_orders)} orders over ${env.seed_days} days`],
        ]}
      />
      {live && (
        <div className="table-scroll env-feed">
          <table className="table table-inset numeric">
            <thead>
              <tr>
                <th>Latest in the source</th>
                <th>Status</th>
                <th className="right">Amount</th>
                <th className="right">Time</th>
              </tr>
            </thead>
            <tbody>
              {live.events.slice(0, 4).map((event) => (
                <tr key={`${event.kind}-${event.id}`}>
                  <td>
                    {titleCase(event.kind)} #{event.id}
                  </td>
                  <td>
                    <span className="status-text">{event.status}</span>
                  </td>
                  <td className="right nowrap">{money(event.amount, event.currency)}</td>
                  <td className="right nowrap muted">{clock(event.at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export default function LabApp() {
  const [code, setCode] = useState(getAccessCode);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const lab = usePoll("/scenarios", 3000);
  const metrics = usePoll("/metrics/summary", 2500);
  const traffic = usePoll("/demo/traffic", 2000);
  const live = usePoll("/transactions/live?limit=6", 2000);
  const incidents = usePoll("/incidents", 3000);
  const agent = usePoll("/agent/status", 10000);
  const resetAction = useAction();
  const injectAction = useAction();

  // A server started before this page was built answers without the fields
  // the lab needs; say so instead of failing on the first missing one.
  const outdatedApi = Boolean(lab.data) && !lab.data.environment;
  const active = lab.data?.active;
  // Whatever is unresolved right now, and the incident this scenario caused.
  // An injection made before reports were kept has no report; the open
  // incident is then the one it caused.
  const openIncident = metrics.data?.active_incident;
  const incidentId = active?.scenario_id ? (active.injection_report?.incident_id ?? openIncident?.incident_id) : null;
  const incident = incidentId ? incidents.data?.find((row) => row.incident_id === incidentId) : null;
  const activeScenario = lab.data?.scenarios.find((s) => s.scenario_id === active?.scenario_id);
  const phase = phaseOf(active, incident, activeScenario);
  const strayIncident = phase === "baseline" ? openIncident : null;

  const refreshAll = () => Promise.all([lab, metrics, traffic, live, incidents].map((poll) => poll.refresh()));

  // Reset wipes every incident, so it takes two clicks; the second must come
  // within a few seconds or the button returns to its first state.
  const reset = async () => {
    // Once a run has reached its end there is nothing in progress to lose.
    if (!confirmingReset && !FINISHED.has(phase)) {
      setConfirmingReset(true);
      window.setTimeout(() => setConfirmingReset(false), 4000);
      return;
    }
    setConfirmingReset(false);
    await resetAction.run(() => api("/demo/reset", { method: "POST" }));
    await refreshAll();
  };
  const inject = async (scenarioId) => {
    await injectAction.run(() => api(`/scenarios/${scenarioId}/inject`, { method: "POST" }));
    await refreshAll();
    document.getElementById("result")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="lab">
      <header className="lab-bar">
        <a className="brand" href="/lab/">
          <BrandMark />
          <div>
            <div className="brand-name">Scenario Lab</div>
            <div className="brand-tag">DataTraceMind demo environment</div>
          </div>
        </a>
        <span className="lab-env" title="Everything on this page acts on the disposable demo environment">
          <Icon name="flask" size={13} /> Simulation
        </span>
        <span className="spacer" />
        {agent.data?.access_code_required && (
          <AccessCode
            value={code}
            onChange={(value) => {
              setCode(value);
              setAccessCode(value);
            }}
          />
        )}
        <a className="lab-link" href={CONSOLE_URL} target={CONSOLE_TAB}>
          Incident console <Icon name="external" size={13} />
        </a>
      </header>

      <main className="content">
        <PageHead
          title="Scenario Lab"
          subtitle="Break the demo pipeline in one known way, then resolve it in the incident console. DataTraceMind is never told which fault is active: it has to find the cause from the same evidence an engineer would have."
        />

        {lab.error && !lab.data && <ErrorNote message={`Cannot reach the DataTraceMind API: ${lab.error.message}`} />}
        <ErrorNote message={resetAction.error} onDismiss={resetAction.clearError} />
        {outdatedApi && (
          <ErrorNote message="The DataTraceMind server is running an older version than this page, so the lab cannot load. Restart the server to pick up the current backend." />
        )}

        {lab.data && !outdatedApi && (
          <div className="stack">
            <Flow phase={phase} trafficRunning={Boolean(traffic.data?.running)} />
            <Headline
              phase={phase}
              active={active}
              incident={incident}
              strayIncident={strayIncident}
              onReset={reset}
              confirming={confirmingReset}
              resetting={resetAction.busy}
            />

            <div className="lab-row">
              <Environment
                lab={lab.data}
                metrics={metrics.data}
                live={live.data}
                phase={phase}
                openIncident={openIncident}
                onReset={reset}
                confirming={confirmingReset}
                resetting={resetAction.busy}
              />
              <TrafficPanel
                traffic={traffic.data}
                etlInterval={lab.data.environment.etl_interval_seconds}
                onChanged={refreshAll}
              />
            </div>

            {activeScenario && (
              <InjectionResult
                scenario={activeScenario}
                active={active}
                incident={incident}
                incidentUrl={incidentUrl}
                consoleTab={CONSOLE_TAB}
              />
            )}

            <ScenarioCatalog
              scenarios={lab.data.scenarios}
              active={active}
              incident={incident}
              blockedBy={strayIncident?.incident_id}
              incidentUrl={incidentUrl}
              consoleTab={CONSOLE_TAB}
              onInject={inject}
              injecting={injectAction.busy}
              error={injectAction.error}
              onDismissError={injectAction.clearError}
            />
          </div>
        )}
      </main>
    </div>
  );
}
