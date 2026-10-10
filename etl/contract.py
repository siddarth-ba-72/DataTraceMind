"""Approved business contracts, loaded from `contracts/`.

Each contract is a Markdown file whose front matter is the machine-readable
form. Quality checks and source-derived metrics are computed from these
rules, never from the transformation they are checking.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from datatracemind.config import CONTRACTS_DIR

# Contract name -> file. Agents may retrieve only these.
METRIC_CONTRACTS = {"net_revenue": "revenue.md"}
DATA_CONTRACTS = {"orders": "orders.md", "oltp_schema": "schemas/oltp.yaml"}


class ContractNotFound(LookupError):
    """No approved contract exists under that name."""


@dataclass(frozen=True)
class RevenueContract:
    version: int
    payment_statuses: tuple[str, ...]
    refund_statuses: tuple[str, ...]
    refund_amount_basis: str
    timezone: str
    tolerance: Decimal


def _front_matter(path: Path) -> dict:
    text = path.read_text()
    if not text.startswith("---"):
        raise ValueError(f"{path.name} has no front matter")
    _, block, _ = text.split("---", 2)
    return yaml.safe_load(block)


def read_contract_text(name: str, *, kind: str) -> tuple[str, str]:
    """Return (relative path, text) of an approved contract."""
    registry = METRIC_CONTRACTS if kind == "metric" else DATA_CONTRACTS
    if name not in registry:
        raise ContractNotFound(
            f"no approved {kind} contract named {name!r}; available: {sorted(registry)}"
        )
    path = CONTRACTS_DIR / registry[name]
    return f"contracts/{registry[name]}", path.read_text()


def load_revenue_contract() -> RevenueContract:
    meta = _front_matter(CONTRACTS_DIR / METRIC_CONTRACTS["net_revenue"])
    rules = meta["rules"]
    return RevenueContract(
        version=int(meta["version"]),
        payment_statuses=tuple(rules["payment_statuses_included"]),
        refund_statuses=tuple(rules["refund_statuses_subtracted"]),
        refund_amount_basis=rules["refund_amount_basis"],
        timezone=rules["business_timezone"],
        tolerance=Decimal(str(rules["reconciliation_tolerance"])),
    )


def load_source_schema_contract() -> dict[str, dict[str, str]]:
    """Expected source columns: {table: {column: type prefix}}."""
    return yaml.safe_load((CONTRACTS_DIR / DATA_CONTRACTS["oltp_schema"]).read_text())["tables"]
