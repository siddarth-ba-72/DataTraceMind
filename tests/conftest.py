"""Test setup: run everything against a separate database.

Tests reset the demo environment freely, so they must never point at the
database a running demo is using. The same PostgreSQL server is reused with a
dedicated `datatracemind_test` database.
"""

from __future__ import annotations

import os

import psycopg
import pytest

TEST_DATABASE = "datatracemind_test"
_PORT = os.environ.get("DTM_POSTGRES_PORT", "5432")
_SERVER = f"127.0.0.1:{_PORT}"
_ADMIN = os.environ.get("DTM_TEST_ADMIN_URL", f"postgresql://dtm_admin:dtm_local_dev@{_SERVER}/datatracemind")


def pytest_configure(config: pytest.Config) -> None:
    # Set before any datatracemind import reads its settings.
    os.environ["DTM_DATABASE_URL"] = f"postgresql://dtm_admin:dtm_local_dev@{_SERVER}/{TEST_DATABASE}"
    os.environ["DTM_READONLY_DATABASE_URL"] = (
        f"postgresql://dtm_readonly:dtm_readonly_dev@{_SERVER}/{TEST_DATABASE}"
    )
    os.environ["DTM_BACKGROUND_WORKERS"] = "0"
    os.environ["DTM_AUTO_INVESTIGATE"] = "0"
    os.environ.pop("DTM_DEMO_ACCESS_CODE", None)
    # Tests must not send traces to the project's neatlogs workspace.
    os.environ["NEATLOGS_API_KEY"] = ""
    with psycopg.connect(_ADMIN, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DATABASE}"')


@pytest.fixture
def baseline() -> dict:
    """A freshly reset demo environment. Returns the reset report."""
    from datatracemind import demo

    return demo.reset()
