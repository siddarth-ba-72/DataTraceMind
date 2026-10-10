"""The ETL worker: runs the pipeline on a schedule and feeds the monitor."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from datatracemind.config import get_settings
from datatracemind.monitoring import monitor
from etl import pipeline

logger = logging.getLogger(__name__)


class EtlWorker:
    """Background thread: one ETL run every few seconds, then detection."""

    def __init__(self, on_incident: Callable[[str], None] | None = None) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_incident = on_incident

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="etl-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        self._thread.join(timeout=30)
        self._thread = None

    def _run(self) -> None:
        interval = get_settings().etl_interval_s
        while not self._stop.wait(interval):
            try:
                result = pipeline.run_once(trigger="schedule")
                incident_id = monitor.evaluate_run(result) or monitor.evaluate_freshness()
                if incident_id and self._on_incident:
                    self._on_incident(incident_id)
            except Exception:
                # run_once records pipeline failures itself; anything that
                # reaches here is infrastructure (for example a reset in
                # progress). Log it and try again on the next tick.
                logger.exception("ETL worker tick failed")
