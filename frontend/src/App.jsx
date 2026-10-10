// The incident console: is the data trustworthy, and if not, what is being done about it.
// Injecting faults and driving the demo environment live in a separate page, the Scenario Lab.
import { useEffect, useState } from "react";
import { getAccessCode, setAccessCode, usePoll } from "./api.js";
import { ago } from "./format.js";
import Audit from "./components/Audit.jsx";
import { AccessCode, Badge, ErrorNote, TRUST_TEXT } from "./components/common.jsx";
import { BrandMark, Icon } from "./components/icons.jsx";
import Incidents from "./components/Incident.jsx";
import Overview from "./components/Overview.jsx";

const LAB_URL = "/lab/";
const PAGES = [
  { key: "overview", label: "Overview", icon: "activity" },
  { key: "incidents", label: "Incidents", icon: "alert" },
  { key: "audit", label: "Audit log", icon: "list" },
];

// The view lives in the URL hash (#/incidents/INC-0001) so a reload or a
// shared link lands on the same page.
function readRoute() {
  const [page, id] = window.location.hash.replace(/^#\/?/, "").split("/");
  return { page: PAGES.some((p) => p.key === page) ? page : "overview", id: id || null };
}

function AgentCard({ agent }) {
  if (!agent) return null;
  const state = !agent.available ? "warning" : "good";
  return (
    <div className="agent-card">
      <div className="agent-card-title">
        <span className={`dot ${agent.investigation_running ? "dot-live" : `dot-${state}`}`} aria-hidden="true" />
        {agent.available ? (agent.investigation_running ? "Agent investigating" : "Agent available") : "Agent unavailable"}
      </div>
      {agent.available ? (
        <dl>
          <dt>Model</dt>
          <dd>{agent.model}</dd>
          <dt>Workflow</dt>
          <dd>{agent.workflow_version}</dd>
          <dt>Limit</dt>
          <dd>{agent.limits.per_hour} runs / hour</dd>
        </dl>
      ) : (
        <p>{agent.detail}</p>
      )}
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const [code, setCode] = useState(getAccessCode);
  const metrics = usePoll("/metrics/summary", 2500);
  const agent = usePoll("/agent/status", 5000);

  useEffect(() => {
    // The lab used to be a tab of this page; old links to it still work.
    if (/^#\/?lab\b/.test(window.location.hash)) window.location.replace(LAB_URL);
    const onHash = () => setRoute(readRoute());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = (page, id) => {
    window.location.hash = id ? `/${page}/${id}` : `/${page}`;
    window.scrollTo(0, 0);
  };
  const refreshAll = () => {
    metrics.refresh();
    agent.refresh();
  };

  const activeIncident = metrics.data?.active_incident;
  const trust = metrics.data?.trust;
  const pipeline = metrics.data?.pipeline;
  const current = PAGES.find((p) => p.key === route.page);

  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="#/overview">
          <BrandMark />
          <div>
            <div className="brand-name">DataTraceMind</div>
            <div className="brand-tag">Data reliability console</div>
          </div>
        </a>

        <div className="nav-label">Monitor</div>
        <nav aria-label="Sections">
          {PAGES.map((page) => (
            <button
              key={page.key}
              type="button"
              className={`nav-item ${route.page === page.key ? "active" : ""}`}
              aria-current={route.page === page.key ? "page" : undefined}
              onClick={() => go(page.key)}
            >
              <Icon name={page.icon} />
              {page.label}
              {page.key === "incidents" && activeIncident && (
                <span className="nav-count" title={`${activeIncident.incident_id} is open`}>
                  1
                </span>
              )}
            </button>
          ))}
        </nav>

        <div className="nav-label">Demo environment</div>
        <a className="nav-item" href={LAB_URL} target="_blank" rel="noreferrer">
          <Icon name="flask" />
          Scenario Lab
          <span className="nav-trailing">
            <Icon name="external" size={13} />
          </span>
        </a>
        <p className="nav-hint">Inject faults, run live traffic, and reset the environment. Opens in its own tab.</p>

        <div className="sidebar-foot">
          <AgentCard agent={agent.data} />
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <nav className="crumbs" aria-label="Breadcrumb">
            <span>order_revenue</span>
            <Icon name="chevronRight" size={13} />
            {route.id ? (
              <>
                <button type="button" onClick={() => go(route.page)}>
                  {current.label}
                </button>
                <Icon name="chevronRight" size={13} />
                <span className="crumb-current">{route.id}</span>
              </>
            ) : (
              <span className="crumb-current">{current.label}</span>
            )}
          </nav>
          <div className="topbar-right">
            {pipeline && (
              <span className="topbar-meta" title="Time since the analytics target was last rebuilt">
                Refreshed {ago(pipeline.freshness_seconds)}
              </span>
            )}
            {trust && <Badge value={trust}>{TRUST_TEXT[trust]}</Badge>}
            {agent.data?.access_code_required && (
              <AccessCode
                value={code}
                onChange={(value) => {
                  setCode(value);
                  setAccessCode(value);
                }}
              />
            )}
          </div>
        </header>

        <main className="content">
          {metrics.error && !metrics.data && (
            <ErrorNote message={`Cannot reach the DataTraceMind API: ${metrics.error.message}`} />
          )}
          {route.page === "overview" && <Overview metrics={metrics.data} onOpenIncident={(id) => go("incidents", id)} />}
          {route.page === "incidents" && (
            <Incidents selectedId={route.id} onSelect={(id) => go("incidents", id)} agent={agent.data} onChanged={refreshAll} />
          )}
          {route.page === "audit" && <Audit />}
        </main>
      </div>
    </div>
  );
}
