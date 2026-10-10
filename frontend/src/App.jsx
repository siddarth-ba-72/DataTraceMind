import { useEffect, useState } from "react";
import { api, getAccessCode, setAccessCode, useAction, usePoll } from "./api.js";
import { titleCase } from "./format.js";
import Audit from "./components/Audit.jsx";
import { Badge, Button, ErrorNote } from "./components/common.jsx";
import Incidents from "./components/Incident.jsx";
import Overview from "./components/Overview.jsx";
import ScenarioLab from "./components/ScenarioLab.jsx";

const TABS = [
  ["overview", "Overview"],
  ["lab", "Scenario lab"],
  ["incidents", "Incidents"],
  ["audit", "Audit log"],
];
const TRUST_TEXT = {
  trustworthy: "Data is trustworthy",
  under_investigation: "Under investigation",
  stale: "Data is stale",
  untrusted: "Data does not reconcile",
};

// The view lives in the URL hash (#/incidents/INC-0001) so a reload or a
// shared link lands on the same page.
function readRoute() {
  const [tab, id] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { tab: TABS.some(([key]) => key === tab) ? tab : "overview", id: id || null };
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [code, setCode] = useState(getAccessCode);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const metrics = usePoll("/metrics/summary", 2500);
  const agent = usePoll("/agent/status", 5000);
  const health = usePoll("/health", 4000);
  const action = useAction();

  useEffect(() => {
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (tab, id) => {
    window.location.hash = id ? `/${tab}/${id}` : `/${tab}`;
  };
  const refreshAll = () => {
    metrics.refresh();
    agent.refresh();
    health.refresh();
  };
  const post = (path) =>
    action.run(async () => {
      await api(path, { method: "POST" });
      refreshAll();
    });
  // Reset wipes every incident, so it takes two clicks; the second must come
  // within a few seconds or the button returns to its first state.
  const reset = async () => {
    if (!confirmingReset) {
      setConfirmingReset(true);
      window.setTimeout(() => setConfirmingReset(false), 4000);
      return;
    }
    setConfirmingReset(false);
    await post("/demo/reset");
    go("overview");
  };

  const trafficRunning = health.data?.traffic_running;
  const activeIncident = metrics.data?.active_incident;
  const trust = metrics.data?.trust;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          <div>
            <div className="brand-name">DataTraceMind</div>
            <div className="brand-tag">Agentic data reliability engineer</div>
          </div>
        </div>
        <div className="topbar-status">
          {trust && <Badge value={trust}>{TRUST_TEXT[trust]}</Badge>}
          {agent.data && (
            <Badge tone={agent.data.available ? (agent.data.investigation_running ? "active" : "good") : "warning"} title={agent.data.detail}>
              {agent.data.available
                ? agent.data.investigation_running
                  ? "Agent investigating"
                  : `Agent available · ${agent.data.model}`
                : "Agent unavailable"}
            </Badge>
          )}
        </div>
        <div className="topbar-actions">
          {agent.data?.access_code_required && (
            <input
              className="access-code"
              type="password"
              placeholder="Demo access code"
              aria-label="Demo access code"
              value={code}
              onChange={(event) => {
                setCode(event.target.value);
                setAccessCode(event.target.value);
              }}
            />
          )}
          <Button onClick={() => post(trafficRunning ? "/demo/traffic/stop" : "/demo/traffic/start")} busy={action.busy}>
            {trafficRunning ? "Stop traffic" : "Start traffic"}
          </Button>
          <Button
            onClick={reset}
            busy={action.busy}
            kind="danger"
            title="Restores the clean baseline and clears all incidents"
          >
            {confirmingReset ? "Click again to reset" : "Reset demo"}
          </Button>
        </div>
      </header>

      <nav className="tabs" aria-label="Sections">
        {TABS.map(([key, label]) => (
          <button key={key} type="button" className={route.tab === key ? "active" : ""} onClick={() => go(key)}>
            {label}
            {key === "incidents" && activeIncident && <span className="tab-count">{titleCase(activeIncident.status)}</span>}
          </button>
        ))}
      </nav>

      <main>
        <ErrorNote message={action.error} onDismiss={action.clearError} />
        {metrics.error && !metrics.data && <ErrorNote message={`Cannot reach the DataTraceMind API: ${metrics.error.message}`} />}
        {route.tab === "overview" && <Overview metrics={metrics.data} onOpenIncident={(id) => go("incidents", id)} />}
        {route.tab === "lab" && (
          <ScenarioLab onChanged={refreshAll} onOpenIncident={(id) => go("incidents", id)} activeIncident={activeIncident} />
        )}
        {route.tab === "incidents" && (
          <Incidents selectedId={route.id} onSelect={(id) => go("incidents", id)} agent={agent.data} onChanged={refreshAll} />
        )}
        {route.tab === "audit" && <Audit />}
      </main>
    </div>
  );
}
