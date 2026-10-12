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
3. The Scenario Lab, a separate page for whoever operates the demo, injects
   one known fault.
4. Deterministic quality checks fail and the monitor opens an incident. No
   model decides that something is wrong.
5. A supervisor runs the agent roles as a Google ADK workflow graph: a
   contract lookup, a source-data investigator and a pipeline investigator in
   parallel, a root-cause analyst, and a repair agent. Each model-backed role
   is one model turn, an ADK agent on Gemini with its own tool allow-list and
   output schema.
6. The repair is run in a staging schema against regression fixtures and every
   quality check, and the load is replayed to prove a retry changes nothing.
7. A person reviews the evidence, diff, and staging result, then approves or
   rejects. Approval is bound to the patch hash.
8. On approval the patch is applied, the pipeline reruns, and the incident
   resolves only if every check passes. Otherwise it rolls back.

## Run it

Requirements: a PostgreSQL database, Python 3.13, Node.js 22 or newer, and a
`GEMINI_API_KEY` (<https://aistudio.google.com/apikey>) in `.env`. The model
defaults to `gemini-3.5-flash-lite`, which a free-tier key can run; set
`GEMINI_MODEL=gemini-3.8-flash` for the stronger model on a paid key. The
earlier providers are still available: `DTM_LLM_PROVIDER=litellm` for the
neatlogs LiteLLM gateway and `DTM_LLM_PROVIDER=codex` for the Codex CLI.
Without model credentials everything except "Investigate" still works; that
button is refused with a message saying why.

The database is any PostgreSQL server, hosted or local: put its connection
string in `.env` as `DTM_DATABASE_URL`. On first start the app creates its
schemas, the seed data, and a read-only role named `dtm_readonly` for the
investigation tools, so the role in the connection string must be allowed to
create roles.

```sh
cp .env.example .env                  # add DTM_DATABASE_URL and GEMINI_API_KEY
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
(cd frontend && npm install && npm run build)
.venv/bin/uvicorn datatracemind.api.app:app --port 8000
```

The first start seeds a deterministic baseline. The same process serves the
API and the built frontend, which has two pages:

| Page | URL | What it is for |
|---|---|---|
| Incident console | <http://127.0.0.1:8000/> | The product: data trust, incidents, evidence, repair review and approval, audit log |
| Scenario Lab | <http://127.0.0.1:8000/lab/> | Operating the demo environment: live traffic, fault injection, reset |

The console never injects anything and is never told what was injected; the
lab never investigates. Each links to the other.

Demo path: in the **Scenario Lab**, pick a scenario, read its briefing, and
press **Inject fault**. Follow **Open INC-0001** to the console, then
**Investigate → review the repair → Approve and apply**. Back in the lab,
**Reset to baseline** returns to the clean state. An investigation takes about
a minute and four model turns.

Live traffic is optional. **Start traffic** in the lab keeps simulated
customers writing orders, payments, and refunds to the source at a chosen rate
(light, steady, or busy); **Send 25 now** writes one batch instead. Every
scenario also reproduces on the seeded baseline with traffic paused, and a
reset pauses it so each run starts from the same data.

For frontend development, `npm run dev` in `frontend/` serves both pages on
port 5173 (`/` and `/lab/`) and proxies `/api` to port 8000.

## Deploy

There is one service to deploy. FastAPI answers `/api` and serves the built
frontend at `/` and `/lab/`; the database is whichever PostgreSQL
`DTM_DATABASE_URL` names.

- **Build:** `pip install -r requirements.txt && (cd frontend && npm ci && npm run build)`
- **Start:** `uvicorn datatracemind.api.app:app --host 0.0.0.0 --port $PORT`
- **Health check:** `GET /api/health`
- **Settings:** `DTM_DATABASE_URL`, `GEMINI_API_KEY`, and `DTM_DEMO_ACCESS_CODE`
  (see [.env.example](.env.example) for the rest)

Run exactly one process and keep it running. The ETL worker, the traffic
stream, and running investigations are threads inside it, so a second worker
or a host that starts the app per request would each have their own.

Run it in the same region as the database. The app makes many small queries,
each on a connection of its own, so its speed follows the network distance:
measured from India against a database in the US, a statement took about a
quarter of a second, the first start (which seeds the baseline) about eight
minutes, and one pipeline run longer than its six-second schedule. A pooled
connection string, such as the one Neon gives by default, works.

## Scenarios

| ID | Fault | What the checks see | Expected ending |
|---|---|---|---|
| DS-01 | A transformation change stops subtracting completed refunds | ETL green; net revenue overstated by exactly the succeeded refunds | Repair |
| DS-02 | A source migration renames `orders.total_amount` | ETL fails; analytics goes stale | Repair |
| DS-03 | The order fact is built with an inner join to payments | ETL green; orders with no payment are missing | Repair |
| DS-04 | The load becomes append-only, then a retry rereads a loaded batch | The deploy run is clean; the retry duplicates business keys and inflates revenue | Repair |
| DS-06 | The source starts writing refunds with a new status, `CHARGEBACK` | ETL green with rejected rows; those refunds are missing; revenue still reconciles | Escalation |
| DS-07 | Reporting days are assigned in UTC instead of the business timezone | ETL green; currency totals reconcile, individual days do not | Repair |
| DS-09 | `orders.total_amount` becomes nullable and some paid orders lose their amount | ETL green with rejected rows; those orders and their payments are missing | Escalation |
| DS-16 | Every refund subtracts the whole order amount | ETL green; net revenue understated, only on partial refunds | Repair |
| DS-17 | Failed payments count as revenue | ETL green; revenue overstated by exactly the failed payments | Repair |

IDs follow the scenario table in the product brief; DS-16 and DS-17 are
additions to it. DS-04 takes two pipeline runs to show, which the lab performs
and reports. DS-06 and DS-09 have no correct transformation change: the
workflow is expected to say so and hand the incident to a person.

Each scenario carries a briefing for the lab's operator (what it changes,
what you will see, which checks should fail, how it should end) and an answer
key (`ground_truth`) that only the evaluator reads. Neither reaches an agent:
nothing an agent can call imports the scenario package (a test enforces
this), no prompt contains it, and an incident never names its scenario.

## Safety model

- **Agents only read.** The investigation tools connect with a PostgreSQL role
  that can `SELECT` from the source, the analytics target, and the
  transformation history, and nothing else. No tool accepts SQL; queries are
  templates with typed parameters.
- **The model has no shell.** It is offered only the role's allowed
  investigation tools (the Codex provider additionally runs with the shell and
  web search disabled and a read-only sandbox).
- **Evidence comes from tool results.** The tools are this project's own
  functions; ADK calls them when the model asks, and the adapter records each
  call as it runs (with Codex, it reads them from the event stream). A turn
  fails unless the role's required calls succeeded. The model's description of
  a result is never stored as evidence.
- **A repair is data, not code.** It is a new version of one allowlisted file,
  `etl/transforms/order_revenue.yaml`, and is accepted only if it parses
  against a strict schema and changes a handful of lines.
- **Deterministic checks decide.** Staging validation, approval checks, and
  post-fix verification are code. A model's statement that something passed
  proves nothing.
- **A repair must survive a retry.** Staging loads the same rows a second
  time and reruns every check, so a patch that leaves the load non-idempotent
  is rejected even though a fresh staging load of it looks clean.
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
.venv/bin/python -m pytest                           # 156 tests, about a minute on a local database
.venv/bin/python -m datatracemind.evaluation DS-01   # one real run, scored
```

The tests never touch the demo's data. They create a separate
`datatracemind_test` database on the server `DTM_DATABASE_URL` names, so its
role must be allowed to create databases; set `DTM_TEST_ADMIN_URL` to run them
on another server. They spend no model usage: the workflow is driven by a
scripted stand-in provider whose tool calls are real. They cover every
scenario from injection to its expected ending,
and the blocked paths as well as the happy one: applying without approval, a
changed patch after approval, a patch that failed validation, rejection, and
a failed post-fix verification.

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
  scenarios/      fault scenarios, their briefings, and their answer keys
  tools/          read-only investigation tools and the MCP server
  llm/            provider contract; the Gemini (ADK), LiteLLM gateway, and Codex adapters
  agents/         role prompts and the role runner
  orchestration/  supervisor, the ADK workflow graph, and routing rules
  repair/         proposals, staging validation, approval, executor
  evaluation.py   scenario evaluator
frontend/         React: the incident console (/) and the Scenario Lab (/lab/)
tests/
```

## Hackathon tools

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

- Gemini has run one whole investigation for real: DS-01 on
  `gemini-3.5-flash-lite`, which reached `awaiting_approval` with the correct
  repair and a passing staging validation, but named the root-cause category
  `schema_drift` where the answer key says `refund_logic`. It was not taken
  through approval. `gemini-3.8-flash` has only run single investigator turns:
  a free-tier key gets 5 requests a minute and 20 a day on it, and both ran
  out. A per-minute limit is waited out; a daily one escalates the incident.
- The other real-model runs recorded are of DS-01 too, with an earlier
  provider: three investigations, one of them taken through approval, apply,
  and verification. Every other scenario passes end to end with the scripted
  provider only, so how a real model diagnoses DS-02 through DS-17, and
  whether it escalates DS-06 and DS-09 as intended, is not yet known.
- With DS-04 active and live traffic running, the duplication keeps growing:
  each run rereads the last few seconds and appends them again. That is the
  fault behaving as it would in production, not a second fault.
- Cost figures are estimates at list prices, not charges: a Gemini key on the
  free tier is not billed, and the Codex provider runs on a flat-rate ChatGPT
  plan.
- The service is one process and cannot be scaled out: the ETL worker, the
  traffic stream, and running investigations are threads inside it.
