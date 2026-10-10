"""Prompt templates for the model-backed roles.

Every prompt is a fixed template filled from typed incident state, tool
results, and other roles' validated outputs. No text typed by a user of the
web application is ever interpolated here; that is what makes it acceptable
to expose "investigate" as a button on a public URL.
"""

from __future__ import annotations

import json
from typing import Any

from datatracemind.config import TRANSFORM_FILE
from datatracemind.db import to_jsonable

# Evidence results are shown to the analyst in full up to this size.
MAX_RESULT_CHARS = 2600
# Fields that hold file text; never cut, the analyst must be able to quote them.
VERBATIM_FIELDS = ("content", "text")

_CONTEXT = """\
You are the {title} of DataTraceMind, a data-reliability system. An automated \
monitor opened the incident below.

SYSTEM
- Source of truth: PostgreSQL schema `oltp` (orders, payments, refunds), written by an e-commerce application.
- A Python ETL named `order_revenue` loads the analytics schema `analytics` \
(fact_orders, fact_payments, fact_refunds, daily_revenue).
- How source rows become fact rows is decided by a versioned transformation file, {transform_file}.
- Approved business definitions are stored as contracts.

RULES
- Work only from tool results and the material in this prompt. Quote figures exactly as given.
- If a tool call fails, read the error and correct the call if you can. Otherwise report the gap. \
Never replace a missing result with a guess.
- State what the evidence shows. Do not present a possibility as a fact.

INCIDENT
{incident}
"""

_SOURCE_TASK = """
YOUR TASK: source-data investigation
Establish from the data what is wrong and how large it is.
1. Compare the source with analytics for the affected metric or table.
2. Find which rows account for the difference: profile the relevant statuses, counts, and amounts on both sides.
3. Collect a small sample of affected source IDs.
Do not read pipeline code; another investigator is doing that in parallel.
You must call these tools at least once: {required}. Use at most {max_calls} tool calls in total.

Return `observations` (each one fact with its figures, and the tool that showed it) and `open_questions`.
"""

_PIPELINE_TASK = """
YOUR TASK: pipeline investigation
Establish what the pipeline is doing with the rows involved in the failed checks.
1. Read the transformation file as deployed, and its deployment history: what changed, when, and by whom.
2. Read the recent run logs: status, row counts, errors, the transformation version of each run, failed checks.
3. Identify the exact line or lines of the transformation file, or the pipeline step, that govern those rows. \
Quote them verbatim.
4. The transformation file and its history come first. Read pipeline source code (a path ending in .py) only if \
they do not explain the failed checks.
Do not profile source data; another investigator is doing that in parallel.
You must call these tools at least once: {required}. Use at most {max_calls} tool calls in total.

Return `observations` (each one fact, and the tool that showed it) and `open_questions`.
"""

_TARGETED_TASK = """
YOUR TASK: targeted follow-up investigation
The root-cause analyst could not confirm a cause and asked for these checks:
{requests}

Run the tool calls that answer them. Use at most {max_calls} tool calls.
Return `observations` (each one fact with its figures, and the tool that showed it) and `open_questions`.
"""

_ANALYST_TASK = """
YOUR TASK: root-cause analysis
All evidence collected so far is listed under EVIDENCE; each item has an ID. You have no tools in this turn.
1. Give at least two competing hypotheses, most likely first, as H1, H2, ... For each: a category, a statement, \
a confidence from 0 to 1, the evidence IDs that support it, the evidence IDs that contradict it, and your reasoning.
2. Set `selected_hypothesis_id` and `confirmed` = true only if the evidence shows the mechanism directly \
(for example the governing configuration line and the affected rows), the mechanism accounts for the size and \
direction of the discrepancy, and no evidence contradicts it. Otherwise set `confirmed` = false.
3. `implicated_component`: the precise file and field, table and column, or pipeline step.
4. If the evidence is insufficient, set `needs_more_evidence` = true and list in `requested_diagnostics` the \
specific checks that would settle it.
5. If deciding requires a business definition that the contracts in the evidence do not give, or the contracts \
conflict, say so in `business_rule_ambiguity`. Do not invent a rule. Otherwise leave it empty.
Cite only evidence IDs that appear under EVIDENCE.

INVESTIGATOR NOTES
{notes}

EVIDENCE
{evidence}
"""

_REPAIR_TASK = """
YOUR TASK: propose a repair
The root cause below was confirmed from evidence. Propose the smallest change that removes it and brings the \
pipeline back in line with the approved contract.

CONSTRAINTS
- You may change exactly one file: {transform_file}. Call get_etl_code with "active" and start from that exact text.
- Return the complete new file in `new_content`. Change only the lines that must change; keep every other line \
and every comment exactly as it is.
- The file is validated against a fixed schema; any other key or value is rejected. The fields are:
    pipeline: order_revenue
    source.orders.amount_column: name of the source column holding the order amount
    fact_orders.payment_join: left | inner
    revenue.timezone: an IANA timezone name
    revenue.payment_statuses: list of status names
    revenue.refund_statuses: list of status names
    revenue.refund_amount_basis: refund_amount | order_total
    load.mode: replace_by_key | append
- You may not change a contract, the source schema, or any data. A repair may bring the transformation back in \
line with a contract; it may never redefine a business rule.
- Your change will be run in a staging copy and checked by deterministic tests, then applied only if a human \
approves that exact change.
- If no change to this file is the right fix (for example the source stopped producing data), or the contracts do \
not define the behaviour needed, set `can_repair` = false, explain in `escalation_reason`, and return the \
unchanged file in `new_content`.

Also return: `rationale` (why this change removes the root cause), `risk`, `rollback_plan`, `affected_metrics`, \
and `regression_cases` (the cases this fix must be tested against).

CONFIRMED ROOT CAUSE
{root_cause}

SUPPORTING EVIDENCE
{evidence}
{revision}"""

_REVISION = """
YOUR PREVIOUS PROPOSAL WAS NOT ACCEPTED
{diff}
Reasons:
{failures}
Propose a corrected change.
"""


def _json(value: Any) -> str:
    return json.dumps(to_jsonable(value), indent=1, default=str)


def _incident_block(incident: dict) -> str:
    """The trigger as the monitor recorded it. Never includes the lab scenario."""
    trigger = incident["trigger"]
    return _json(
        {
            "incident_id": incident["incident_id"],
            "incident_type": incident["incident_type"],
            "severity": incident["severity"],
            "summary": incident["summary"],
            "detected_at": incident["detected_at"],
            "etl_run": {
                "run_id": trigger.get("etl_run_id"),
                "status": trigger.get("etl_status"),
                "error": trigger.get("etl_error"),
                "transform_version": trigger.get("transform_version"),
            },
            "failed_checks": [
                {
                    "check": check["check_name"],
                    "expected": check["expected_condition"],
                    "observed": check["observed_value"],
                }
                for check in trigger.get("failed_checks", [])
            ],
        }
    )


def _context(title: str, incident: dict) -> str:
    return _CONTEXT.format(title=title, transform_file=TRANSFORM_FILE, incident=_incident_block(incident))


def _compact(result: Any) -> str:
    """One evidence result as text: file contents whole, everything else capped."""
    result = to_jsonable(result)
    verbatim = ""
    if isinstance(result, dict):
        for name in VERBATIM_FIELDS:
            if isinstance(result.get(name), str):
                verbatim += f"\n--- {name} ---\n{result[name]}\n--- end {name} ---"
                result = {key: value for key, value in result.items() if key != name}
    text = json.dumps(result, default=str)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f" ... [{len(text) - MAX_RESULT_CHARS} more characters not shown]"
    return text + verbatim


def render_evidence(evidence: list[dict]) -> str:
    if not evidence:
        return "(none)"
    blocks = []
    for item in evidence:
        blocks.append(
            f"[{item['evidence_id']}] {item['description']} "
            f"(tool {item['query_or_tool']}, arguments {json.dumps(item['arguments'], default=str)}, "
            f"collected by {item['collected_by']})\n{_compact(item['result_summary'])}"
        )
    return "\n\n".join(blocks)


def source_investigator(incident: dict, *, required: tuple[str, ...], max_calls: int) -> str:
    return _context("source-data investigator", incident) + _SOURCE_TASK.format(
        required=", ".join(required), max_calls=max_calls
    )


def pipeline_investigator(incident: dict, *, required: tuple[str, ...], max_calls: int) -> str:
    return _context("pipeline investigator", incident) + _PIPELINE_TASK.format(
        required=", ".join(required), max_calls=max_calls
    )


def targeted_investigator(incident: dict, *, requests: list[str], max_calls: int) -> str:
    return _context("follow-up investigator", incident) + _TARGETED_TASK.format(
        requests="\n".join(f"- {request}" for request in requests), max_calls=max_calls
    )


def root_cause_analyst(incident: dict, *, evidence: list[dict], notes: dict[str, Any]) -> str:
    return _context("root-cause analyst", incident) + _ANALYST_TASK.format(
        notes=_json(notes) if notes else "(none)", evidence=render_evidence(evidence)
    )


def repair_agent(incident: dict, *, root_cause: dict, evidence: list[dict], previous: dict | None) -> str:
    revision = ""
    if previous:
        revision = _REVISION.format(
            diff=previous.get("diff") or "(the proposal could not be turned into a patch)",
            failures="\n".join(f"- {failure}" for failure in previous["failures"]),
        )
    return _context("repair agent", incident) + _REPAIR_TASK.format(
        transform_file=TRANSFORM_FILE,
        root_cause=_json(root_cause),
        evidence=render_evidence(evidence),
        revision=revision,
    )
