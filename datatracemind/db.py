"""PostgreSQL access and schema management.

Two credentials exist. The application role owns every schema and is used by
the simulator, the ETL, and the repair executor. The read-only role can only
SELECT from the source, the analytics target, and the transform history; it
is the one handed to the investigation tools, so nothing an agent calls can
write, and nothing it calls can read DataTraceMind's own state.

Application-role connections are kept open and reused, because opening one
costs far more than a query when the database is hosted elsewhere. A reused
connection keeps its session, so nothing may depend on session state: use
SET LOCAL and transaction-scoped advisory locks, as a connection pooler in
front of the database already requires. Read-only connections are opened for
each call and closed after it, so SQL run on behalf of a model cannot leave a
setting or a lock behind for the next call.
"""

from __future__ import annotations

import atexit
import functools
import json
import logging
import threading
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
from psycopg_pool import ConnectionPool

from datatracemind.config import SQL_DIR, get_settings

ANALYTICS_SCHEMA = "analytics"
STAGING_SCHEMA = "staging"
SOURCE_SCHEMA = "oltp"
READONLY_SCHEMAS = (SOURCE_SCHEMA, ANALYTICS_SCHEMA, "pipeline")
READONLY_STATEMENT_TIMEOUT_MS = 5000
# Session defaults of the read-only role.
READONLY_ROLE_SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": str(READONLY_STATEMENT_TIMEOUT_MS),
}
# Most connections the application keeps open at once. A pipeline run or an
# injection holds several at a time (the pipeline lock is one of them), on top
# of whatever the API is serving.
POOL_MAX_SIZE = 20

# The pool logs every checkout and return at INFO.
logging.getLogger("psycopg.pool").setLevel(logging.WARNING)


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


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def _check(url: str, conn: psycopg.Connection) -> None:
    """Test a kept connection before it is handed out; the pool replaces one that fails.

    Connections are usually lost together (the database restarted, the network
    changed), so the first failure replaces all the idle ones at once. Left to
    itself the pool tries them one by one, pausing longer after each.
    """
    try:
        ConnectionPool.check_connection(conn)
    except Exception:
        pool = _pools.get(url)
        if pool is not None:
            pool.drain()
        raise


def _pool(url: str) -> ConnectionPool:
    """The open application-role connections to `url`. Connections are made as they are needed."""
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            pool = _pools[url] = ConnectionPool(
                url,
                # No server-side prepared statements: a reset recreates every
                # table and the schema-drift scenario renames a column, and a
                # statement prepared before either would fail after it.
                kwargs={"row_factory": dict_row, "prepare_threshold": None},
                min_size=0,
                max_size=POOL_MAX_SIZE,
                check=functools.partial(_check, url),
                name="dtm",
                open=True,
            )
        return pool


def close_pools() -> None:
    """Close every kept connection. The next `connect()` opens new ones."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


@contextmanager
def connect(*, readonly: bool = False, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """Yield a connection that commits on success and rolls back on error."""
    if _writes_forbidden and not readonly:
        raise PermissionError("this process may only open read-only database connections")
    settings = get_settings()
    url = settings.readonly_database_url if readonly else settings.database_url
    if url is None:
        raise RuntimeError("DTM_DATABASE_URL is not set; put the PostgreSQL connection string in .env")
    if readonly or autocommit:
        with psycopg.connect(url, row_factory=dict_row, autocommit=autocommit) as conn:
            yield conn
    else:
        with _pool(url).connection() as conn:
            # What the last caller set on the connection (the ETL reads the
            # source read-only at repeatable read) must not reach this one.
            conn.autocommit = False
            conn.isolation_level = conn.read_only = conn.deferrable = None
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
    # Set on the role rather than on each connection: a hosted connection
    # pooler does not pass startup options through to the server.
    for name, value in READONLY_ROLE_SETTINGS.items():
        conn.execute(
            sql.SQL("ALTER ROLE {} SET {} = {}").format(sql.Identifier(role), sql.Identifier(name), sql.Literal(value))
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
