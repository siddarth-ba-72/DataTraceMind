"""E-commerce transaction simulator.

Writes customers, orders, payments, and refunds to the operational schema.
`seed_baseline` creates a deterministic history; `TrafficGenerator` keeps
adding transactions in the background. Both draw from one seeded generator,
so a reset followed by the same actions produces the same rows.

This stands in for the Spring Boot simulator named in the product brief. The
only interface between simulator and pipeline is the `oltp` schema, so a
different producer can replace this module without touching the ETL.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from datatracemind.config import get_settings
from datatracemind.db import connect

logger = logging.getLogger(__name__)

BUSINESS_TZ = ZoneInfo("Asia/Kolkata")
SEED_CUSTOMERS = 40
SEED_ORDERS = 320
SEED_DAYS = 14

# Share of orders by outcome. The remainder is paid in full.
P_UNPAID = 0.10
P_PAYMENT_FAILED = 0.08
P_CANCELLED = 0.03
# Share of paid orders by refund outcome. The remainder has no refund.
P_FULL_REFUND = 0.09
P_PARTIAL_REFUND = 0.08
P_REFUND_FAILED = 0.03
P_REFUND_PENDING = 0.03
P_RETRIED_PAYMENT = 0.06


def _money(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _order_amount_column(conn: psycopg.Connection) -> str:
    """Name of the order amount column as the source schema currently has it.

    The application that owns the source follows its own migrations, so the
    simulator keeps writing after the schema-drift scenario renames the column.
    """
    row = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'oltp' "
        "AND table_name = 'orders' AND column_name IN ('total_amount', 'order_total')"
    ).fetchone()
    return row["column_name"]


class Simulator:
    """Generates transactions from a seeded random stream."""

    def __init__(self, seed: int, *, history_end: datetime | None = None) -> None:
        self.rng = random.Random(seed)
        # When seeding, nothing may be dated at or after this instant.
        self.history_end = history_end

    def _amount(self) -> tuple[Decimal, str]:
        if self.rng.random() < 0.15:
            return _money(self.rng.uniform(5, 300)), "USD"
        return _money(self.rng.uniform(199, 14999)), "INR"

    def place_order(self, conn: psycopg.Connection, *, at: datetime | None = None, settle_refunds: bool = False) -> int:
        """Create an order and its payment outcome. Returns the order ID.

        `at` backdates the order (used for seeding); otherwise the database
        clock is used. With `settle_refunds`, some paid orders also get a
        refund dated up to a day after payment.
        """
        rng = self.rng
        customer_id = conn.execute(
            "SELECT customer_id FROM oltp.customers ORDER BY customer_id OFFSET %s LIMIT 1",
            (rng.randrange(SEED_CUSTOMERS),),
        ).fetchone()["customer_id"]
        amount, currency = self._amount()
        outcome = rng.random()
        created = at
        order_id = conn.execute(
            sql.SQL(
                """
                INSERT INTO oltp.orders (customer_id, order_status, {}, currency, created_at, updated_at)
                VALUES (%s, 'CREATED', %s, %s, coalesce(%s, now()), coalesce(%s, now()))
                RETURNING order_id
                """
            ).format(sql.Identifier(_order_amount_column(conn))),
            (customer_id, amount, currency, created, created),
        ).fetchone()["order_id"]

        if outcome < P_UNPAID:
            return order_id
        paid_at = created + timedelta(seconds=rng.randint(20, 600)) if created else None
        if outcome < P_UNPAID + P_PAYMENT_FAILED:
            self._add_payment(conn, order_id, "FAILED", amount, paid_at)
            self._set_order_status(conn, order_id, "CANCELLED", paid_at)
            return order_id
        if outcome < P_UNPAID + P_PAYMENT_FAILED + P_CANCELLED:
            self._set_order_status(conn, order_id, "CANCELLED", paid_at)
            return order_id

        if rng.random() < P_RETRIED_PAYMENT:
            self._add_payment(conn, order_id, "FAILED", amount, paid_at)
            paid_at = paid_at + timedelta(seconds=rng.randint(30, 300)) if paid_at else None
        payment_id = self._add_payment(conn, order_id, "CAPTURED", amount, paid_at)
        self._set_order_status(conn, order_id, "PAID", paid_at)
        if settle_refunds:
            self._maybe_refund(conn, order_id, payment_id, amount, paid_at)
        return order_id

    def _add_payment(
        self, conn: psycopg.Connection, order_id: int, status: str, amount: Decimal, at: datetime | None
    ) -> int:
        return conn.execute(
            """
            INSERT INTO oltp.payments (order_id, payment_status, amount, paid_at, updated_at)
            VALUES (%s, %s, %s, coalesce(%s, now()), coalesce(%s, now()))
            RETURNING payment_id
            """,
            (order_id, status, amount, at, at),
        ).fetchone()["payment_id"]

    def _set_order_status(self, conn: psycopg.Connection, order_id: int, status: str, at: datetime | None) -> None:
        conn.execute(
            "UPDATE oltp.orders SET order_status = %s, updated_at = coalesce(%s, now()) WHERE order_id = %s",
            (status, at, order_id),
        )

    def _maybe_refund(
        self, conn: psycopg.Connection, order_id: int, payment_id: int, amount: Decimal, paid_at: datetime | None
    ) -> None:
        rng = self.rng
        roll = rng.random()
        at = paid_at + timedelta(minutes=rng.randint(60, 1380)) if paid_at else None
        if at and self.history_end and at >= self.history_end:
            return  # the refund would fall after the seeded history; leave the order paid
        if roll < P_FULL_REFUND:
            self._add_refund(conn, order_id, payment_id, "SUCCEEDED", amount, at)
            self._set_order_status(conn, order_id, "REFUNDED", at)
        elif roll < P_FULL_REFUND + P_PARTIAL_REFUND:
            partial = _money(float(amount) * rng.uniform(0.2, 0.7))
            self._add_refund(conn, order_id, payment_id, "SUCCEEDED", partial, at)
            self._set_order_status(conn, order_id, "PARTIALLY_REFUNDED", at)
        elif roll < P_FULL_REFUND + P_PARTIAL_REFUND + P_REFUND_FAILED:
            self._add_refund(conn, order_id, payment_id, "FAILED", amount, at)
        elif roll < P_FULL_REFUND + P_PARTIAL_REFUND + P_REFUND_FAILED + P_REFUND_PENDING:
            self._add_refund(conn, order_id, payment_id, "PENDING", amount, at)

    def _add_refund(
        self,
        conn: psycopg.Connection,
        order_id: int,
        payment_id: int,
        status: str,
        amount: Decimal,
        at: datetime | None,
    ) -> int:
        return conn.execute(
            """
            INSERT INTO oltp.refunds (payment_id, order_id, refund_status, refund_amount, created_at, updated_at)
            VALUES (%s, %s, %s, %s, coalesce(%s, now()), coalesce(%s, now()))
            RETURNING refund_id
            """,
            (payment_id, order_id, status, amount, at, at),
        ).fetchone()["refund_id"]

    def request_refund(self, conn: psycopg.Connection) -> bool:
        """Open a refund on a recent paid order that has none. False if none qualifies."""
        candidates = conn.execute(
            """
            SELECT o.order_id, p.payment_id, p.amount
            FROM oltp.orders o JOIN oltp.payments p ON p.order_id = o.order_id AND p.payment_status = 'CAPTURED'
            WHERE o.order_status = 'PAID'
              AND NOT EXISTS (SELECT 1 FROM oltp.refunds r WHERE r.order_id = o.order_id)
            ORDER BY o.order_id DESC LIMIT 50
            """
        ).fetchall()
        if not candidates:
            return False
        chosen = self.rng.choice(candidates)
        full = self.rng.random() < 0.5
        amount = chosen["amount"] if full else _money(float(chosen["amount"]) * self.rng.uniform(0.2, 0.7))
        if self.rng.random() < 0.5:
            self._add_refund(conn, chosen["order_id"], chosen["payment_id"], "PENDING", amount, None)
        else:
            self._add_refund(conn, chosen["order_id"], chosen["payment_id"], "SUCCEEDED", amount, None)
            status = "REFUNDED" if full else "PARTIALLY_REFUNDED"
            self._set_order_status(conn, chosen["order_id"], status, None)
        return True

    def settle_pending_refund(self, conn: psycopg.Connection) -> bool:
        """Resolve one pending refund. False if there is none."""
        pending = conn.execute(
            """
            SELECT r.refund_id, r.order_id, r.refund_amount, p.amount AS paid
            FROM oltp.refunds r JOIN oltp.payments p ON p.payment_id = r.payment_id
            WHERE r.refund_status = 'PENDING' ORDER BY r.refund_id LIMIT 50
            """
        ).fetchall()
        if not pending:
            return False
        chosen = self.rng.choice(pending)
        status = "SUCCEEDED" if self.rng.random() < 0.8 else "FAILED"
        conn.execute(
            "UPDATE oltp.refunds SET refund_status = %s, updated_at = now() WHERE refund_id = %s",
            (status, chosen["refund_id"]),
        )
        if status == "SUCCEEDED":
            order_status = "REFUNDED" if chosen["refund_amount"] >= chosen["paid"] else "PARTIALLY_REFUNDED"
            self._set_order_status(conn, chosen["order_id"], order_status, None)
        return True

    def tick(self, conn: psycopg.Connection) -> str:
        """Perform one random live action and return its name."""
        roll = self.rng.random()
        if roll < 0.15 and self.request_refund(conn):
            return "refund_requested"
        if roll < 0.30 and self.settle_pending_refund(conn):
            return "refund_settled"
        self.place_order(conn)
        return "order_placed"


def seed_baseline(conn: psycopg.Connection, seed: int) -> None:
    """Insert the deterministic history: customers and two weeks of orders.

    History ends at the start of the current business day, so totals for a
    given seed do not depend on the time of day the reset runs.
    """
    day_start = datetime.now(BUSINESS_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    simulator = Simulator(seed, history_end=day_start)
    history_start = day_start - timedelta(days=SEED_DAYS)
    conn.cursor().executemany(
        "INSERT INTO oltp.customers (created_at) VALUES (%s)",
        [(history_start - timedelta(days=30),)] * SEED_CUSTOMERS,
    )
    # Leave room after the last order for its payment attempts to land before
    # the history ends.
    span_seconds = int((day_start - history_start).total_seconds()) - 1200
    offsets = sorted(simulator.rng.randrange(span_seconds) for _ in range(SEED_ORDERS))
    for offset in offsets:
        simulator.place_order(conn, at=history_start + timedelta(seconds=offset), settle_refunds=True)


# Live traffic rates: seconds between transactions, as (shortest, longest).
# "steady" uses the configured interval; the others are fixed.
TRAFFIC_PROFILES = ("light", "steady", "busy")
DEFAULT_PROFILE = "steady"
MAX_BATCH = 200


def traffic_interval(profile: str) -> tuple[float, float]:
    if profile == "light":
        return 2.0, 5.0
    if profile == "busy":
        return 0.1, 0.4
    settings = get_settings()
    return settings.traffic_min_interval_s, settings.traffic_max_interval_s


class TrafficGenerator:
    """Background thread that keeps adding transactions to the source.

    One transaction at a time, at the pace of the selected profile. It also
    counts what it wrote, by action, so the lab can show what the stream did.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.profile = DEFAULT_PROFILE
        self.started_at: datetime | None = None
        self.last_action_at: datetime | None = None
        self.counts: dict[str, int] = {}
        self.failed_ticks = 0
        self._batches = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def actions(self) -> int:
        return sum(self.counts.values())

    def set_profile(self, profile: str) -> None:
        """Choose the rate. A running stream adopts it on its next transaction."""
        if profile not in TRAFFIC_PROFILES:
            raise ValueError(f"unknown traffic profile {profile!r}; choose one of {', '.join(TRAFFIC_PROFILES)}")
        self.profile = profile

    def start(self, profile: str | None = None) -> bool:
        """Start the stream, optionally at a given rate. False if it was already running."""
        if profile is not None:
            self.set_profile(profile)
        with self._lock:
            if self.running:
                return False
            self._stop.clear()
            self.started_at = datetime.now(BUSINESS_TZ)
            self._thread = threading.Thread(target=self._run, name="traffic", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self.running:
                return False
            self._stop.set()
            self._thread.join(timeout=10)
            self._thread = None
            self.started_at = None
            return True

    def clear_counts(self) -> None:
        self.counts = {}
        self.failed_ticks = 0
        self.last_action_at = None
        self._batches = 0

    def _record(self, action: str) -> None:
        self.counts[action] = self.counts.get(action, 0) + 1
        self.last_action_at = datetime.now(BUSINESS_TZ)

    def send_batch(self, size: int) -> dict[str, int]:
        """Write `size` transactions now, in one go. Returns how many of each action."""
        if not 1 <= size <= MAX_BATCH:
            raise ValueError(f"a batch is between 1 and {MAX_BATCH} transactions")
        with self._lock:
            self._batches += 1
            # Its own seeded stream per batch, apart from the baseline and the live stream.
            simulator = Simulator(get_settings().demo_seed + 1000 + self._batches)
        written: dict[str, int] = {}
        with connect() as conn:
            for _ in range(size):
                action = simulator.tick(conn)
                written[action] = written.get(action, 0) + 1
        for action, count in written.items():
            self.counts[action] = self.counts.get(action, 0) + count
        self.last_action_at = datetime.now(BUSINESS_TZ)
        return written

    def status(self) -> dict:
        shortest, longest = traffic_interval(self.profile)
        return {
            "running": self.running,
            "profile": self.profile,
            "profiles": [
                {
                    "name": name,
                    "interval_seconds": list(traffic_interval(name)),
                    "per_minute": round(120 / sum(traffic_interval(name))),
                }
                for name in TRAFFIC_PROFILES
            ],
            "per_minute": round(120 / (shortest + longest)),
            "started_at": self.started_at,
            "last_action_at": self.last_action_at,
            "counts": {
                "orders_placed": self.counts.get("order_placed", 0),
                "refunds_requested": self.counts.get("refund_requested", 0),
                "refunds_settled": self.counts.get("refund_settled", 0),
            },
            "total": self.actions,
            "failed_ticks": self.failed_ticks,
            "max_batch": MAX_BATCH,
        }

    def _run(self) -> None:
        # Offset from the baseline seed so live traffic does not replay it.
        simulator = Simulator(get_settings().demo_seed + 1)
        while not self._stop.is_set():
            try:
                with connect() as conn:
                    action = simulator.tick(conn)
                self._record(action)
            except psycopg.Error:
                # The schema can be mid-reset or deliberately drifted; keep trying.
                self.failed_ticks += 1
                logger.warning("traffic tick failed", exc_info=True)
            # Read each time, so a rate change takes effect on the next transaction.
            self._stop.wait(simulator.rng.uniform(*traffic_interval(self.profile)))
