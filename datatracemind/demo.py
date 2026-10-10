"""Demo harness: reset to the baseline and inject scenario faults.

This module is the only caller of the scenario answer keys' owners. It is
used by the API's demo routes and by tests; the agent workflow never imports
it.
"""

from __future__ import annotations

import logging

from psycopg.types.json import Jsonb

from datatracemind import db, store
from datatracemind.config import get_settings
from datatracemind.monitoring import monitor
from datatracemind.monitoring.summary import metrics_summary
from datatracemind.scenarios import SCENARIOS, InjectionContext
from etl import pipeline, transform_store
from etl.pipeline import PIPELINE_LOCK, RunResult
from simulator.generator import TrafficGenerator, seed_baseline

logger = logging.getLogger(__name__)

traffic = TrafficGenerator()


class LabError(RuntimeError):
    """The lab cannot do what was asked in its current state."""


def run_summary(result: RunResult) -> dict:
    return {
        "run_id": result.run_id,
        "status": result.status,
        "error_summary": result.error_summary,
        "transform_version": result.transform_version,
        "full_refresh": result.full_refresh,
        "extracted": result.extracted,
        "loaded": result.loaded,
        "rejected": result.rejected,
        "failed_checks": [c.name for c in result.failed_checks],
    }


def reset() -> dict:
    """Restore the clean baseline: data, transformation, and incident state.

    Traffic is left stopped so that the baseline is the same on every reset.
    """
    traffic.stop()
    with PIPELINE_LOCK:
        db.drop_all()
        db.ensure_schema()
        with db.connect() as conn:
            seed_baseline(conn, get_settings().demo_seed)
            transform_store.register_version(
                conn,
                transform_store.baseline_content(),
                author="baseline",
                message="Baseline transformation (repository version)",
            )
        result = pipeline.run_once(trigger="reset")
        with db.connect() as conn:
            store.audit(conn, actor="demo", action="demo.reset", target="environment", result=result.status)
    logger.info("demo reset: baseline run %s %s", result.run_id, result.status)
    return {"run": run_summary(result), "metrics": metrics_summary()["reconciliation"]}


def inject(scenario_id: str) -> dict:
    """Inject one scenario's fault, run the pipeline, and let the monitor react."""
    scenario = SCENARIOS[scenario_id]
    with PIPELINE_LOCK:
        with db.connect() as conn:
            state = conn.execute("SELECT scenario_id FROM dtm.lab_state WHERE id = 1 FOR UPDATE").fetchone()
            if state["scenario_id"] is not None:
                raise LabError(f"{state['scenario_id']} is already injected; reset the demo first")
            baseline = metrics_summary()["reconciliation"]
            scenario.inject(InjectionContext(conn=conn))
            conn.execute(
                "UPDATE dtm.lab_state SET scenario_id = %s, injected_at = now(), baseline_metrics = %s, "
                "post_injection_metrics = NULL WHERE id = 1",
                (scenario_id, Jsonb(baseline)),
            )
            store.audit(conn, actor="demo", action="scenario.inject", target=scenario_id, result="injected")
        result = pipeline.run_once(trigger="post_deploy")
        incident_id = monitor.evaluate_run(result)
        after = metrics_summary()["reconciliation"]
        db.execute("UPDATE dtm.lab_state SET post_injection_metrics = %s WHERE id = 1", (Jsonb(after),))
    logger.info("injected %s: run %s %s, incident %s", scenario_id, result.run_id, result.status, incident_id)
    return {
        "scenario": scenario.public_view(),
        "run": run_summary(result),
        "incident_id": incident_id,
        "baseline_metrics": baseline,
        "post_injection_metrics": after,
    }


def lab_status() -> dict:
    """Scenario list and what is currently injected."""
    state = db.fetch_one(
        "SELECT scenario_id, injected_at, baseline_metrics, post_injection_metrics FROM dtm.lab_state WHERE id = 1"
    )
    return {
        "scenarios": [s.public_view() for s in SCENARIOS.values()],
        "active": state,
        "traffic_running": traffic.running,
    }
