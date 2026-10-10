// Audit timeline: who did what, to what, and with what result.
import { usePoll } from "../api.js";
import { dateTime, titleCase } from "../format.js";
import { Badge, Card, Empty } from "./common.jsx";

export default function Audit() {
  const { data } = usePoll("/audit?limit=300", 4000);
  if (!data) return <Empty>Loading the audit log…</Empty>;
  return (
    <Card
      title="Audit log"
      subtitle="Detection, lifecycle changes, patch proposals, validation, human decisions, application, and verification. Newest first."
    >
      {data.length === 0 ? (
        <Empty>Nothing recorded yet.</Empty>
      ) : (
        <div className="table-scroll">
          <table className="table">
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
              {data.map((entry) => (
                <tr key={entry.audit_id}>
                  <td className="nowrap muted">{dateTime(entry.created_at)}</td>
                  <td className="nowrap">{titleCase(entry.actor.replace(/^human:/, "human · "))}</td>
                  <td>
                    <code>{entry.action}</code>
                  </td>
                  <td className="nowrap">{entry.target}</td>
                  <td>
                    <Badge value={entry.result} />
                  </td>
                  <td className="audit-details">
                    {Object.entries(entry.details)
                      .filter(([key]) => key !== "incident_id")
                      .map(([key, value]) => (
                        <span key={key}>
                          <span className="muted">{titleCase(key)}:</span>{" "}
                          {typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? <code>{value.slice(0, 12)}</code> : typeof value === "object" ? JSON.stringify(value) : String(value)}
                        </span>
                      ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
