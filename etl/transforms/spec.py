"""Schema of the transformation file (`order_revenue.yaml`).

The file is data, not code: the ETL interprets these fields and nothing else.
That is what makes a repair safe to run. A proposed patch is accepted only if
the patched file parses against this schema, so a patch can change which
rows qualify and how they are loaded, but cannot introduce SQL, code, or a
field the pipeline does not already understand.
"""

from __future__ import annotations

from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Identifier = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")]
StatusName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,39}$")]
StatusList = Annotated[list[StatusName], Field(max_length=10)]

MAX_SPEC_BYTES = 8_000


class SpecError(ValueError):
    """The transformation file is not valid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceOrders(_Strict):
    amount_column: Identifier


class SourceSpec(_Strict):
    orders: SourceOrders


class FactOrdersSpec(_Strict):
    payment_join: Literal["left", "inner"]


class RevenueSpec(_Strict):
    timezone: str
    payment_statuses: StatusList
    refund_statuses: StatusList
    refund_amount_basis: Literal["refund_amount", "order_total"]

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value


class LoadSpec(_Strict):
    mode: Literal["replace_by_key", "append"]


class TransformSpec(_Strict):
    pipeline: Literal["order_revenue"]
    source: SourceSpec
    fact_orders: FactOrdersSpec
    revenue: RevenueSpec
    load: LoadSpec


def parse_spec(text: str) -> TransformSpec:
    """Parse and validate a transformation file."""
    if len(text.encode()) > MAX_SPEC_BYTES:
        raise SpecError(f"transformation file exceeds {MAX_SPEC_BYTES} bytes")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"transformation file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("transformation file must be a YAML mapping")
    try:
        return TransformSpec.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise SpecError(f"transformation file is invalid: {problems}") from exc
