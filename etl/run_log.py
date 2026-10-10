"""Run metadata for the ETL: the run log, quality results, and the checkpoint."""

from __future__ import annotations

from datetime import datetime

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from datatracemind.config import PIPELINE_NAME
from datatracemind.db import connect
from etl.quality_checks import CheckResult


def _table(schema: str, name: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def start_run(schema: str, *, trigger: str, transform_version: int | None, full_refresh: bool) -> int:
    """Insert a RUNNING row in its own transaction, so it is visible at once."""
    with connect() as conn:
        row = conn.execute(
            sql.SQL(
                "INSERT INTO {} (trigger, status, transform_version, full_refresh) "
                "VALUES (%s, 'RUNNING', %s, %s) RETURNING run_id"
            ).format(_table(schema, "etl_run_log")),
            (trigger, transform_version, full_refresh),
        ).fetchone()
    return row["run_id"]


def finish_run(
    conn: psycopg.Connection,
    schema: str,
    run_id: int,
    *,
    status: str,
    full_refresh: bool = False,
    extracted: int = 0,
    transformed: int = 0,
    rejected: int = 0,
    loaded: int = 0,
    watermark: datetime | None = None,
    error_summary: str | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        sql.SQL(
            """
            UPDATE {} SET status = %s, finished_at = clock_timestamp(), full_refresh = %s,
                extracted_count = %s, transformed_count = %s, rejected_count = %s, loaded_count = %s,
                watermark = %s, error_summary = %s, details = %s
            WHERE run_id = %s
            """
        ).format(_table(schema, "etl_run_log")),
        (
            status,
            full_refresh,
            extracted,
            transformed,
            rejected,
            loaded,
            watermark,
            error_summary,
            Jsonb(details or {}),
            run_id,
        ),
    )


def record_checks(conn: psycopg.Connection, schema: str, run_id: int, checks: list[CheckResult]) -> None:
    conn.cursor().executemany(
        sql.SQL(
            "INSERT INTO {} (run_id, check_name, passed, severity, observed_value, expected_condition) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        ).format(_table(schema, "data_quality_results")),
        [(run_id, c.name, c.passed, c.severity, Jsonb(c.observed), c.expected) for c in checks],
    )


def get_checkpoint(conn: psycopg.Connection, schema: str) -> dict | None:
    return conn.execute(
        sql.SQL("SELECT * FROM {} WHERE pipeline_name = %s").format(_table(schema, "etl_checkpoint")),
        (PIPELINE_NAME,),
    ).fetchone()


def set_checkpoint(
    conn: psycopg.Connection, schema: str, *, watermark: datetime, transform_version: int, run_id: int
) -> None:
    conn.execute(
        sql.SQL(
            """
            INSERT INTO {} (pipeline_name, watermark, transform_version, last_run_id, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (pipeline_name) DO UPDATE SET watermark = EXCLUDED.watermark,
                transform_version = EXCLUDED.transform_version, last_run_id = EXCLUDED.last_run_id,
                updated_at = now()
            """
        ).format(_table(schema, "etl_checkpoint")),
        (PIPELINE_NAME, watermark, transform_version, run_id),
    )


def recent_runs(conn: psycopg.Connection, schema: str, *, limit: int = 20) -> list[dict]:
    """Latest runs, newest first, each with its quality results."""
    runs = conn.execute(
        sql.SQL("SELECT * FROM {} ORDER BY run_id DESC LIMIT %s").format(_table(schema, "etl_run_log")),
        (limit,),
    ).fetchall()
    if not runs:
        return []
    checks = conn.execute(
        sql.SQL(
            "SELECT run_id, check_name, passed, severity, observed_value, expected_condition "
            "FROM {} WHERE run_id = ANY(%s) ORDER BY result_id"
        ).format(_table(schema, "data_quality_results")),
        ([r["run_id"] for r in runs],),
    ).fetchall()
    by_run: dict[int, list[dict]] = {}
    for check in checks:
        by_run.setdefault(check.pop("run_id"), []).append(check)
    for run in runs:
        run["checks"] = by_run.get(run["run_id"], [])
    return runs


def latest_run(conn: psycopg.Connection, schema: str, *, finished_only: bool = True) -> dict | None:
    condition = sql.SQL("WHERE status <> 'RUNNING'") if finished_only else sql.SQL("")
    return conn.execute(
        sql.SQL("SELECT * FROM {} {} ORDER BY run_id DESC LIMIT 1").format(
            _table(schema, "etl_run_log"), condition
        )
    ).fetchone()
