-- DataTraceMind's own state: incidents, the agent timeline, evidence, repair
-- proposals, approvals, and the audit log. This is the application's record
-- for the UI and for approval enforcement; it does not depend on trace
-- export. The read-only investigation role has no access to this schema, so
-- agents cannot read scenario ground truth or approval state through SQL.

CREATE SCHEMA IF NOT EXISTS dtm;

CREATE SEQUENCE IF NOT EXISTS dtm.incident_seq;
CREATE SEQUENCE IF NOT EXISTS dtm.proposal_seq;

CREATE TABLE IF NOT EXISTS dtm.incidents (
    incident_id    TEXT PRIMARY KEY,
    incident_type  TEXT NOT NULL,
    severity       TEXT NOT NULL,
    status         TEXT NOT NULL,
    summary        TEXT NOT NULL,
    detected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at    TIMESTAMPTZ,
    trigger        JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    target_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Workflow state that has no table of its own: hypotheses, the selected
    -- root cause, applicable business rules, errors, trace references.
    state          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Evaluator-only: which lab scenario was active at detection. Never put
    -- in a prompt or a tool result.
    scenario_id    TEXT
);

CREATE TABLE IF NOT EXISTS dtm.agent_events (
    event_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES dtm.incidents (incident_id) ON DELETE CASCADE,
    agent       TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    status      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_events_incident_idx ON dtm.agent_events (incident_id, event_id);

CREATE TABLE IF NOT EXISTS dtm.evidence (
    incident_id    TEXT NOT NULL REFERENCES dtm.incidents (incident_id) ON DELETE CASCADE,
    evidence_id    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    source         TEXT NOT NULL,
    description    TEXT NOT NULL,
    query_or_tool  TEXT NOT NULL,
    arguments      JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary JSONB NOT NULL,
    collected_by   TEXT NOT NULL,
    observed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (incident_id, evidence_id)
);

-- status: proposed | validated | validation_failed | approved | rejected
--         | applied | verified | application_failed | superseded
CREATE TABLE IF NOT EXISTS dtm.repair_proposals (
    proposal_id       TEXT PRIMARY KEY,
    incident_id       TEXT NOT NULL REFERENCES dtm.incidents (incident_id) ON DELETE CASCADE,
    target_file       TEXT NOT NULL,
    base_version      INTEGER NOT NULL,
    base_hash         TEXT NOT NULL,
    new_content       TEXT NOT NULL,
    diff              TEXT NOT NULL,
    patch_hash        TEXT NOT NULL,
    rationale         TEXT NOT NULL,
    risk              TEXT NOT NULL,
    rollback_plan     TEXT NOT NULL,
    cited_evidence    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status            TEXT NOT NULL,
    validation_result JSONB,
    post_fix_result   JSONB,
    applied_version   INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per human decision. Approval is bound to the patch hash recorded
-- here; the executor compares it with the proposal's current hash.
CREATE TABLE IF NOT EXISTS dtm.approvals (
    approval_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES dtm.incidents (incident_id) ON DELETE CASCADE,
    proposal_id TEXT NOT NULL REFERENCES dtm.repair_proposals (proposal_id) ON DELETE CASCADE,
    patch_hash  TEXT NOT NULL,
    decision    TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    actor       TEXT NOT NULL,
    reason      TEXT,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dtm.audit_log (
    audit_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL,
    result     TEXT NOT NULL,
    details    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dtm.alerts (
    alert_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES dtm.incidents (incident_id) ON DELETE CASCADE,
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per investigation run: what the before/after comparison and the
-- cost-per-investigation export are built from.
CREATE TABLE IF NOT EXISTS dtm.investigation_runs (
    run_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id         TEXT NOT NULL REFERENCES dtm.incidents (incident_id) ON DELETE CASCADE,
    scenario_id         TEXT,
    workflow_version    TEXT NOT NULL,
    model               TEXT NOT NULL,
    trace_id            TEXT,
    status              TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    duration_s          NUMERIC(10, 2),
    role_turns          INTEGER NOT NULL DEFAULT 0,
    tool_calls          INTEGER NOT NULL DEFAULT 0,
    failed_tool_calls   INTEGER NOT NULL DEFAULT 0,
    retries             INTEGER NOT NULL DEFAULT 0,
    input_tokens        BIGINT NOT NULL DEFAULT 0,
    cached_input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens       BIGINT NOT NULL DEFAULT 0,
    est_cost_usd        NUMERIC(10, 4) NOT NULL DEFAULT 0
);

-- Scenario lab state (single row). Evaluator/demo-harness only.
CREATE TABLE IF NOT EXISTS dtm.lab_state (
    id                     INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    scenario_id            TEXT,
    injected_at            TIMESTAMPTZ,
    baseline_metrics       JSONB,
    post_injection_metrics JSONB
);

INSERT INTO dtm.lab_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
