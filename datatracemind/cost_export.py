"""Export measured cost per investigation as CSV, for the cfo.ai business plan.

    python -m datatracemind.cost_export > business-plan/investigation_costs.csv

One row per evaluated run, read from evaluation/results/. Token counts are
measured. `est_cost_usd` is an estimate at API list prices: the model runs on
a flat-rate plan, so nothing was charged per run. Only summaries are written;
no source rows, prompts, or secrets.
"""

from __future__ import annotations

import csv
import json
import sys

from datatracemind.evaluation import RESULTS_DIR

COLUMNS = (
    "run_date", "incident_id", "scenario_id", "workflow_version", "model", "outcome", "role_turns",
    "tool_calls", "input_tokens", "cached_input_tokens", "output_tokens", "est_cost_usd", "duration_s",
)  # fmt: skip


def rows() -> list[dict]:
    exported = []
    for path in sorted(RESULTS_DIR.glob("*.json")):
        result = json.loads(path.read_text())
        run = result["run"]
        exported.append(
            {
                "run_date": result["evaluated_at"][:10],
                "incident_id": result["incident_id"],
                "scenario_id": result["scenario_id"],
                "workflow_version": result["workflow_version"],
                "model": result["model"],
                "outcome": result["outcome"],
                **{name: run[name] for name in COLUMNS if name in run and name not in ("incident_id",)},
            }
        )
    return exported


def main() -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows())


if __name__ == "__main__":
    main()
