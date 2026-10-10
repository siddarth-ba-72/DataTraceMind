# DataTraceMind

An agentic data-reliability engineer. It detects when a data pipeline is
producing wrong numbers, investigates the cause with evidence from the source
database, the pipeline, the analytics target, and the approved business
rules, proposes a minimal repair, proves that repair in staging, and applies
it only after a human approves that exact change.

The failure it is built around is the silent one: the ETL job reports
`SUCCESS` while the revenue it loaded is wrong.

Built for neatHack (10–12 October 2026). The build log and submission
evidence are in [hackathon.md](hackathon.md).

## What happens in one incident

1. A simulator writes orders, payments, and refunds to PostgreSQL (`oltp`).
2. A Python ETL loads the analytics schema, driven by a versioned
   transformation file.
3. The scenario lab injects one known fault.
4. Deterministic quality checks fail and the monitor opens an incident. No
   model decides that something is wrong.
5. A supervisor runs the agent roles: a source-data investigator and a
   pipeline investigator in parallel, a contract lookup, a root-cause analyst,
   and a repair agent. Each model-backed role is one Codex turn with its own
   tool allow-list and output schema.
6. The repair is run in a staging schema against regression fixtures and every
   quality check.
7. A person reviews the evidence, diff, and staging result, then approves or
   rejects. Approval is bound to the patch hash.
8. On approval the patch is applied, the pipeline reruns, and the incident
   resolves only if every check passes. Otherwise it rolls back.

## Run it

Requirements: Docker, Python 3.13, Node.js 22 or newer, and the
[Codex CLI](https://developers.openai.com/codex/cli) signed in (`codex login`)
or `CODEX_API_KEY` set. Without model credentials everything except
"Investigate" still works; that button is refused with a message saying why.

```sh
cp .env.example .env                  # add NEATLOGS_API_KEY to trace agent runs
docker compose up -d postgres
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
(cd frontend && npm install && npm run build)
.venv/bin/uvicorn datatracemind.api.app:app --port 8000
```

Open <http://127.0.0.1:8000>. The first start seeds a deterministic baseline.

Demo path: **Scenario lab → Inject fault (DS-01) → Investigate → review the
repair → Approve and apply**. **Reset demo** returns to the clean baseline.
An investigation takes about a minute and four Codex turns.

For frontend development, `npm run dev` in `frontend/` serves the dashboard on
port 5173 and proxies `/api` to port 8000.

## Scenarios

| ID | Fault | What the checks see |
|---|---|---|
| DS-01 | A transformation change stops subtracting completed refunds | ETL green; net revenue overstated by exactly the succeeded refunds |
| DS-02 | A source migration renames `orders.total_amount` | ETL fails; analytics goes stale |
| DS-03 | The order fact is built with an inner join to payments | ETL green; orders with no payment are missing |

Each scenario carries an answer key (`ground_truth`) that only the evaluator
reads. Nothing an agent can call, and no prompt, contains it.

## Safety model

- **Agents only read.** The investigation tools connect with a PostgreSQL role
  that can `SELECT` from the source, the analytics target, and the
  transformation history, and nothing else. No tool accepts SQL; queries are
  templates with typed parameters.
- **Codex has no shell.** Each turn runs with the shell and web search
  disabled and a read-only sandbox, so the MCP tools are its only reach.
- **Evidence comes from tool results.** The adapter reads the tool calls from
  Codex's event stream and fails a turn unless the role's required calls
  succeeded. The model's description of a result is never stored as evidence.
- **A repair is data, not code.** It is a new version of one allowlisted file,
  `etl/transforms/order_revenue.yaml`, and is accepted only if it parses
  against a strict schema and changes a handful of lines.
- **Deterministic checks decide.** Staging validation, approval checks, and
  post-fix verification are code. A model's statement that something passed
  proves nothing.
- **Approval is bound to the patch.** The executor re-reads the proposal, the
  approval, and the validation result and refuses unless all three name the
  same hash and the patch still applies to the deployed version.
- **Prompts are fixed templates.** No text typed into the web application
  reaches a prompt. Mutating routes can require a demo access code, and
  investigations are capped per hour and per day.
- **Unclear business rules go to a human.** A missing contract, or a rule the
  analyst cannot find, escalates the incident instead of being assumed.

## Tests and evaluation

```sh
.venv/bin/python -m pytest                           # 99 tests, about a minute
.venv/bin/python -m datatracemind.evaluation DS-01   # one real run, scored
```

The tests use a separate `datatracemind_test` database and spend no model
usage: the workflow is driven by a scripted stand-in provider whose tool calls
are real. They cover the blocked paths as well as the happy one: applying
without approval, a changed patch after approval, a patch that failed
validation, rejection, and a failed post-fix verification.

The evaluator resets the demo, injects a scenario, runs a real investigation,
and scores it against the answer key. Results are written to
`evaluation/results/`.

## Layout

```
contracts/        approved business definitions (revenue, orders, source schema)
etl/              pipeline, transformation, quality checks, run log
simulator/        seeded transaction generator
infra/postgres/   schema for source, analytics target, and application state
datatracemind/
  api/            FastAPI routes
  monitoring/     detection and dashboard read models
  scenarios/      fault scenarios and their answer keys
  tools/          read-only investigation tools and the MCP server
  llm/            provider contract and the Codex adapter
  agents/         role prompts and the role runner
  orchestration/  supervisor state machine and routing
  repair/         proposals, staging validation, approval, executor
  observability/  neatlogs tracing
  evaluation.py   scenario evaluator
frontend/         React dashboard
tests/
```

## Hackathon tools

- **neatlogs** traces every investigation: one trace per run, a span per role,
  per Codex turn (with token usage and estimated cost), and per tool call.
  Approval, apply, and verification are traced as guardrail spans in the same
  session.
- **Entire** captures the coding-agent sessions behind each commit.
- **cfo.ai** holds the business plan; the measured cost per investigation
  feeds it.

## Deviations from the plan

- The simulator is Python, not Spring Boot. The `oltp` schema is its only
  interface to the pipeline, so another producer can replace it.
- The analytics target is a PostgreSQL schema, the plan's documented fallback
  to ClickHouse.
- The ETL worker runs as a thread inside the API process rather than as its
  own service.
- Contract lookup runs before the two investigators rather than beside them,
  so a missing definition escalates before any model usage is spent.
- Two database credentials exist (application and read-only), not a third
  repair-only one. The executor's single write is deploying a new version of
  the transformation file.

## Known limitations

- Only DS-01 has been run end to end with the real model so far. DS-02 and
  DS-03 pass end to end with the scripted provider.
- Cost figures are estimates at API list prices; the model runs on a
  flat-rate ChatGPT plan.
- There is no container image for the API yet, and the stack has not been
  deployed.
