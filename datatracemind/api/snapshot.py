"""Shared, short-lived answers for the pages' snapshot request.

Every open page asks for the same read models every few seconds. Each one is
computed at most once per `ttl_s`, and a request that arrives while one is
being computed waits for that computation instead of starting its own. They
are computed on worker threads, so a snapshot takes as long as its slowest
part, not the sum of them.

A request that changes anything empties the cache, so the page that made the
change never reads an answer from before it. Changes made elsewhere (the
pipeline worker, a running investigation) show up once the answer expires.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Entry:
    future: Future = field(default_factory=Future)
    finished_at: float | None = None
    failed: bool = False


class SnapshotCache:
    def __init__(self, ttl_s: float, workers: int = 8) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snapshot")

    def get(self, key: str, compute: Callable[[], Any]) -> Future:
        """The answer for `key`: one being computed, a recent one, or a new computation."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or not self._usable(entry, now):
                # Rebuilt rather than added to, so keys nobody asks for any more do not pile up.
                self._entries = {k: e for k, e in self._entries.items() if self._usable(e, now)}
                entry = self._entries[key] = _Entry()
                entry.future = self._executor.submit(self._run, entry, compute)
            return entry.future

    def invalidate(self) -> None:
        """Forget every answer. One still being computed reaches only the requests already waiting for it."""
        with self._lock:
            self._entries = {}

    def _usable(self, entry: _Entry, now: float) -> bool:
        if entry.finished_at is None:
            return True
        return not entry.failed and now - entry.finished_at < self.ttl_s

    @staticmethod
    def _run(entry: _Entry, compute: Callable[[], Any]) -> Any:
        try:
            return compute()
        except BaseException:
            entry.failed = True
            raise
        finally:
            entry.finished_at = time.monotonic()
