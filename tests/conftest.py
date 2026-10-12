"""Test setup: run everything against a separate database.

Tests reset the demo environment freely, so they must never point at the
database a running demo is using. They create a dedicated `datatracemind_test`
database on the server that DTM_DATABASE_URL names, or on the one that
DTM_TEST_ADMIN_URL names when the tests should use a different server.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv
from psycopg.conninfo import make_conninfo

TEST_DATABASE = "datatracemind_test"


def pytest_configure(config: pytest.Config) -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    server = os.environ.get("DTM_TEST_ADMIN_URL") or os.environ.get("DTM_DATABASE_URL")
    if not server:
        raise pytest.UsageError(
            "Set DTM_DATABASE_URL (or DTM_TEST_ADMIN_URL) to a PostgreSQL server; "
            f"the tests create their own {TEST_DATABASE} database on it."
        )
    # Set before any datatracemind import reads its settings. An empty value
    # still counts as set, so .env cannot bring back the demo's.
    os.environ["DTM_DATABASE_URL"] = make_conninfo(server, dbname=TEST_DATABASE)
    os.environ["DTM_READONLY_DATABASE_URL"] = ""
    os.environ["DTM_BACKGROUND_WORKERS"] = "0"
    os.environ["DTM_AUTO_INVESTIGATE"] = "0"
    os.environ.pop("DTM_DEMO_ACCESS_CODE", None)
    with psycopg.connect(server, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DATABASE}"')


@pytest.fixture
def baseline() -> dict:
    """A freshly reset demo environment. Returns the reset report."""
    from datatracemind import demo

    return demo.reset()
