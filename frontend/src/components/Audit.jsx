// Audit timeline: who did what, to what, and with what result.
import { useState } from "react";
import { usePoll } from "../api.js";
import { dateTime, titleCase } from "../format.js";
import { Badge, Card, Empty, PageHead } from "./common.jsx";
import { Icon } from "./icons.jsx";

function matches(entry, query) {
  if (!query) return true;
  const haystack = `${entry.actor} ${entry.action} ${entry.target} ${entry.result} ${JSON.stringify(entry.details)}`;
  return haystack.toLowerCase().includes(query.toLowerCase());
}

export default function Audit() {
  const { data } = usePoll("/audit?limit=300", 5000);
  const [query, setQuery] = useState("");
  if (!data) return <Empty>Loading the audit log…</Empty>;
  const shown = data.filter((entry) => matches(entry, query.trim()));
  return (
    <div className="stack">
      <PageHead
        title="Audit log"
        subtitle="Detection, lifecycle changes, patch proposals, validation, human decisions, application, and verification. Newest first."
        actions={
          <>
            <span className="muted">
              {shown.length === data.length ? `${data.length} entries` : `${shown.length} of ${data.length} entries`}
            </span>
            <label className="search">
              <Icon name="search" size={14} />
              <span className="sr-only">Filter the audit log</span>
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter by actor, action, target…" />
            </label>
          </>
        }
      />
      <Card flush>
        {shown.length === 0 ? (
          <Empty>{data.length === 0 ? "Nothing recorded yet." : "No entry matches that filter."}</Empty>
        ) : (
          <div className="table-scroll">
            <table className="table table-cards">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Target</th>
                  <th>Result</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((entry) => (
                  <tr key={entry.audit_id}>
                    <td className="cell-meta nowrap muted">{dateTime(entry.created_at)}</td>
                    <td className="cell-meta nowrap">{titleCase(entry.actor.replace(/^human:/, "human · "))}</td>
                    <td>
                      <code>{entry.action}</code>
                    </td>
                    <td className="nowrap">
                      <span className="id">{entry.target}</span>
                    </td>
                    <td>
                      <Badge value={entry.result} />
                    </td>
                    <td className="cell-wide">
                      <span className="audit-details">
                        {Object.entries(entry.details)
                          .filter(([key]) => key !== "incident_id")
                          .map(([key, value]) => (
                            <span key={key}>
                              <span className="muted">{titleCase(key)}:</span>{" "}
                              {typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? (
                                <code>{value.slice(0, 12)}</code>
                              ) : typeof value === "object" ? (
                                JSON.stringify(value)
                              ) : (
                                String(value)
                              )}
                            </span>
                          ))}
                      </span>
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
