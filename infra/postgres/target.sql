-- Analytics target. Applied once per target schema: `analytics` (the demo
-- environment) and `staging` (rebuilt for every repair validation run).
-- __TARGET__ is replaced with the schema name by datatracemind.db.
--
-- Fact tables deliberately have no unique constraint on the business key.
-- Uniqueness is the load step's job (replace-by-key) and is verified by a
-- data-quality check, so a non-idempotent load shows up as duplicates
-- instead of being masked by the database.

CREATE SCHEMA IF NOT EXISTS __TARGET__;

CREATE TABLE IF NOT EXISTS __TARGET__.fact_orders (
    row_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id          BIGINT NOT NULL,
    customer_id       BIGINT NOT NULL,
    order_status      TEXT NOT NULL,
    total_amount      NUMERIC(12, 2) NOT NULL,
    currency          CHAR(3) NOT NULL,
    order_date        DATE NOT NULL,
    payment_count     INTEGER NOT NULL,
    captured_amount   NUMERIC(12, 2) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL,
    etl_run_id        BIGINT NOT NULL,
    transform_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS __TARGET__.fact_payments (
    row_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id        BIGINT NOT NULL,
    order_id          BIGINT NOT NULL,
    payment_status    TEXT NOT NULL,
    amount            NUMERIC(12, 2) NOT NULL,
    currency          CHAR(3) NOT NULL,
    paid_at           TIMESTAMPTZ NOT NULL,
    revenue_date      DATE NOT NULL,
    is_revenue        BOOLEAN NOT NULL,
    etl_run_id        BIGINT NOT NULL,
    transform_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS __TARGET__.fact_refunds (
    row_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    refund_id         BIGINT NOT NULL,
    payment_id        BIGINT NOT NULL,
    order_id          BIGINT NOT NULL,
    refund_status     TEXT NOT NULL,
    refund_amount     NUMERIC(12, 2) NOT NULL,
    currency          CHAR(3) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL,
    revenue_date      DATE NOT NULL,
    is_subtracted     BOOLEAN NOT NULL,
    subtracted_amount NUMERIC(12, 2) NOT NULL,
    etl_run_id        BIGINT NOT NULL,
    transform_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS fact_orders_order_idx ON __TARGET__.fact_orders (order_id);
CREATE INDEX IF NOT EXISTS fact_payments_order_idx ON __TARGET__.fact_payments (order_id);
CREATE INDEX IF NOT EXISTS fact_refunds_order_idx ON __TARGET__.fact_refunds (order_id);

-- One row per business day and currency; rebuilt from the fact tables.
CREATE TABLE IF NOT EXISTS __TARGET__.daily_revenue (
    revenue_date      DATE NOT NULL,
    currency          CHAR(3) NOT NULL,
    gross_paid_amount NUMERIC(14, 2) NOT NULL,
    refund_amount     NUMERIC(14, 2) NOT NULL,
    net_revenue       NUMERIC(14, 2) NOT NULL,
    order_count       INTEGER NOT NULL,
    refreshed_at      TIMESTAMPTZ NOT NULL,
    transform_version INTEGER NOT NULL,
    PRIMARY KEY (revenue_date, currency)
);

-- status: RUNNING | SUCCESS | FAILED
CREATE TABLE IF NOT EXISTS __TARGET__.etl_run_log (
    run_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trigger           TEXT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL,
    transform_version INTEGER,
    full_refresh      BOOLEAN NOT NULL DEFAULT false,
    extracted_count   INTEGER NOT NULL DEFAULT 0,
    transformed_count INTEGER NOT NULL DEFAULT 0,
    rejected_count    INTEGER NOT NULL DEFAULT 0,
    loaded_count      INTEGER NOT NULL DEFAULT 0,
    watermark         TIMESTAMPTZ,
    error_summary     TEXT,
    details           JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS __TARGET__.data_quality_results (
    result_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id             BIGINT NOT NULL,
    check_name         TEXT NOT NULL,
    passed             BOOLEAN NOT NULL,
    severity           TEXT NOT NULL,
    observed_value     JSONB NOT NULL,
    expected_condition TEXT NOT NULL,
    checked_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS data_quality_results_run_idx ON __TARGET__.data_quality_results (run_id);

-- High-water mark of the last successful load, and the transform version
-- that produced the current fact rows.
CREATE TABLE IF NOT EXISTS __TARGET__.etl_checkpoint (
    pipeline_name     TEXT PRIMARY KEY,
    watermark         TIMESTAMPTZ,
    transform_version INTEGER,
    last_run_id       BIGINT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
