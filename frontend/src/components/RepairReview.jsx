// Repair review: the diff, why it is believed correct, the staging proof, and
// the human decision. The server enforces every rule; this page only shows them.
import { useState } from "react";
import { api, useAction } from "../api.js";
import { dateTime, money, number, signedMoney } from "../format.js";
import { Badge, Button, Card, ErrorNote, KeyValue } from "./common.jsx";

const STAGES = ["proposed", "validated", "approved", "applied", "verified"];
const REACHED = {
  proposed: 1,
  validation_failed: 1,
  validated: 2,
  rejected: 2,
  approved: 3,
  applied: 4,
  application_failed: 4,
  verified: 5,
  superseded: 0,
};

function Diff({ text }) {
  return (
    <pre className="diff" aria-label="Patch diff">
      {text.split("\n").map((line, index) => {
        const kind = line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("@@") ? "hunk" : "ctx";
        return (
          <span key={index} className={`diff-${kind}`}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}

function BeforeAfter({ data }) {
  if (!data?.before) return null;
  const currencies = Object.keys(data.before.by_currency);
  const cell = (side, currency) =>
    side.by_currency[currency].within_tolerance ? (
      <Badge tone="good">Matches</Badge>
    ) : (
      <Badge tone="critical">{signedMoney(side.by_currency[currency].net_difference, currency)}</Badge>
    );
  return (
    <div className="table-scroll">
      <table className="table table-inset numeric">
        <thead>
          <tr>
            <th>Currency</th>
            <th className="right">Source net</th>
            <th className="right">Analytics now</th>
            <th className="right">Staging, patched</th>
            <th>Now vs source</th>
            <th>Staging vs source</th>
          </tr>
        </thead>
        <tbody>
          {currencies.map((currency) => (
            <tr key={currency}>
              <td>{currency}</td>
              <td className="right">{money(data.before.by_currency[currency].source.net, currency)}</td>
              <td className="right">{money(data.before.by_currency[currency].analytics.net, currency)}</td>
              <td className="right">{money(data.after.by_currency[currency].analytics.net, currency)}</td>
              <td>{cell(data.before, currency)}</td>
              <td>{cell(data.after, currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Staging loads the same rows a second time; an idempotent load changes nothing.
function Replay({ replay }) {
  if (!replay?.run_id) return null;
  return (
    <p className="note">
      <Badge tone={replay.passed ? "good" : "critical"}>{replay.passed ? "Replay safe" : "Replay failed"}</Badge>
      <span>
        Staging run {replay.run_id} loaded the same {number(replay.loaded)} rows a second time, as a retry would.{" "}
        {replay.passed
          ? "Every quality check still passes, so the load is idempotent."
          : `It then failed ${replay.failed_checks.join(", ") || "to complete"}.`}
      </span>
    </p>
  );
}

function Decision({ incident, proposal, onChanged }) {
  const [approver, setApprover] = useState("");
  const [reason, setReason] = useState("");
  const action = useAction();
  const body = { patch_hash: proposal.patch_hash, approver: approver.trim() };
  const decide = (path, extra = {}) =>
    action.run(async () => {
      await api(`/incidents/${incident.incident_id}/${path}`, { method: "POST", body: { ...body, ...extra } });
      await onChanged();
    });

  return (
    <div className="decision">
      <h3>Your decision</h3>
      <p>
        Approving applies <strong>exactly this patch</strong> (hash <code>{proposal.patch_hash.slice(0, 12)}</code>) to the demo
        environment, reruns the pipeline, and verifies recovery. Nothing is applied without it.
      </p>
      <ErrorNote message={action.error} onDismiss={action.clearError} />
      <div className="decision-fields">
        <label>
          Your name
          <input value={approver} onChange={(event) => setApprover(event.target.value)} placeholder="Name recorded in the audit log" maxLength={60} />
        </label>
        <label>
          Reason, if rejecting
          <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Optional" maxLength={500} />
        </label>
      </div>
      <div className="decision-actions">
        <Button kind="primary" icon="check" busy={action.busy} disabled={!approver.trim()} onClick={() => decide("approve")}>
          Approve and apply
        </Button>
        <Button kind="danger" busy={action.busy} disabled={!approver.trim()} onClick={() => decide("reject", { reason: reason.trim() || null })}>
          Reject
        </Button>
      </div>
    </div>
  );
}

export default function RepairReview({ incident, onChanged }) {
  const proposal = incident.proposal;
  if (!proposal) return null;
  const validation = proposal.validation_result;
  const postFix = proposal.post_fix_result;
  const reached = REACHED[proposal.status] ?? 0;
  const approval = incident.approvals.findLast((a) => a.proposal_id === proposal.proposal_id);
  const failedChecks = validation?.checks.filter((c) => !c.passed) || [];

  return (
    <Card
      id="repair"
      title={`Repair review · ${proposal.proposal_id}`}
      subtitle={`Patch ${proposal.patch_hash.slice(0, 12)} against transformation version ${proposal.base_version} · proposed ${dateTime(proposal.created_at)}`}
      actions={<Badge value={proposal.status} />}
    >
      <ol className="stages" aria-label="Repair status">
        {STAGES.map((stage, index) => (
          <li key={stage} className={index < reached ? "stage-done" : ""}>
            <span aria-hidden="true">{index < reached ? "✓" : index + 1}</span> {stage}
          </li>
        ))}
      </ol>

      <h3>Patch · {proposal.target_file}</h3>
      <Diff text={proposal.diff} />

      <KeyValue
        rows={[
          ["Why this fixes the root cause", proposal.rationale],
          ["Risk", proposal.risk],
          ["Rollback", proposal.rollback_plan],
          ["Evidence relied on", (proposal.cited_evidence.evidence || []).join(", ") || "–"],
        ]}
      />

      {validation && (
        <>
          <h3>
            Staging validation <Badge tone={validation.passed ? "good" : "critical"}>{validation.passed ? "Passed" : "Failed"}</Badge>
          </h3>
          <p className="muted">
            Deterministic: the patched transformation was run against a staging copy
            {validation.staging_run_id ? ` (staging run ${validation.staging_run_id}, ${validation.staging_status})` : ""}. No model decides the result.
          </p>
          {validation.failures.length > 0 && (
            <ul className="failures">
              {validation.failures.map((failure) => (
                <li key={failure}>{failure}</li>
              ))}
            </ul>
          )}
          <BeforeAfter data={validation.before_after} />
          <Replay replay={validation.replay} />
          <div className="columns">
            <div>
              <h4>
                Regression cases · {validation.regression.filter((c) => c.passed).length} of {validation.regression.length} pass
              </h4>
              <ul className="checklist">
                {validation.regression.map((item) => (
                  <li key={item.name} title={JSON.stringify({ expected: item.expected, actual: item.actual }, null, 1)}>
                    <Badge tone={item.passed ? "good" : "critical"}>{item.passed ? "Pass" : "Fail"}</Badge> {item.name}
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h4>
                Quality checks in staging · {validation.checks.length - failedChecks.length} of {validation.checks.length} pass
              </h4>
              <ul className="checklist">
                {validation.checks.map((check) => (
                  <li key={check.check_name} title={check.expected_condition}>
                    <Badge tone={check.passed ? "good" : "critical"}>{check.passed ? "Pass" : "Fail"}</Badge> {check.check_name}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </>
      )}

      {approval && (
        <p className="note">
          <Badge value={approval.decision} tone={approval.decision === "approved" ? "good" : "neutral"} /> by{" "}
          <strong>{approval.actor}</strong> on {dateTime(approval.decided_at)} for patch <code>{approval.patch_hash.slice(0, 12)}</code>
          {approval.reason ? ` — “${approval.reason}”` : ""}
        </p>
      )}

      {postFix && (
        <>
          <h3>
            Post-fix verification <Badge tone={postFix.passed ? "good" : "critical"}>{postFix.passed ? "Recovered" : "Failed"}</Badge>
          </h3>
          <p>
            Pipeline run {postFix.etl_run_id} finished {postFix.etl_status} on transformation version {postFix.transform_version}.{" "}
            {postFix.passed
              ? `All ${postFix.checks.length} quality checks pass.`
              : `Failed checks: ${postFix.failed_checks.join(", ") || postFix.etl_error}. The previous transformation was redeployed (version ${postFix.rolled_back_to_version}).`}
          </p>
        </>
      )}

      {incident.status === "awaiting_approval" && proposal.status === "validated" && (
        <Decision incident={incident} proposal={proposal} onChanged={onChanged} />
      )}
    </Card>
  );
}
