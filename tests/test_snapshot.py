"""The cache behind the pages' snapshot request."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from datatracemind.api.snapshot import SnapshotCache


class Counter:
    """A computation that counts its runs and can be held open."""

    def __init__(self, value: str = "answer") -> None:
        self.value = value
        self.runs = 0
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()

    def __call__(self) -> str:
        self.runs += 1
        run = self.runs
        self.started.set()
        assert self.release.wait(5)
        return f"{self.value} {run}"


def test_an_answer_is_computed_once_and_shared_until_it_expires():
    cache, compute = SnapshotCache(ttl_s=60), Counter()
    assert cache.get("metrics", compute).result() == "answer 1"
    assert cache.get("metrics", compute).result() == "answer 1"
    assert cache.get("runs", compute).result() == "answer 2"  # another key is another answer
    cache.ttl_s = 0
    assert cache.get("metrics", compute).result() == "answer 3"


def test_requests_that_arrive_during_a_computation_wait_for_it():
    cache, compute = SnapshotCache(ttl_s=0), Counter()
    compute.release.clear()
    with ThreadPoolExecutor(8) as callers:
        waiting = [callers.submit(lambda: cache.get("metrics", compute).result()) for _ in range(8)]
        assert compute.started.wait(5)
        time.sleep(0.05)  # let every caller reach the cache while the one computation is held open
        compute.release.set()
        assert {w.result() for w in waiting} == {"answer 1"}
    assert compute.runs == 1


def test_an_answer_computed_before_a_change_is_not_served_after_it():
    cache, compute = SnapshotCache(ttl_s=60), Counter()
    compute.release.clear()
    before = cache.get("metrics", compute)
    assert compute.started.wait(5)
    cache.invalidate()  # the change lands while the old answer is still being computed
    after = cache.get("metrics", compute)
    compute.release.set()
    assert before.result() == "answer 1"
    assert after.result() == "answer 2"
    assert cache.get("metrics", compute).result() == "answer 2"


def test_a_failure_is_not_kept():
    cache = SnapshotCache(ttl_s=60)

    def broken() -> None:
        raise RuntimeError("database unreachable")

    with pytest.raises(RuntimeError):
        cache.get("metrics", broken).result()
    assert cache.get("metrics", Counter()).result() == "answer 1"
