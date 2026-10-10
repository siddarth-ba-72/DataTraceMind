"""Runtime settings, read from the environment.

`.env` is loaded here so that every entry point (API, ETL, tool server,
scripts) sees the same values. Database defaults match docker-compose.yml and
are meant for the disposable local stack only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"
TRANSFORMS_DIR = REPO_ROOT / "etl" / "transforms"
SQL_DIR = REPO_ROOT / "infra" / "postgres"

PIPELINE_NAME = "order_revenue"
# The only file a repair may target, relative to the repository root.
TRANSFORM_FILE = "etl/transforms/order_revenue.yaml"


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    readonly_database_url: str
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
    port = env.get("DTM_POSTGRES_PORT", "5432")
    return Settings(
        database_url=env.get(
            "DTM_DATABASE_URL",
            f"postgresql://dtm_admin:dtm_local_dev@127.0.0.1:{port}/datatracemind",
        ),
        readonly_database_url=env.get(
            "DTM_READONLY_DATABASE_URL",
            f"postgresql://dtm_readonly:dtm_readonly_dev@127.0.0.1:{port}/datatracemind",
        ),
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
