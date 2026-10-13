"""Runtime settings, read from the environment.

`.env` is loaded here so that every entry point (API, ETL, tool server,
scripts) sees the same values. The database is whatever PostgreSQL
DTM_DATABASE_URL names; there is no default.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"
TRANSFORMS_DIR = REPO_ROOT / "etl" / "transforms"
SQL_DIR = REPO_ROOT / "infra" / "postgres"

PIPELINE_NAME = "order_revenue"
# The only file a repair may target, relative to the repository root.
TRANSFORM_FILE = "etl/transforms/order_revenue.yaml"
READONLY_ROLE = "dtm_readonly"


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _readonly_url(database_url: str) -> str:
    """The same database, signed in as the read-only role.

    Its password is derived from the application role's, so one connection
    string configures both and the read-only one is never a known default.
    """
    password = conninfo_to_dict(database_url).get("password")
    if not password:
        raise RuntimeError(
            "DTM_DATABASE_URL has no password to derive the read-only role's from; "
            "set DTM_READONLY_DATABASE_URL as well"
        )
    derived = hmac.new(str(password).encode(), READONLY_ROLE.encode(), hashlib.sha256).hexdigest()
    return make_conninfo(database_url, user=READONLY_ROLE, password=derived)


@dataclass(frozen=True)
class Settings:
    # None when unset. The tool server is started with the read-only one only.
    database_url: str | None
    readonly_database_url: str | None
    demo_seed: int
    etl_interval_s: float
    traffic_min_interval_s: float
    traffic_max_interval_s: float
    freshness_objective_s: float
    run_background_workers: bool
    auto_investigate: bool
    demo_access_code: str | None
    investigations_per_hour: int
    investigations_per_day: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env")
    env = os.environ
    database_url = env.get("DTM_DATABASE_URL") or None
    readonly_database_url = env.get("DTM_READONLY_DATABASE_URL") or (
        _readonly_url(database_url) if database_url else None
    )
    return Settings(
        database_url=database_url,
        readonly_database_url=readonly_database_url,
        demo_seed=int(env.get("DTM_DEMO_SEED", "20261010")),
        etl_interval_s=float(env.get("DTM_ETL_INTERVAL_S", "6")),
        traffic_min_interval_s=float(env.get("DTM_TRAFFIC_MIN_INTERVAL_S", "0.5")),
        traffic_max_interval_s=float(env.get("DTM_TRAFFIC_MAX_INTERVAL_S", "2.0")),
        freshness_objective_s=float(env.get("DTM_FRESHNESS_OBJECTIVE_S", "60")),
        run_background_workers=_flag("DTM_BACKGROUND_WORKERS", True),
        auto_investigate=_flag("DTM_AUTO_INVESTIGATE", False),
        demo_access_code=env.get("DTM_DEMO_ACCESS_CODE") or None,
        investigations_per_hour=int(env.get("DTM_INVESTIGATIONS_PER_HOUR", "6")),
        investigations_per_day=int(env.get("DTM_INVESTIGATIONS_PER_DAY", "30")),
    )
