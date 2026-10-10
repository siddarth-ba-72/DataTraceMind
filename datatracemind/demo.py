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
from etl.pipeline import RunResult, pipeline_lock
from simulator.generator import SEED_DAYS, SEED_ORDERS, TrafficGenerator, seed_baseline

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
    traffic.clear_counts()
    with pipeline_lock():
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
    with pipeline_lock():
        with db.connect() as conn:
            state = conn.execute("SELECT scenario_id FROM dtm.lab_state WHERE id = 1 FOR UPDATE").fetchone()
            if state["scenario_id"] is not None:
                raise LabError(f"{state['scenario_id']} is already injected; reset the demo first")
            baseline = metrics_summary()["reconciliation"]
            deployed_before = transform_store.get_active(conn).version
            scenario.inject(InjectionContext(conn=conn))
            deployed = transform_store.get_active(conn).version != deployed_before
            conn.execute(
                "UPDATE dtm.lab_state SET scenario_id = %s, injected_at = now(), baseline_metrics = %s, "
                "post_injection_metrics = NULL, injection_report = NULL WHERE id = 1",
                (scenario_id, Jsonb(baseline)),
            )
            store.audit(conn, actor="demo", action="scenario.inject", target=scenario_id, result="injected")
        # A fault in the source arrives with no deployment; the run that meets
        # it is an ordinary scheduled one.
        results = [pipeline.run_once(trigger="post_deploy" if deployed else "schedule")]
        incident_id = monitor.evaluate_run(results[0])
        if incident_id is None:
            with db.connect() as conn:
                again = scenario.after_first_run(InjectionContext(conn=conn))
            if again:
                results.append(pipeline.run_once(trigger=scenario.second_run_trigger))
                incident_id = monitor.evaluate_run(results[-1])
        after = metrics_summary()["reconciliation"]
        report = {"runs": [run_summary(r) for r in results], "incident_id": incident_id}
        db.execute(
            "UPDATE dtm.lab_state SET post_injection_metrics = %s, injection_report = %s WHERE id = 1",
            (Jsonb(after), Jsonb(report)),
        )
    result = results[-1]
    logger.info("injected %s: run %s %s, incident %s", scenario_id, result.run_id, result.status, incident_id)
    return {
        "scenario": scenario.public_view(),
        # The run the monitor reacted to; `runs` lists every run of the injection.
        "run": run_summary(result),
        "runs": report["runs"],
        "incident_id": incident_id,
        "baseline_metrics": baseline,
        "post_injection_metrics": after,
    }


def lab_status() -> dict:
    """Scenario catalogue, what is currently injected, and the environment it runs in."""
    state = db.fetch_one(
        "SELECT scenario_id, injected_at, baseline_metrics, post_injection_metrics, injection_report "
        "FROM dtm.lab_state WHERE id = 1"
    )
    settings = get_settings()
    return {
        "scenarios": [s.public_view() for s in SCENARIOS.values()],
        "active": state,
        "traffic_running": traffic.running,
        "environment": {
            "seed": settings.demo_seed,
            "seed_orders": SEED_ORDERS,
            "seed_days": SEED_DAYS,
            "etl_interval_seconds": settings.etl_interval_s,
            "freshness_objective_seconds": settings.freshness_objective_s,
        },
    }
