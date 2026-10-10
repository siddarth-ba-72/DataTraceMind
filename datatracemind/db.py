"""PostgreSQL access and schema management.

Two credentials exist. The application role owns every schema and is used by
the simulator, the ETL, and the repair executor. The read-only role can only
SELECT from the source, the analytics target, and the transform history; it
is the one handed to the investigation tools, so nothing an agent calls can
write, and nothing it calls can read DataTraceMind's own state.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import set_json_dumps

from datatracemind.config import SQL_DIR, get_settings

ANALYTICS_SCHEMA = "analytics"
STAGING_SCHEMA = "staging"
SOURCE_SCHEMA = "oltp"
READONLY_SCHEMAS = (SOURCE_SCHEMA, ANALYTICS_SCHEMA, "pipeline")
READONLY_STATEMENT_TIMEOUT_MS = 5000


def to_jsonable(value: Any) -> Any:
    """Convert database values into plain JSON types."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


set_json_dumps(lambda obj: json.dumps(to_jsonable(obj)))


_writes_forbidden = False


def forbid_write_connections() -> None:
    """Make this process unable to open a read-write connection.

    Called by the tool server, which runs code on behalf of a model.
    """
    global _writes_forbidden
    _writes_forbidden = True


@contextmanager
def connect(*, readonly: bool = False, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection that commits on success and rolls back on error."""
    if _writes_forbidden and not readonly:
        raise PermissionError("this process may only open read-only database connections")
    settings = get_settings()
    if readonly:
        url = settings.readonly_database_url
        options = (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={READONLY_STATEMENT_TIMEOUT_MS}"
        )
    else:
        url = settings.database_url
        options = None
    with psycopg.connect(url, row_factory=dict_row, autocommit=autocommit, options=options) as conn:
        yield conn


def fetch_all(query: str | sql.Composable, params: Any = None, *, readonly: bool = False) -> list[dict]:
    with connect(readonly=readonly) as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str | sql.Composable, params: Any = None, *, readonly: bool = False) -> dict | None:
    with connect(readonly=readonly) as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str | sql.Composable, params: Any = None) -> None:
    with connect() as conn:
        conn.execute(query, params)


def _read_sql(name: str) -> str:
    return (SQL_DIR / name).read_text()


def create_target_schema(conn: psycopg.Connection, schema: str) -> None:
    """Create the analytics tables in `schema` (idempotent)."""
    if schema not in (ANALYTICS_SCHEMA, STAGING_SCHEMA):
        raise ValueError(f"not a target schema: {schema}")
    conn.execute(_read_sql("target.sql").replace("__TARGET__", schema))


def rebuild_staging(conn: psycopg.Connection) -> None:
    """Drop and recreate the staging target so each validation starts empty."""
    conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(STAGING_SCHEMA)))
    create_target_schema(conn, STAGING_SCHEMA)


def _ensure_readonly_role(conn: psycopg.Connection) -> None:
    info = conninfo_to_dict(get_settings().readonly_database_url)
    role, password = info["user"], info["password"]
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
    verb = sql.SQL("ALTER") if exists else sql.SQL("CREATE")
    conn.execute(
        sql.SQL("{} ROLE {} LOGIN PASSWORD {}").format(verb, sql.Identifier(role), sql.Literal(password))
    )
    for schema in READONLY_SCHEMAS:
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {s} TO {r}; GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r}").format(
                s=sql.Identifier(schema), r=sql.Identifier(role)
            )
        )


def ensure_schema() -> None:
    """Create every schema, table, and role that is missing (idempotent)."""
    with connect() as conn:
        # Concurrent callers (tests, API startup) would otherwise race on
        # CREATE SCHEMA IF NOT EXISTS, which is not atomic in PostgreSQL.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('dtm_schema'))")
        conn.execute(_read_sql("oltp.sql"))
        conn.execute(_read_sql("pipeline.sql"))
        create_target_schema(conn, ANALYTICS_SCHEMA)
        conn.execute(_read_sql("dtm.sql"))
        _ensure_readonly_role(conn)


def drop_all() -> None:
    """Remove all demo data and state. Used only by the demo reset."""
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('dtm_schema'))")
        for schema in (SOURCE_SCHEMA, ANALYTICS_SCHEMA, STAGING_SCHEMA, "pipeline", "dtm"):
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
