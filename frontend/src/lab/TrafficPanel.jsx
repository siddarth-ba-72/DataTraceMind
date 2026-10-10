// Source traffic: the simulated customers behind the source database.
// One switch, one rate, and a count of what the stream has written.
import { api, useAction } from "../api.js";
import { number, since, titleCase } from "../format.js";
import { Badge, Button, Card, ErrorNote, Segmented } from "../components/common.jsx";

const BATCH_SIZE = 25;
const PROFILE_HINT = {
  light: "A quiet shop: easy to follow row by row",
  steady: "The default pace",
  busy: "A rush: differences grow visibly within seconds",
};

function Counter({ label, value }) {
  return (
    <div className="counter">
      <div className="counter-value">{number(value)}</div>
      <div className="counter-label">{label}</div>
    </div>
  );
}

export default function TrafficPanel({ traffic, etlInterval, onChanged }) {
  const action = useAction();
  if (!traffic) return <Card title="Source traffic">Loading…</Card>;

  const post = (path, body) =>
    action.run(async () => {
      await api(path, { method: "POST", body });
      await onChanged();
    });
  // A running stream adopts the new rate on its next transaction; a paused one
  // uses it when it is started.
  const setProfile = (profile) => post("/demo/traffic/rate", { profile });

  return (
    <Card
      title="Source traffic"
      subtitle="Simulated customers placing orders, paying, and asking for refunds in the source database."
      actions={
        <span className="traffic-state">
          {traffic.running ? (
            <span className="field-inline">
              <span className="dot dot-live" aria-hidden="true" /> Running · about {traffic.per_minute} per minute
            </span>
          ) : (
            <Badge tone="neutral">Paused</Badge>
          )}
        </span>
      }
    >
      <ErrorNote message={action.error} onDismiss={action.clearError} />
      <div className="traffic-controls">
        <Button
          kind={traffic.running ? "default" : "primary"}
          icon={traffic.running ? "pause" : "play"}
          busy={action.busy}
          onClick={() => post(traffic.running ? "/demo/traffic/stop" : "/demo/traffic/start")}
        >
          {traffic.running ? "Pause traffic" : "Start traffic"}
        </Button>
        <Button
          icon="send"
          busy={action.busy}
          onClick={() => post("/demo/traffic/batch", { size: BATCH_SIZE })}
          title={`Writes ${BATCH_SIZE} transactions at once, whether or not the stream is running`}
        >
          Send {BATCH_SIZE} now
        </Button>
        <span className="field-inline traffic-rate">
          Rate
          <Segmented
            label="Traffic rate"
            value={traffic.profile}
            disabled={action.busy}
            onChange={setProfile}
            options={traffic.profiles.map((profile) => ({
              value: profile.name,
              label: titleCase(profile.name),
              count: `${profile.per_minute}/min`,
              title: PROFILE_HINT[profile.name],
            }))}
          />
        </span>
      </div>

      <div className="counters">
        <Counter label="Orders placed" value={traffic.counts.orders_placed} />
        <Counter label="Refunds requested" value={traffic.counts.refunds_requested} />
        <Counter label="Refunds settled" value={traffic.counts.refunds_settled} />
      </div>
      <p className="muted traffic-caption">
        Written by this lab since the last reset.{" "}
        {traffic.last_action_at ? `Last transaction ${since(traffic.last_action_at)}.` : "Nothing sent yet."}
      </p>

      <ul className="explain">
        <li>
          <strong>Optional.</strong> Every scenario reproduces on the seeded baseline with traffic paused.
        </li>
        <li>
          New rows reach analytics on the next pipeline run, every {number(etlInterval)} seconds.
        </li>
        <li>
          With a fault injected, the error keeps growing while traffic runs, as it would in production.
        </li>
        <li>A reset pauses traffic, so every run starts from the same baseline.</li>
      </ul>

    </Card>
  );
}
