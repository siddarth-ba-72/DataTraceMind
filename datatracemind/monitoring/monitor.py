"""Deterministic detection: turns failed quality checks into incidents.

No model is involved in deciding that something is wrong. The monitor reads
the checks recorded for an ETL run (and the freshness of the target), and
opens one incident with a structured trigger, a severity from fixed
thresholds, and an in-app alert. While an incident is unresolved, further
failures are treated as the same incident rather than opening new ones.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from datatracemind import store
from datatracemind.config import get_settings
from datatracemind.db import ANALYTICS_SCHEMA, connect
from etl.pipeline import RunResult
from etl.quality_checks import CheckResult

logger = logging.getLogger(__name__)

# A net-revenue difference at or above this share of source revenue is critical.
CRITICAL_REVENUE_SHARE = 0.01

# First failed check that matches decides the incident type. Duplicate keys
# come before the revenue check: duplicated rows inflate revenue, never the
# other way round, so they are the more specific finding.
_TYPE_BY_CHECK = (
    ("pipeline_run_succeeded", "pipeline_failure"),
    ("unique_business_keys", "duplicate_records"),
    ("net_revenue_reconciliation", "revenue_mismatch"),
    ("fact_orders_completeness", "record_loss"),
    ("fact_payments_completeness", "record_loss"),
    ("fact_refunds_completeness", "record_loss"),
    ("order_count_reconciliation", "record_loss"),
    ("payment_revenue_eligibility", "rule_violation"),
    ("refund_subtraction_eligibility", "rule_violation"),
    ("source_schema_contract", "schema_drift"),
)

_METRIC_BY_TYPE = {
    "pipeline_failure": "order_revenue pipeline",
    "revenue_mismatch": "daily_revenue.net_revenue",
    "record_loss": "fact tables / daily_revenue.order_count",
    "duplicate_records": "fact tables",
    "rule_violation": "fact row eligibility flags",
    "schema_drift": "oltp source schema",
    "stale_data": "daily_revenue freshness",
    "data_quality": "analytics target",
}


def classify(failed: list[CheckResult]) -> str:
    names = {c.name for c in failed}
    for check_name, incident_type in _TYPE_BY_CHECK:
        if check_name in names:
            return incident_type
    return "data_quality"


def _revenue_gaps(check: CheckResult) -> list[tuple[str, float, float]]:
    """Currencies that do not reconcile: (currency, difference, share of source net), largest share first."""
    gaps = []
    for currency, sides in check.observed["by_currency"].items():
        if sides["within_tolerance"]:
            continue
        base = abs(sides["source"]["net"]) or 1.0
        gaps.append((currency, sides["net_difference"], abs(sides["net_difference"]) / base))
    return sorted(gaps, key=lambda gap: -gap[2])


def assess(incident_type: str, result: RunResult) -> tuple[str, str]:
    """Severity and one-line summary for an incident, from fixed rules."""
    by_name = {c.name: c for c in result.checks}
    declared = f"ETL run {result.run_id} reported {result.status}"
    if incident_type == "pipeline_failure":
        return "high", f"ETL run {result.run_id} failed: {result.error_summary}"
    if incident_type == "revenue_mismatch":
        reconciliation = by_name["net_revenue_reconciliation"]
        gaps = _revenue_gaps(reconciliation)
        if not gaps:
            days = reconciliation.observed["mismatched_day_count"]
            return "high", f"Net revenue is assigned to the wrong day on {days} days; totals reconcile; {declared}."
        severity = "critical" if gaps[0][2] >= CRITICAL_REVENUE_SHARE else "high"
        parts = [
            f"{abs(difference):,.2f} {currency} {'higher' if difference > 0 else 'lower'} ({share:.1%})"
            for currency, difference, share in gaps
        ]
        return severity, (
            f"Analytics net revenue differs from the source-derived value: {' and '.join(parts)}; {declared}."
        )
    if incident_type == "record_loss":
        missing = sum(
            c.observed.get("missing_in_analytics", 0) for c in result.failed_checks if c.name.endswith("_completeness")
        )
        return "high", f"{missing} source rows are missing from the analytics fact tables; {declared}."
    if incident_type == "duplicate_records":
        extra = sum(t["extra_rows"] for t in by_name["unique_business_keys"].observed.values())
        reconciliation = by_name.get("net_revenue_reconciliation")
        gaps = _revenue_gaps(reconciliation) if reconciliation else []
        if not gaps:
            return "high", f"{extra} duplicate fact rows share a business key; {declared}."
        severity = "critical" if gaps[0][2] >= CRITICAL_REVENUE_SHARE else "high"
        parts = [
            f"{abs(difference):,.2f} {currency} {'higher' if difference > 0 else 'lower'} ({share:.1%})"
            for currency, difference, share in gaps
        ]
        return severity, (
            f"{extra} duplicate fact rows share a business key; analytics net revenue is "
            f"{' and '.join(parts)} than the source-derived value; {declared}."
        )
    if incident_type == "schema_drift":
        return "medium", f"The source schema no longer matches its contract; {declared}."
    failed = ", ".join(c.name for c in result.failed_checks)
    return "medium", f"Data-quality checks failed ({failed}); {declared}."


def _last_successful_refresh(conn) -> datetime | None:
    row = conn.execute(f"SELECT max(refreshed_at) AS at FROM {ANALYTICS_SCHEMA}.daily_revenue").fetchone()
    return row["at"]


def _open_incident(
    conn, *, incident_type: str, severity: str, summary: str, trigger: dict, source: dict, target: dict
) -> str:
    scenario_id = conn.execute("SELECT scenario_id FROM dtm.lab_state WHERE id = 1").fetchone()["scenario_id"]
    incident_id = store.create_incident(
        conn,
        incident_type=incident_type,
        severity=severity,
        summary=summary,
        trigger=trigger,
        source_metrics=source,
        target_metrics=target,
        scenario_id=scenario_id,
    )
    conn.execute(
        "INSERT INTO dtm.alerts (incident_id, severity, title, body) VALUES (%s, %s, %s, %s)",
        (
            incident_id,
            severity,
            summary,
            Jsonb(
                {
                    "incident_id": incident_id,
                    "incident_type": incident_type,
                    "affected": _METRIC_BY_TYPE[incident_type],
                    "expected": source,
                    "observed": target,
                    "last_successful_refresh": _last_successful_refresh(conn),
                    "status": "detected",
                }
            ),
        ),
    )
    logger.info("opened %s (%s, %s): %s", incident_id, incident_type, severity, summary)
    return incident_id


def evaluate_run(result: RunResult) -> str | None:
    """Open an incident if this run failed a check. Returns the new incident ID."""
    if result.target_schema != ANALYTICS_SCHEMA or not result.failed_checks:
        return None
    incident_type = classify(result.failed_checks)
    severity, summary = assess(incident_type, result)
    reconciliation = next((c for c in result.checks if c.name == "net_revenue_reconciliation"), None)
    by_currency = reconciliation.observed["by_currency"] if reconciliation else {}
    trigger = {
        "detected_by": "post-run quality checks",
        "etl_run_id": result.run_id,
        "etl_status": result.status,
        "etl_error": result.error_summary,
        "transform_version": result.transform_version,
        "failed_checks": [c.as_dict() for c in result.failed_checks],
    }
    with connect() as conn:
        # One transaction and one lock: two evaluations cannot both decide
        # that no incident is open.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('dtm_monitor'))")
        if store.unresolved_incident(conn) is not None:
            return None
        return _open_incident(
            conn,
            incident_type=incident_type,
            severity=severity,
            summary=summary,
            trigger=trigger,
            source={c: v["source"] for c, v in by_currency.items()},
            target={c: v["analytics"] for c, v in by_currency.items()},
        )


def evaluate_freshness() -> str | None:
    """Open a stale-data incident when the target has not refreshed in time."""
    objective = get_settings().freshness_objective_s
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('dtm_monitor'))")
        refreshed_at = _last_successful_refresh(conn)
        if refreshed_at is None:
            return None
        age = (datetime.now(UTC) - refreshed_at).total_seconds()
        if age <= objective or store.unresolved_incident(conn) is not None:
            return None
        check = {
            "check_name": "freshness",
            "passed": False,
            "severity": "medium",
            "observed_value": {"refreshed_at": refreshed_at.isoformat(), "age_seconds": round(age)},
            "expected_condition": f"daily_revenue is refreshed at least every {objective:.0f} seconds",
        }
        return _open_incident(
            conn,
            incident_type="stale_data",
            severity="medium",
            summary=f"daily_revenue was last refreshed {age:.0f}s ago (objective {objective:.0f}s).",
            trigger={"detected_by": "freshness monitor", "failed_checks": [check]},
            source={},
            target={},
        )
