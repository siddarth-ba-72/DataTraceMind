# DataTraceMind --- Agentic Data Reliability Engineer

## Hackathon Product Brief, Technical Blueprint, and Phased Implementation Plan

**Document purpose:** This document is the implementation source of
truth for coding agents building DataTraceMind. It defines the problem,
hackathon scope, architecture, simulated production incidents, agentic
workflow, safety model, acceptance criteria, and implementation phases.

**Target build:** A working, demoable MVP built by one or two people in
approximately 48 hours.

**Event:** neatHack (neatlogs), 10--12 October 2026. Every project must
use **neatlogs**, **Entire**, and **cfo.ai** and keep evidence of all
three for the submission and demo. Section 4 defines how each one is
used here; it is binding on every phase of this plan.

------------------------------------------------------------------------

## 1. Executive Summary

DataTraceMind is an agentic data-reliability assistant that detects data
pipeline anomalies, investigates the likely root cause using evidence
from the source database, ETL pipeline, analytical database, and
documented business rules, then proposes a minimal repair. It validates
that repair in an isolated staging environment. A human must approve the
proposed fix before it is applied to the demo environment. After
approval, DataTraceMind applies the fix, reruns the pipeline, verifies the
result, and records an audit trail.

The key product differentiator is **silent data corruption**: a pipeline
may report `SUCCESS` while its analytical output is wrong. DataTraceMind
should demonstrate that it can detect and explain these failures---not
merely alert when a job crashes.

### Product promise

> Detect data reliability issues, show evidence for the likely root
> cause, propose a tested fix, and apply it only after human approval.

### MVP demo path

1.  An e-commerce simulator generates orders, payments, and refunds.
2.  A lightweight ETL job moves and transforms source data into an
    analytical store.
3.  A deterministic fault injector introduces a known pipeline/data
    issue.
4.  DataTraceMind detects an invariant violation or metric mismatch.
5.  A supervisor agent orchestrates investigation tasks.
6.  Investigator agents collect evidence; an analyst ranks root-cause
    hypotheses.
7.  A repair agent proposes a patch.
8.  A validation step runs the patch in staging and checks data-quality
    invariants.
9.  The UI shows the evidence, root cause, patch diff, expected impact,
    and approval controls.
10. After a human approves, DataTraceMind applies the patch in the demo
    environment, reruns the pipeline, and verifies recovery.

------------------------------------------------------------------------

## 2. Problem Statement

Modern organizations move data through multiple systems: transactional
applications, ETL/ELT jobs, data warehouses, dashboards, and downstream
reports. Failures are not always obvious. A pipeline can finish
successfully but silently omit records, double-count revenue, mishandle
refunds, use stale data, or apply outdated business logic.

These issues are expensive because teams often discover them only after
a dashboard, financial report, or downstream service has already used
incorrect data. Diagnosis can require several people to inspect source
records, schemas, ETL code, run logs, checkpoints, and business
definitions.

### Core problem

**How might we build an AI-assisted data reliability engineer that
detects data anomalies, investigates their root cause across a
lightweight data stack, recommends a repair, proves the repair in
staging, and applies it only after explicit human approval?**

### Primary user story

As a **data engineer or analytics engineer**, I want DataTraceMind to
identify when a data pipeline is broken or producing incorrect results,
explain the cause with verifiable evidence, and offer a validated
repair, so that I can restore trustworthy data quickly without blindly
trusting an AI-generated change.

### Supporting user stories

-   As a data engineer, I want to see why an incident was opened and
    which data-quality rule failed.
-   As an on-call engineer, I want a concise alert with severity,
    affected metrics, probable cause, and next action.
-   As an engineer, I want to inspect the evidence and SQL/query results
    used to reach a diagnosis.
-   As an engineer, I want a patch diff and before/after test results
    before approving a fix.
-   As an authorized user, I want to approve or reject the proposed fix.
-   As an engineer, I want every tool call, patch, approval, and
    verification result recorded for auditability.
-   As a stakeholder, I want the dashboard to show whether the data is
    trustworthy, stale, or currently under investigation.

### Goals

1.  Demonstrate detection of both **hard failures** and **silent
    data-quality failures**.
2.  Use a multi-agent workflow with a supervisor/master agent and
    specialized roles.
3.  Ground diagnoses in observable evidence rather than unsupported LLM
    claims.
4.  Generate a minimal, reviewable repair proposal.
5.  Validate the proposed repair in staging before any application.
6.  Require explicit human approval before applying the fix.
7.  Show the full workflow in an understandable live UI.
8.  Keep infrastructure small enough for a 48-hour hackathon.
9.  Satisfy the neatHack mandatory-tool rule: trace the agent with
    neatlogs, capture the build with Entire, plan the business with
    cfo.ai, and retain checkable evidence of each (Section 4).

### Non-goals for the hackathon MVP

-   Connecting to real customer production systems.
-   Unrestricted autonomous changes to production databases or ETL code.
-   Kubernetes, Spark, Kafka, Airflow, Debezium, or a distributed
    microservice deployment unless already available and trivial to use.
-   Training or fine-tuning a custom model.
-   Solving every possible data incident.
-   Claiming that a generated diagnosis is correct without passing
    deterministic checks.

------------------------------------------------------------------------

## 3. Product Scope and Demo Use Case

### Selected use case: E-commerce order-to-revenue pipeline

Use a small e-commerce application because it gives us realistic
transactional events, understandable business rules, and metrics that
can be reconciled across systems.

The system simulates:

-   Customers placing orders.
-   Payments succeeding or failing.
-   Orders changing status.
-   Refunds being issued.
-   An ETL job transforming operational records into analytics tables.
-   A dashboard reporting revenue, order counts, refund totals, and
    pipeline freshness.

The most important demo incident is **silent revenue corruption caused
by incorrect refund handling**. The ETL job reports success, but the
analytics revenue metric is wrong because its transformation does not
account for a newly introduced `REFUNDED` state or refund events.
DataTraceMind must detect the discrepancy, retrieve the approved metric
definition, identify the faulty rule, propose a correction, validate it,
obtain approval, and apply it.

### Keep the system intentionally small

Use a **modular monolith plus one ETL worker**, not a fleet of services.

  -----------------------------------------------------------------------
  Component               Recommended choice      Responsibility
  ----------------------- ----------------------- -----------------------
  Transaction             Spring Boot             Generate and expose
  simulator/API                                   e-commerce transactions

  Operational database    PostgreSQL              Source of truth for
                                                  orders, payments,
                                                  refunds

  ETL worker              Python                  Extract, transform,
                                                  validate, and load
                                                  analytics data

  Analytical database     ClickHouse, or          Store transformed
                          PostgreSQL analytics    reporting data
                          schema as fallback      

  Agent orchestration API Python + FastAPI        Incident state, agent
                                                  tools, orchestration,
                                                  repair lifecycle

  LLM integration         One model provider      Reasoning, hypothesis
                          behind a small adapter, ranking, repair
                          wrapped with neatlogs   proposal

  Frontend                React                   Live transactions,
                                                  pipeline health,
                                                  incident timeline,
                                                  approvals

  Notifications           In-app alert first;     Notify the user when an
                          optional webhook        incident is detected

  Agent observability     neatlogs Python SDK     Trace model/tool calls,
  (mandatory)                                     retries, failures,
                                                  latency, tokens, cost

  Build-process capture   Entire CLI on the       Checkpoint coding-agent
  (mandatory)             public GitHub repo      sessions against
                                                  commits

  Business plan           cfo.ai                  Pricing, costs,
  (mandatory)                                     revenue, and runway
                                                  model for submission
  -----------------------------------------------------------------------

The last three rows are required by the hackathon rules and are not
subject to the scope cuts described elsewhere in this document. See
Section 4.

**Hackathon simplification:** If ClickHouse setup or integration
threatens the schedule, use a separate PostgreSQL schema/database as the
analytical target. Preserve a clear source-versus-analytics boundary so
the demo still represents a pipeline. Do not spend the hackathon
building infrastructure instead of the product.

### High-level architecture

``` text
                 ┌─────────────────────────────┐
                 │     React Dashboard         │
                 │ live data / incidents /     │
                 │ evidence / patch / approval │
                 └──────────────┬──────────────┘
                                │ HTTP
                 ┌──────────────▼──────────────┐
                 │ FastAPI: DataTraceMind      │   traces
                 │ Supervisor + agent workflow │──────────▶ neatlogs
                 │ tools + approval + audit    │
                 └───────┬──────────────┬──────┘
                         │              │
                  read-only tools    staging/repair
                         │              │
          ┌──────────────▼───┐     ┌────▼────────────┐
          │ PostgreSQL OLTP  │     │ Staging target  │
          │ orders/payments/ │     │ validation run  │
          │ refunds          │     └─────────────────┘
          └─────────┬────────┘
                    │
                    ▼
             ┌─────────────┐       ┌─────────────────┐
             │ Python ETL  │──────▶│ Analytics store │
             │ + fault lab │       │ ClickHouse or   │
             └─────────────┘       │ PostgreSQL      │
                                   └─────────────────┘
```

### Suggested data model

Keep the model small and explicit. Names can be adjusted, but the
relationships and business meaning must be documented.

**Operational tables**

-   `customers`: `customer_id`, `created_at`
-   `orders`: `order_id`, `customer_id`, `order_status`, `total_amount`,
    `currency`, `created_at`, `updated_at`
-   `payments`: `payment_id`, `order_id`, `payment_status`, `amount`,
    `paid_at`
-   `refunds`: `refund_id`, `payment_id`, `order_id`, `refund_status`,
    `refund_amount`, `created_at`

**Analytics tables**

-   `fact_orders`: one row per order; transformed status and amount
    fields
-   `fact_payments`: one row per payment
-   `fact_refunds`: one row per refund
-   `daily_revenue`: date-level aggregate containing gross paid amount,
    successful refunds, net revenue, order count, and refresh timestamp
-   `etl_run_log`: run ID, start/end time, status,
    extracted/transformed/rejected/loaded counts, watermark, error
    summary
-   `data_quality_results`: check name, run ID, observed value, expected
    condition, pass/fail

**DataTraceMind operational tables or equivalent state store**

-   `incidents`: incident ID, type, severity, status, detected time,
    summary, source/target metrics
-   `agent_events`: incident ID, agent, event type, status, input/output
    summary, timestamp
-   `evidence`: incident ID, evidence type, source/tool reference,
    result summary, timestamp
-   `repair_proposals`: incident ID, patch, rationale, risk, validation
    result, approval status
-   `audit_log`: actor, action, target, timestamp, result

For a 48-hour build, incident and audit state may be held in SQLite or
PostgreSQL. Avoid adding a separate database just for orchestration.

These tables are not replaced by neatlogs. neatlogs holds the runtime
trace of each investigation (model calls, tool calls, tokens, cost,
latency); the tables above remain the application's own record for the
UI timeline and for approval and audit enforcement, which must work
even if trace export is unavailable.

### Seed and simulated traffic

-   Seed a small set of customers and historical orders.
-   Generate new orders/payments/refunds every 0.5--2 seconds, with a
    small number of TPS being sufficient.
-   Use deterministic random seeds for reproducible demo scenarios.
-   Include successful and failed payments, cancellations, partial
    refunds, and full refunds.
-   Add a **Reset demo** control that restores a known baseline and
    clears incident state.
-   Display source records and analytics results so the audience can see
    the discrepancy.

### ETL behavior

Implement a simple Python ETL loop running every 5--10 seconds:

1.  Read new/changed operational records using a reliable watermark or
    high-water mark.
2.  Transform records according to versioned transformation logic.
3.  Validate types, required fields, relationships, and business
    invariants.
4.  Load analytics tables idempotently where practical.
5.  Record run metadata and data-quality results.
6.  Publish pipeline health and freshness to the dashboard.
7.  Trigger a DataTraceMind evaluation when a check fails or a metric
    discrepancy is detected.

The ETL must be deliberately simple but not misleading. Use stable
source IDs, an explicit watermark, and documented retry behavior. If a
scenario is meant to demonstrate duplicates, deliberately configure an
append-style or non-idempotent path for that scenario and explain why it
creates duplicates.

------------------------------------------------------------------------

## 4. Mandatory Hackathon Tooling and Submission Evidence

neatHack requires every project to use **neatlogs**, **Entire**, and
**cfo.ai**, and to keep evidence of all three for the submission and
demo. How effectively the three are used is 25% of the judging score.
AWS and Crustdata credits are optional resources.

Sources: the organisers' team-leader email of 10 October 2026 ("Your
neatHack team is ready --- setup guide & credits") and the hackathon
guide at <https://neatlogs.com/hackathon>. If this section and the
published guide disagree, the guide wins; recheck it before submitting.

### Summary

  ------------------------------------------------------------------------
  Tool         Status     Where it is used        Evidence to keep
  ------------ ---------- ----------------------- ------------------------
  neatlogs     Mandatory  DataTraceMind agent     Trace links or
                          runtime (FastAPI        screenshots; a
                          service)                before/after pair of
                                                  runs; a recovery run

  Entire       Mandatory  The Git repository and  Accessible checkpoint
                          coding-agent sessions   link and checkpoint ID
                          that build the product

  cfo.ai       Mandatory  Business plan for the   The plan: pricing,
                          product                 costs, revenue, runway

  AWS          Optional   Model access or demo    None required
                          hosting, if useful

  Crustdata    Optional   Not planned for the     None required
                          MVP
  ------------------------------------------------------------------------

### neatlogs --- agent observability (runtime)

**Role.** neatlogs is the trace system for the DataTraceMind agent
workflow. Every investigation run must appear in the neatlogs dashboard
as one trace, with each agent role and each tool call nested under it.
The team also uses those traces itself: to investigate agent failures
and to verify that a change to prompts, tools, or routing actually
improved the run.

**Setup (Phase 0)**

1.  A human signs up at <https://app.neatlogs.com> and creates the
    organisation and project. Sign-up is browser-only; a coding agent
    cannot do it.
2.  In Settings → API keys, copy the **Project API key**. Do not use the
    HTTP ingest key that starts with `nlw_`; the SDK returns `401` with
    it.
3.  Export it as `NEATLOGS_API_KEY`. List the variable name in
    `.env.example`; never commit the value.
4.  Install the SDK: `pip install -U neatlogs`.
5.  Run `python -m neatlogs doctor --probe` and confirm `TRACE_VISIBLE`
    and `TRACE_FINALIZED` are `PASS` before instrumenting the app.

**Instrumentation rules**

-   Call `neatlogs.init(api_key=os.environ["NEATLOGS_API_KEY"],
    workflow_name="datatracemind")` once at FastAPI startup, before
    importing the libraries it instruments.
-   Wrap the model client inside the LLM adapter:
    `client = neatlogs.wrap(<provider client>)`. All model calls go
    through that adapter, so none can bypass tracing.
-   **If the workflow uses LangGraph or LangChain:** `neatlogs.init()`
    alone traces nothing. Create one `neatlogs.langchain_handler()` per
    investigation run and pass it at the graph invocation
    (`config={"callbacks": [handler]}`), not on individual node calls,
    and reuse that handler for the whole run. Use Python 3.11+ if the
    graph is invoked asynchronously; on 3.10 nested model spans are
    lost.
-   **If the workflow is an explicit state machine:** decorate the run
    entry point with `@neatlogs.span(kind="WORKFLOW")`, each agent role
    with `kind="AGENT"`, each tool in Section 11 with `kind="TOOL"`, and
    the validation and approval gate checks with `kind="GUARDRAIL"`.
-   One trace per investigation run. Attach `incident_id`,
    `scenario_id`, and a workflow/prompt version label to each trace so
    runs can be compared and an incident page can point to its trace.
-   The FastAPI service is long-running: do not flush per request. Call
    `neatlogs.flush()` then `neatlogs.shutdown()` once in the lifespan
    shutdown handler. One-off scripts (evaluator, seed, replay) must
    flush and shut down before exiting or their trace may never appear.
-   Trace export must never block or fail the incident workflow. If the
    key is missing or the network is down, the workflow continues and
    the application's own `agent_events` and `audit_log` still record
    the run.
-   Apply the same hygiene as the evidence rules: send result summaries,
    not whole tables, and keep secrets and unnecessary personal data out
    of spans. The SDK offers `mask` and PII-redaction options if needed.

**Reading traces back (optional).** `neatlogs-cli` is a separate npm
package that needs Node.js 22 or 24. After
`npm install --global neatlogs-cli@0.2.0` and
`neatlogs auth login --device` (a human approves the code in a browser),
`neatlogs traces list --limit 5` and
`neatlogs traces get '<trace-id>' --json` let a coding agent check its
own work without opening the dashboard.

**Evidence to keep**

-   Trace links or screenshots from this project. Traces should cover
    model and tool calls, state changes, retries, failures, agent
    messages, latency, token usage, and cost.
-   A before-and-after pair of runs with the numbers compared (see
    Section 15).
-   A run in which an action failed, showing what the agent did next
    --- for example a staging validation failure that routes back to
    repair, or a tool error followed by a bounded retry or escalation.

### Entire --- build-process capture (development time)

**Role.** Entire records the coding-agent sessions that produce the code
and links them to Git commits as checkpoints. It is a development-time
tool applied to this repository; it is not a runtime dependency of
DataTraceMind.

**Prerequisites**

-   The code lives in a **public GitHub repository** with commits dated
    during the hackathon. This is a submission requirement in its own
    right.
-   Every team member signs in to Entire with GitHub and authorises the
    Entire GitHub App. If the repository belongs to a GitHub
    organisation, an org admin must install the app and import the
    repository; until then the repo and its sessions appear for no one.
    Syncing can take up to an hour, so do this before any feature work.

**Setup**

``` sh
brew install --cask entireio/tap/entire    # macOS
cd <repo>
entire enable --agent <agent-integration-name>
```

Then run a coding-agent session, make one focused change, and commit it,
answering **yes** when asked to link the commit to session context.
Confirm the checkpoint exists:

``` sh
entire checkpoint list
entire checkpoint explain HEAD
entire status                               # after pushing: verify sync
```

**Working rules**

-   Enable Entire before the first feature commit. A committed
    checkpoint needs both captured agent context and a Git commit, so
    work done outside an enabled session leaves no checkpoint.
-   Commit in small, feature-sized increments and push regularly, so
    every phase of Section 13 has checkpoints behind it.
-   Checkpoints in a public repository are public. Keep API keys and
    other secrets out of coding-agent sessions as well as out of code.
-   Trails (feature-flagged; access must be requested), Graph, and Brain
    are optional. Try Graph only after the DS-01 vertical slice works:
    `entire plugin install graph`, then
    `entire graph init-agents --repo .`. It writes
    `.entire/graph-agent.md` and adds instruction blocks to `AGENTS.md`
    and `CLAUDE.md`; review and commit all three.

**Evidence to keep**

-   An accessible checkpoint link **and** its checkpoint ID. Link
    format: `https://entire.io/gh/OWNER/REPO/commit/FULL_COMMIT_SHA`. A
    URL alone does not grant access; confirm a reviewer can open it.
-   For the demo: open repository → branch → commit → checkpoint, then
    show **Sessions** (requests and decisions) followed by **Changes**
    (the implementation).
-   Entire Graph output such as `entire graph stats --repo .`, if Graph
    was used.

### cfo.ai --- business plan

**Role.** cfo.ai produces the business plan for DataTraceMind as a
product: pricing, costs, revenue, and runway. It is a submission
deliverable with no runtime integration.

**Setup (human only).** Sign up through the neatHack link
<https://cfo.ai/neathacks>. A card is required at sign-up; per the
organisers, cancel the subscription immediately in Settings → Billing
so that nothing is billed. Credits are provided directly by the neatHack
team. Because this step involves payment details, only the team leader
performs it.

**What the plan must cover**

-   **Pricing:** one explicit hypothesis, for example per monitored
    pipeline per month or per resolved incident.
-   **Costs:** inference cost per investigation taken from real neatlogs
    traces (tokens and cost per run), plus infrastructure.
-   **Revenue:** at least a base and a growth scenario.
-   **Runway:** months of runway under each scenario.

Use measured numbers where they exist and label everything else as an
assumption.

**Evidence to keep.** The resulting business plan, included in the
submission (store an export or screenshots under `docs/business-plan/`)
and shown in the demo video.

### Optional resources

-   **AWS credits.** One \$25 allowance shared by the whole team; access
    is sent separately to the team leader through a private link that
    expires after 24 hours. Event limits: region `us-east-1` and the
    supplied `Hackathon_VPC` only (never create a VPC); only EC2,
    Lambda, IAM roles, and load balancers; one EC2 instance per team
    (terminate it before launching a replacement), of type t2, t3, or
    t3a from nano to large; at most 50 GB of EBS. The page does not
    meter spend, so stop or terminate resources when finished. Local
    Docker Compose remains the default runtime. The only sensible use is
    hosting the demo on the single EC2 instance, so that judges can open
    a live URL (Postgres, ETL, FastAPI, and the React build via Docker
    Compose). Bedrock and other services are not on the allowed list.
    Do not make the demo depend on so small a budget; keep the recorded
    demo as the backup.
-   **Crustdata.** 5,000 credits for company and people data, claimed
    with the promo code in the organisers' email (sign up by email, not
    "Continue with Google", so the code applies). The order-to-revenue
    pipeline does not need this data; it is out of MVP scope.

### Steps only a human may perform

Coding agents must not attempt these. They stop and ask instead.

-   Creating accounts or signing in: neatlogs, Entire (GitHub
    authorisation), cfo.ai, Crustdata, AWS.
-   Entering card details and cancelling the cfo.ai subscription.
-   Approving the `neatlogs auth login --device` code.
-   Placing API keys in the local environment.
-   Posting in public and making the final submission.

### Judging, schedule, and submission

**Judging weights:** the agent 30%; use of neatlogs, Entire, and cfo.ai
25%; demo video 20%; building in public 15%; usefulness and originality
10%.

**Schedule (IST):** the hackathon begins 10 October at 12:30 PM; the
submission window is 12 October, 7:00--11:55 PM; winners are announced
16 October.

**How to submit:** on X, by quoting the official neatlogs post with the
team, name, project, public repository, and demo.

**Evidence the submission must be able to show**

-   **The agent improved:** a before-and-after pair of runs (for example
    two neatlogs traces) with the numbers compared.
-   **neatlogs:** trace links or screenshots from the project.
-   **Entire:** checkpoints or Entire Graph output, with a checkpoint
    link and ID.
-   **cfo.ai:** the business plan.
-   **Recovery from failure:** a run in which an action failed, showing
    what the agent did next.
-   **Building in public:** links to what was shared during the
    hackathon, tagged `#neatHack`.
-   **Code:** a public repository with commits dated during the
    hackathon.
-   **Demo video:** a clear explanation of the agent and of how
    neatlogs, Entire, and cfo.ai were used.

Keep a running `hackathon.md` build log at the repository root (the
organisers publish a build-log skill at
<https://neatlogs.com/hackathon/skill>). Append a dated entry per
working session: what changed, which tests actually ran, what failed,
and links to sanitised evidence. It is the collection point for
everything in the list above. The skill is a convenience, not a rule.

------------------------------------------------------------------------

## 5. Data Contracts and Business Rules

DataTraceMind cannot reliably infer every business meaning from table
names. Important definitions must be available as versioned,
machine-readable contracts or simple Markdown/YAML files that agents can
retrieve.

Example `contracts/revenue.md`:

``` markdown
# Revenue metric contract

- Metric: net_revenue
- Grain: reporting currency and calendar day
- Include: successful captured payments for valid orders
- Subtract: successfully completed refunds
- Exclude: failed payments and cancelled orders without captured payment
- Partial refunds: subtract the successfully refunded amount, not the full order amount
- Currency: aggregate currencies separately unless an explicit conversion rule is configured
- Time basis: use the approved business timezone
- Reconciliation: source-derived net revenue must match analytics within configured tolerance
- Changes to these rules require an explicit version and approval
```

Treat this as an example contract, not a universal accounting
definition. The demo's approved policy is the source of truth.

### Required invariants

Implement deterministic checks such as:

-   Source-derived net revenue reconciles to analytical net revenue
    within tolerance.
-   Every analytics order references a valid source order.
-   A payment is counted as revenue only when its payment state
    qualifies under the contract.
-   A refund is subtracted only if its refund state qualifies under the
    contract.
-   A full refund cannot subtract more than the associated captured
    amount, unless the contract explicitly allows adjustments.
-   Row counts and distinct source IDs are tracked across extract,
    transform, reject, and load.
-   `daily_revenue.refreshed_at` meets the freshness objective.
-   The analytics table has no unexpected duplicate business keys.
-   Required columns and expected types exist.
-   The pipeline's declared status is consistent with its actual
    validation results.

The LLM may help interpret evidence and propose a cause. **Deterministic
checks decide whether a repair passes.**

------------------------------------------------------------------------

## 6. Pipeline Failure Scenario Lab

This is a central part of the product. Build a **scenario lab** that can
inject realistic, repeatable faults and then reset to a clean state.
Each scenario must define its preconditions, fault injection, expected
observable symptoms, ground-truth cause, and the invariant that should
fail.

Do not rely on an LLM to invent the fault. The scenario injector creates
a known issue; DataTraceMind must independently diagnose it from evidence.
Keep ground truth hidden from the investigation agents and expose it
only to the evaluator/demo harness.

### Priority scenarios

  ----------------------------------------------------------------------------------------
  ID             Scenario                Failure type      Typical symptom  MVP priority
  -------------- ----------------------- ----------------- ---------------- --------------
  DS-01          Refund state omitted    Silent semantic   ETL is green;    P0
                 from revenue logic      corruption        revenue is       
                                                           overstated       

  DS-02          Schema/column drift     Hard failure      ETL errors       P0
                                                           because expected 
                                                           column is        
                                                           missing          

  DS-03          Join drops valid orders Silent record     Analytics        P0
                                         loss              count/revenue    
                                                           lower than       
                                                           source           

  DS-04          Duplicate ingestion on  Silent            Counts/revenue   P1
                 retry                   duplication       increase after a 
                                                           retry            

  DS-05          Late-arriving refund or Stale aggregate   Recent or        P1
                 payment                                   historical daily 
                                                           totals are wrong 

  DS-06          New enum/status value   Semantic          New records are  P1
                                         incompatibility   rejected or      
                                                           misclassified    

  DS-07          Timezone boundary       Temporal          Records appear   P1
                 mismatch                corruption        on the wrong     
                                                           reporting day    

  DS-08          Join fan-out            Aggregate         Totals grow      P1
                                         inflation         after joining    
                                                           one-to-many      
                                                           tables           

  DS-09          Nullability/precision   Validation or     Rows rejected or P1
                 change                  rounding issue    totals differ by 
                                                           small amounts    

  DS-10          Deleted/updated source  Stale target      Analytics        P2
                 row not propagated                        contains         
                                                           obsolete state   

  DS-11          Watermark/checkpoint    Missing data      Source has       P2
                 skips records                             records after a  
                                                           run but target   
                                                           does not         

  DS-12          Pipeline partially      Partial           Some tables are  P2
                 fails after writing     inconsistency     current while    
                                                           others are stale 

  DS-13          Currency aggregation    Semantic          Totals combine   P2
                 without conversion      corruption        unlike           
                                                           currencies       

  DS-14          Historical backfill     Mixed metric      Recent and       P2
                 uses new logic          definitions       historical       
                 inconsistently                            numbers disagree 

  DS-15          Source is stale or      Freshness failure ETL succeeds but P2
                 stops producing events                    data stops       
                                                           changing         
  ----------------------------------------------------------------------------------------

### DS-01 --- Refund logic omitted (primary demo)

**Setup** - Seed paid orders and successful full/partial refunds. -
Compute the approved source-derived net revenue. - Ensure the baseline
analytics result reconciles.

**Inject** - Modify the transformation so it subtracts only one status
or ignores successful refund rows. - Keep the ETL job technically
successful so the failure is silent.

**Expected symptoms** - ETL status remains `SUCCESS`. - Source-derived
net revenue and analytics net revenue diverge. - The discrepancy
corresponds to one or more refund amounts. - Refund counts/amounts in
source do not reconcile with the analytics result.

**Expected diagnosis** - The transformation's refund inclusion/filter
logic conflicts with the approved revenue contract. - The agent should
show the relevant filter, affected refund IDs, and discrepancy
calculation.

**Expected repair** - Propose a minimal transformation change that
includes only contract-approved completed refunds and handles partial
refunds correctly. - Run the corrected transformation against a staging
copy. - Check that net revenue reconciles and that unrelated metrics
have not regressed.

### DS-02 --- Schema drift

**Setup** - Baseline ETL expects `orders.total_amount`.

**Inject** - Apply a versioned migration or scenario-specific view that
renames the field to `order_total`, or change a column type in a
controlled way.

**Expected symptoms** - ETL run fails with a missing-column or
type-conversion error. - The source schema differs from the schema
assumed by the transformation. - The target's last successful refresh
becomes stale.

**Expected diagnosis** - The code references a field that no longer
exists or whose type is incompatible. - The diagnosis must cite the
observed schema and failing transformation reference.

**Expected repair** - Generate a patch that maps the new field or adds a
compatible adapter. - Validate the patch against the current schema in
staging. - Do not silently alter the source schema as a repair.

### DS-03 --- Join drops valid orders

**Setup** - Include orders that are created but not yet paid, plus paid
orders. - Analytics contract defines whether each order should appear in
the order fact and how revenue should be calculated.

**Inject** - Use an `INNER JOIN` from orders to payments when the order
fact requires all orders, or otherwise use a join that excludes valid
rows.

**Expected symptoms** - Source order count is greater than analytics
order count. - Missing IDs are concentrated among orders with no
payment. - Revenue may or may not differ depending on the metric; do not
conflate order-count correctness with revenue correctness.

**Expected diagnosis** - Join type or join placement excludes records
required by the contract. - The agent identifies a sample of missing
order IDs and their source state.

**Expected repair** - Propose the correct join strategy, such as
preserving orders with a `LEFT JOIN` and applying revenue eligibility
rules separately. - Validate row counts, uniqueness, and revenue
invariants.

### DS-04 --- Duplicate ingestion after retry

**Setup** - Use a reproducible batch with known source IDs and target
row counts.

**Inject** - Simulate a crash after writing target rows but before
recording the checkpoint; rerun using an append-only/non-idempotent
load.

**Expected symptoms** - Target row count or aggregate amount increases
while distinct source IDs remain unchanged. - Duplicate business keys
appear. - Run logs show a retry around the affected batch.

**Expected diagnosis** - Retry/checkpoint ordering and non-idempotent
writes allow a batch to be loaded twice.

**Expected repair** - Propose deduplication or idempotent
upsert/replace-by-key behavior, and correct checkpoint sequencing. -
Verify unique business keys and unchanged totals after replay.

### DS-05 --- Late-arriving payment or refund

**Setup** - Create an older order whose payment or refund is ingested
after its original reporting date has already been aggregated.

**Inject** - Delay the event beyond the ETL watermark or make the ETL
process only the current day without reprocessing affected historical
aggregates.

**Expected symptoms** - Raw event exists in the source but the
corresponding daily aggregate remains unchanged. - Data freshness may
look healthy for recent rows even though a historical partition is
stale.

**Expected diagnosis** - The incremental extraction/recomputation
strategy does not handle late-arriving events.

**Expected repair** - Propose a bounded lookback, affected-date
recomputation, or event-driven correction. - Validate both the affected
historical day and unaffected days.

### DS-06 --- New enum/status value

**Setup** - Transformation expects a fixed set of statuses.

**Inject** - Introduce a new status such as `PARTIALLY_REFUNDED` or
`PAYMENT_REVERSED` without updating the mapping.

**Expected symptoms** - New records are rejected, mapped to `UNKNOWN`,
or incorrectly counted. - Existing statuses continue to work, making the
problem appear intermittent.

**Expected diagnosis** - Source enum values exceed the values understood
by the transformation or contract.

**Expected repair** - Propose an explicit mapping and, if needed,
request a business-rule decision. - If the financial meaning is
ambiguous, stop and ask for human clarification rather than guessing.

### DS-07 --- Timezone boundary mismatch

**Setup** - Place transactions near midnight UTC and near midnight in
the configured business timezone.

**Inject** - Aggregate using UTC while the contract specifies a
different business timezone, or convert a timestamp twice.

**Expected symptoms** - Daily counts/revenue differ on adjacent dates
while a multi-day total may still reconcile. - Mismatches cluster near
day boundaries.

**Expected diagnosis** - Timestamp conversion or date truncation differs
from the metric contract.

**Expected repair** - Propose a consistent timezone conversion before
date grouping. - Validate boundary fixtures and adjacent-day aggregates.

### DS-08 --- Join fan-out

**Setup** - Use an order with multiple payments or multiple refund rows.

**Inject** - Join orders, payments, and refunds at incompatible grains
before aggregation.

**Expected symptoms** - Revenue is inflated by repeated order/payment
amounts. - The increase correlates with records having multiple child
rows.

**Expected diagnosis** - One-to-many joins multiply rows and cause
aggregates to be counted multiple times.

**Expected repair** - Aggregate child entities at the correct grain
before joining, or calculate each metric at its own grain. - Validate
distinct keys and totals for single-child and multi-child fixtures.

### DS-09 --- Nullability or numeric precision change

**Setup** - ETL expects a non-null numeric amount with a fixed
precision.

**Inject** - Add a null amount or change precision/scale in a controlled
migration.

**Expected symptoms** - Rows are rejected, silently defaulted, rounded,
or fail type conversion. - Difference may be concentrated in a subset of
records.

**Expected diagnosis** - A schema constraint or conversion assumption is
incompatible with actual source values.

**Expected repair** - Propose explicit null handling or safe precision
mapping only if the business contract defines the behavior. - Never
invent a financial default such as zero without approval.

### DS-10 --- Deletes or updates not propagated

**Setup** - Load an order into analytics, then update its status or mark
it deleted in the source.

**Inject** - Use insert-only ETL logic that does not propagate
updates/deletes.

**Expected symptoms** - Source and target states disagree for known
IDs. - Old rows persist after the source changes.

**Expected diagnosis** - The incremental strategy handles new records
but not source mutations.

**Expected repair** - Propose merge/upsert, tombstone, or controlled
reconciliation behavior appropriate to the chosen data model. - Verify
the target reflects the source's current approved state.

### DS-11 --- Incorrect watermark/checkpoint

**Setup** - Generate records with ordered IDs and timestamps.

**Inject** - Advance the checkpoint too far, use a non-unique timestamp
without a tie-breaker, or mishandle inclusive/exclusive boundaries.

**Expected symptoms** - A range of source IDs is missing from
analytics. - The ETL job can report success and later runs may not
recover the missing records.

**Expected diagnosis** - The watermark skips records due to boundary or
ordering logic.

**Expected repair** - Propose a stable composite cursor or safe overlap
strategy with deduplication. - Verify no source IDs are missing across
the injected boundary.

### DS-12 --- Partial pipeline failure

**Setup** - Pipeline loads fact tables and then refreshes daily
aggregates.

**Inject** - Fail after fact tables are written but before aggregate
refresh completes.

**Expected symptoms** - Some target tables have recent timestamps while
dependent aggregates are stale. - Run status may be incorrect if success
is recorded too early.

**Expected diagnosis** - The pipeline does not represent or recover
partial completion correctly.

**Expected repair** - Propose correct run-state handling, retry-safe
steps, and dependency-aware aggregate refresh. - Verify the rerun
restores consistency without duplication.

### DS-13 --- Currency aggregation error

**Setup** - Include transactions in at least two currencies.

**Inject** - Sum raw amounts into one total without conversion or
currency grouping.

**Expected symptoms** - Reported total is numerically plausible but
semantically invalid. - Per-currency values reconcile while the combined
value does not have a valid unit.

**Expected diagnosis** - Aggregation mixes currencies without an
approved exchange-rate and reporting-currency rule.

**Expected repair** - Prefer separate totals by currency unless an
approved conversion source/rule exists. - If conversion rules are not
defined, flag the ambiguity for human decision.

### DS-14 --- Inconsistent historical backfill

**Setup** - Create daily aggregates for a historical range using
transformation version 1.

**Inject** - Change the revenue transformation but recompute only recent
dates.

**Expected symptoms** - Recent dates use one rule and historical dates
use another. - A trend line changes abruptly at the deployment date.

**Expected diagnosis** - A transformation change was not applied
consistently across the required historical range.

**Expected repair** - Propose a controlled backfill with scope, expected
impact, and idempotent execution. - Validate a sample and compare
before/after historical totals before full application.

### DS-15 --- Stale or stalled source

**Setup** - ETL continues to run on schedule.

**Inject** - Stop the transaction simulator or pause a source feed.

**Expected symptoms** - ETL runs succeed but source event timestamps and
record counts stop advancing. - Dashboard freshness exceeds the
configured objective.

**Expected diagnosis** - The source feed is stalled or stale; this is
not necessarily an ETL transformation defect.

**Expected repair** - Alert and recommend checking the producer/source
connection. - Do not change ETL business logic to compensate for missing
source events.

### Scenario implementation contract

Every scenario should implement a common interface or equivalent
configuration:

``` python
class FaultScenario:
    scenario_id: str
    name: str
    description: str

    def reset(self, context) -> None:
        """Restore baseline data and pipeline configuration."""
        ...

    def inject(self, context) -> None:
        """Introduce one deterministic fault."""
        ...

    def expected_symptoms(self) -> list[str]:
        """Evaluator-only expected observations."""
        ...

    def ground_truth(self) -> dict:
        """Evaluator-only cause and expected repair category."""
        ...
```

Do not expose `ground_truth()` to investigation agents. Agents should
receive only the same evidence a real diagnostic system could retrieve.

Each scenario should also define: - Preconditions and baseline
metrics. - Fault injection operation and rollback/reset operation. -
Expected failing quality checks. - Evidence sources an investigator can
query. - Correct root-cause category. - Required repair behavior. -
Staging validation tests. - Expected final state. - A deterministic
pass/fail evaluator.

### Recommended demo order

1.  **DS-01 Refund logic omitted** --- strongest business story and
    clear silent corruption.
2.  **DS-02 Schema drift** --- easy to understand and visibly breaks a
    job.
3.  **DS-03 Join drops orders** --- demonstrates record-level evidence
    and SQL diagnosis.
4.  **DS-04 Duplicate ingestion** --- demonstrates retry and idempotency
    reasoning if time allows.

Build DS-01 end-to-end before adding scenario breadth. A few fully
working scenarios are more convincing than many incomplete fault
toggles.

------------------------------------------------------------------------

## 7. DataTraceMind Agentic Workflow

### Design principle

Use **one supervisor/master agent coordinating specialized agent
roles**. For a 48-hour build, these roles should be functions/nodes in
one Python application---not separate LLM deployments or microservices.
A graph-based orchestrator such as LangGraph is suitable, but a small
explicit state machine is acceptable if it is easier to debug.

Agents should exchange structured state and evidence, not long free-form
chat histories. Use Pydantic models or equivalent schemas for all
important agent outputs.

Whichever orchestrator is chosen, the whole workflow is traced with
neatlogs (Section 4). The choice changes how: LangGraph needs the
neatlogs LangChain callback handler passed at graph invocation, while an
explicit state machine needs `@neatlogs.span` on the run, each role, and
each tool. Decide this in Phase 0, because tracing that is bolted on
late tends to produce flat, unreadable traces.

### Agent roles

#### 1. Supervisor / Master Agent

Responsibilities: - Receive an alert or user-triggered investigation. -
Create the incident record and assign an incident ID. - Select the
relevant diagnostic plan based on incident type. - Launch appropriate
investigations, concurrently where independent. - Track state and decide
whether more evidence is needed. - Route to root-cause analysis, repair,
validation, approval, or escalation. - Ensure no repair is applied
without explicit approval. - Close the incident only after post-fix
verification.

The supervisor orchestrates; it should not fabricate evidence or bypass
safety checks.

#### 2. Investigator Agent

Can be implemented as one role with two task modes for the MVP:

**Source-data investigation** - Inspect source schema and column
types. - Run allowlisted/read-only SQL queries. - Profile relevant
statuses, nulls, duplicates, and timestamps. - Find sample source IDs
related to the discrepancy. - Compute source-derived metrics using
approved definitions where possible.

**Pipeline investigation** - Inspect ETL code/version and
configuration. - Read run logs, row counts, checkpoint/watermark, and
last-success timestamps. - Compare expected versus actual
extraction/transformation/load counts. - Identify the exact
transformation or pipeline step implicated by evidence.

The supervisor can run these two investigation tasks in parallel.

#### 3. Business Rules / Contract Agent

Responsibilities: - Retrieve the metric contract, schema contract, and
approved invariants. - Explain which rule applies to the incident. -
Identify missing or conflicting definitions. - Escalate ambiguity to the
human rather than inventing a rule.

This may be a retrieval/tool node rather than a separate LLM call for
every incident.

#### 4. Root-Cause Analyst Agent

Responsibilities: - Correlate evidence from source data, pipeline logs,
analytics, and contracts. - Create and rank competing hypotheses. -
Explain which observations support or contradict each hypothesis. -
Request additional targeted diagnostics when confidence is
insufficient. - Produce a root-cause report tied to evidence references.

Example hypotheses for a revenue mismatch: - Refund handling omitted or
incorrect. - Duplicate target rows. - Wrong time window/timezone. - Join
fan-out. - Stale or incomplete source data.

The agent must not treat a plausible explanation as confirmed without
evidence.

#### 5. Repair Agent

Responsibilities: - Propose the smallest change that addresses the
confirmed cause. - Generate a patch, transformation change, SQL change,
or configuration update. - Explain the intended behavior, affected
tables/metrics, risk, and rollback approach. - Generate regression tests
for the incident and nearby edge cases. - Produce a patch diff for
review.

The repair agent does **not** have direct production write access and
cannot approve its own patch.

#### 6. Validation Agent / Validator

Responsibilities: - Apply the proposed patch to an isolated staging copy
or sandbox. - Run the pipeline and regression tests. - Compare source
and target metrics. - Check schema compatibility, row uniqueness,
required fields, and other invariants. - Record before/after values and
validation logs. - Mark the repair as passed only when deterministic
checks pass. - Send failed validation back to investigation/repair or
escalate.

For the MVP, implement validation primarily as deterministic code. An
LLM may summarize results, but it does not decide whether the tests
passed.

#### 7. Notification / Alert Module

This can be a normal application service, not an LLM agent.

Responsibilities: - Create an in-app alert on detection. - Optionally
send a webhook or email if time permits. - Include incident ID,
severity, affected metric, discrepancy, likely cause, and link/deep link
to the incident view. - Avoid including secrets or unnecessary raw
personal data.

### Orchestration graph

``` text
Quality monitor / user trigger
              |
              v
     Supervisor creates incident
              |
              v
    Plan and dispatch investigations
       /          |           \
      v           v            v
 Source-data   Pipeline     Business-rule
 investigator  investigator  lookup
       \          |           /
        \         |          /
         v        v         v
          Root-cause analyst
                  |
          Enough evidence?
            /          \
          No            Yes
          |              |
          v              v
  Targeted investigation  Repair proposal
  / loop or escalation          |
                                v
                       Staging validation
                         /           \
                      Failed        Passed
                        |              |
                        v              v
                Investigate/revise  Human approval
                                       |
                               Approved? ─── No ──> Rejected/cancelled
                                       |
                                      Yes
                                       |
                                       v
                               Apply approved patch
                                       |
                                       v
                           Rerun + post-fix verification
                                  /          \
                               Failed       Passed
                                 |             |
                                 v             v
                           Reopen incident  Close incident
```

### Shared incident state

Use a typed state object with stable identifiers. Example:

``` python
from typing import Any, Literal, TypedDict

class IncidentState(TypedDict, total=False):
    incident_id: str
    scenario_id: str
    status: Literal[
        "detected", "investigating", "diagnosed",
        "repair_proposed", "validating", "awaiting_approval",
        "applying", "verifying", "resolved", "rejected", "escalated"
    ]
    trigger: dict[str, Any]
    source_metrics: dict[str, Any]
    target_metrics: dict[str, Any]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    selected_root_cause: dict[str, Any]
    approved_business_rules: list[dict[str, Any]]
    proposed_patch: dict[str, Any] | None
    validation_result: dict[str, Any] | None
    approval: dict[str, Any] | None
    post_fix_result: dict[str, Any] | None
    errors: list[dict[str, Any]]
    trace_ref: dict[str, Any] | None   # neatlogs trace for this run
```

Recommended evidence object:

``` json
{
  "evidence_id": "E-014",
  "kind": "query_result",
  "source": "postgresql",
  "description": "Successful refunds excluded from the current revenue calculation",
  "query_or_tool": "profile_refunds_by_status",
  "result_summary": {
    "status": "SUCCEEDED",
    "count": 8,
    "amount": 37000
  },
  "observed_at": "ISO-8601 timestamp"
}
```

Use real result values returned by tools. The example amount is
illustrative; do not hardcode it as the expected value for all runs.

### Example incident journey: silent revenue corruption

1.  Quality monitor sees source-derived net revenue differs from
    analytics net revenue.
2.  Supervisor opens an incident and captures baseline metrics.
3.  Source investigator queries payments/refunds and profiles status
    distribution.
4.  Pipeline investigator reads the transformation and finds the current
    refund filter.
5.  Contract lookup retrieves the approved net-revenue definition.
6.  Root-cause analyst links the discrepancy to refund amounts excluded
    by the transformation, and records evidence references.
7.  Repair agent proposes a small transformation patch and tests for
    full and partial refunds.
8.  Validator runs the patch in staging, compares source-derived and
    analytics metrics, and checks regression invariants.
9.  UI shows root cause, evidence, patch diff, validation results, risk,
    and approval controls.
10. Human explicitly approves.
11. Application verifies the approval belongs to this incident and this
    exact patch hash.
12. Repair executor applies the approved patch to the demo environment
    and reruns ETL.
13. Post-fix verification checks reconciliation, freshness, duplicates,
    and regression tests.
14. Incident closes only if verification passes; otherwise it reopens or
    escalates.

------------------------------------------------------------------------

## 8. Detection, Alerting, and Incident Lifecycle

### Detection triggers

Use deterministic monitors first: - Source-to-target metric
reconciliation. - Data-quality invariant failure. - ETL failure or
repeated retries. - Pipeline freshness threshold exceeded. - Row-count
or distinct-ID mismatch. - Unexpected schema change. - Duplicate-key
detection.

A monitor should produce a structured incident trigger. The LLM should
not be required to notice every simple arithmetic mismatch.

### Incident severity

A simple severity model is sufficient:

-   **Critical:** major metric corruption or data unavailable;
    downstream reporting is unsafe.
-   **High:** material mismatch, record loss, or repeated pipeline
    failures.
-   **Medium:** stale data, limited-scope schema issue, or recoverable
    delay.
-   **Low:** warning or early signal with no confirmed impact.

Severity thresholds must be configured and deterministic where possible.

### Alert content

An alert should include: - Incident ID and severity. - Affected
dataset/metric. - Detection time and last successful refresh. - Expected
versus observed value. - Estimated discrepancy or affected-row count,
when known. - Current investigation status. - Link to evidence and the
incident page. - Whether a fix is proposed, validated, or awaiting
approval.

**MVP:** in-app alerts and an incident detail page are required. A
webhook/email/Slack integration is optional.

### Suggested incident lifecycle

`DETECTED → INVESTIGATING → DIAGNOSED → REPAIR_PROPOSED → VALIDATING → AWAITING_APPROVAL → APPLYING → VERIFYING → RESOLVED`

Alternative terminal or exception states: - `REJECTED` - `ESCALATED` -
`VALIDATION_FAILED` - `APPLICATION_FAILED`

Persist transitions and timestamps. The UI must show the current status
and event timeline.

------------------------------------------------------------------------

## 9. Human Approval and Safe Auto-Apply

The product requirement is **human approval before applying a fix**,
followed by automatic application and verification. Do not confuse this
with fully autonomous production changes.

### Required flow

1.  Agent proposes a repair.
2.  Validator runs it in staging.
3.  DataTraceMind presents the diagnosis, evidence, patch diff, risk,
    tests, and before/after results.
4.  Authorized human approves or rejects that exact proposal.
5.  The application verifies the approval and patch identity.
6.  Repair executor applies the approved patch to the demo target.
7.  ETL is rerun.
8.  Post-fix checks run automatically.
9.  The incident is resolved only if verification passes.

### Approval requirements

-   Approval must be an explicit UI action.
-   Approval is bound to a specific incident and immutable
    patch/version/hash.
-   Any change to the patch invalidates prior approval and requires
    revalidation and reapproval.
-   Rejection records a reason if supplied and prevents application.
-   Timeout or missing approval means no application.
-   The agent cannot approve its own patch.
-   The UI must clearly distinguish `proposed`, `validated`, `approved`,
    `applied`, and `verified`.

### Execution guardrails

-   Use separate read-only investigation credentials and narrowly scoped
    repair credentials.
-   Never provide unrestricted production credentials to an LLM.
-   Execute only allowlisted patch types/operations in the hackathon
    environment.
-   Prefer version-controlled transformation files or predefined patch
    templates over arbitrary shell commands.
-   Do not allow the LLM to run unrestricted generated SQL against the
    source or target.
-   Validate generated SQL/code before execution; enforce timeouts and
    resource limits.
-   Use a staging database/schema or a snapshot for validation.
-   Record the patch hash, approver, approval time, execution result,
    and verification outcome.
-   Provide a reset/rollback path for every demo scenario.
-   Do not let a repair alter business rules if the relevant contract is
    missing or ambiguous; escalate for a human decision.

### Important distinction

For the hackathon, "auto-apply" means **automatically apply after
explicit human approval in a disposable demo environment**. It does not
mean autonomous production write access.

------------------------------------------------------------------------

## 10. API and Frontend Scope

### Minimal API surface

Routes may be adjusted to the chosen framework, but keep the API small.

**Demo/data** - `POST /demo/reset` --- restore clean data and pipeline
state. - `POST /demo/traffic/start` --- start transaction generation. -
`POST /demo/traffic/stop` --- stop transaction generation. -
`GET /transactions/live` --- recent source transactions. -
`GET /metrics/summary` --- source/target metrics and freshness. -
`GET /pipeline/runs` --- recent ETL runs and quality results. -
`GET /scenarios` --- list available scenarios. -
`POST /scenarios/{scenario_id}/inject` --- inject a selected fault.

**Incidents** - `GET /incidents` --- list incidents. -
`GET /incidents/{incident_id}` --- incident details, metrics, evidence,
timeline, and repair. - `POST /incidents/{incident_id}/investigate` ---
start/restart investigation. - `GET /incidents/{incident_id}/events` ---
agent and lifecycle events.

**Repair lifecycle** - `POST /incidents/{incident_id}/validate` --- run
staging validation for a proposal. -
`POST /incidents/{incident_id}/approve` --- approve a specific
proposal/hash. - `POST /incidents/{incident_id}/reject` --- reject a
proposal. - `POST /incidents/{incident_id}/apply` --- execute only a
validated, approved proposal. - `POST /incidents/{incident_id}/verify`
--- run or retrieve post-fix verification.

The backend must enforce lifecycle rules; hiding or disabling a button
in React is not a security boundary.

### Frontend pages/components

1.  **Overview dashboard**
    -   Live transaction count and recent events.
    -   Source versus analytics revenue/order metrics.
    -   Pipeline status and freshness.
    -   Active incidents and recent alerts.
2.  **Scenario Lab**
    -   Scenario list with plain-language descriptions.
    -   Reset and inject buttons.
    -   Current scenario status.
    -   Baseline and post-injection metrics.
3.  **Incident detail**
    -   Summary, severity, status, affected metrics.
    -   Agent workflow timeline with running/completed/failed states.
    -   Evidence cards with query/tool, result, and source.
    -   Root-cause hypotheses and confidence/rationale.
    -   Link to the relevant contract or transformation.
4.  **Repair review**
    -   Patch diff.
    -   Why the patch addresses the root cause.
    -   Risk and rollback plan.
    -   Regression test results.
    -   Before/after metric comparison.
    -   Approve and reject controls.
    -   Explicit status showing whether a patch is only proposed,
        validated, approved, applied, or verified.
5.  **Audit timeline**
    -   Detection, tool calls, patch generation, validation, approval,
        application, and final checks.

### UI design principle

Make the system explainable. The most compelling demo is not a chat
window; it is a visible chain from **anomaly → evidence → root cause →
patch → staging proof → human approval → verified recovery**.

------------------------------------------------------------------------

## 11. Tool Design and Agent Boundaries

Expose small, typed tools. The LLM chooses among allowed tools;
application code executes them and returns real results.

Every tool below is traced as a neatlogs tool span (Section 4), so each
call, its arguments, its result summary, its latency, and any failure or
retry is visible in the run's trace. A tool that is callable by an agent
but absent from the trace is a defect.

### Investigation tools

-   `get_source_schema(table_name)`
-   `get_analytics_schema(table_name)`
-   `run_readonly_sql(query_template_id, parameters)`
-   `profile_column(table_name, column_name)`
-   `compare_source_target_metrics(metric_name, time_range)`
-   `find_missing_source_ids(source_table, target_table)`
-   `find_duplicate_business_keys(table_name, key_columns)`
-   `get_etl_code(version_or_path)`
-   `get_etl_run_logs(run_id_or_recent_window)`
-   `get_checkpoint(pipeline_name)`
-   `get_metric_contract(metric_name)`
-   `get_data_contract(dataset_name)`

For the MVP, prefer query templates/allowlisted diagnostic functions
over arbitrary SQL authored by an LLM.

### Repair and validation tools

-   `create_patch(target_file, patch_content, rationale)`
-   `generate_regression_tests(scenario_id, root_cause)`
-   `run_staging_pipeline(patch_id, scenario_id)`
-   `run_quality_checks(run_id)`
-   `compare_before_after_metrics(incident_id, validation_run_id)`
-   `request_human_approval(incident_id, patch_hash)`
-   `apply_approved_patch(incident_id, patch_hash)`
-   `run_post_fix_verification(incident_id)`

The `apply_approved_patch` tool must independently check approval,
validation status, patch hash, and allowed target before applying
anything. The model's statement that a patch is approved is not proof of
approval.

### Evidence rules

-   Every major diagnosis must reference evidence IDs.
-   Evidence must originate from actual tool results, code versions,
    contracts, or logs.
-   Store the query/tool name and a concise result summary.
-   Avoid dumping large tables or secrets into the LLM context.
-   Record when evidence is missing or conflicting.
-   Do not present an unverified hypothesis as a confirmed root cause.

------------------------------------------------------------------------

## 12. Suggested Repository Structure

Use one repository unless the team has a compelling reason not to. It
must be a public GitHub repository connected to Entire (Section 4).

``` text
datatracemind/
├── README.md
├── hackathon.md                    # dated build log + evidence links
├── docker-compose.yml
├── .env.example                    # includes NEATLOGS_API_KEY (name only)
├── .entire/                        # generated by the Entire CLI; commit it
├── docs/
│   └── business-plan/              # cfo.ai plan export / screenshots
├── contracts/
│   ├── revenue.md
│   ├── orders.md
│   └── schemas/
├── infra/
│   ├── postgres/
│   └── clickhouse/                 # optional if using ClickHouse
├── simulator/
│   └── spring-boot-app/
├── etl/
│   ├── pipeline.py
│   ├── transforms/
│   ├── quality_checks.py
│   └── run_log.py
├── datatracemind/
│   ├── api/
│   ├── orchestration/
│   │   ├── supervisor.py
│   │   ├── state.py
│   │   ├── routing.py
│   │   └── workflow.py
│   ├── agents/
│   │   ├── investigator.py
│   │   ├── contract_agent.py
│   │   ├── root_cause_analyst.py
│   │   ├── repair_agent.py
│   │   └── validator.py
│   ├── tools/
│   ├── scenarios/
│   │   ├── base.py
│   │   ├── refund_logic.py
│   │   ├── schema_drift.py
│   │   ├── join_loss.py
│   │   └── duplicate_ingestion.py
│   ├── repair/
│   │   ├── patch_store.py
│   │   ├── approval.py
│   │   └── executor.py
│   ├── monitoring/
│   ├── observability/              # neatlogs init, handler, span helpers
│   ├── audit/
│   └── models/
└── frontend/
    └── React application
```

This is a suggested layout, not a mandate. Avoid spending time creating
many packages with no useful separation.

------------------------------------------------------------------------

## 13. Phased Implementation Plan

The order matters. Build a complete vertical slice before adding many
scenarios or sophisticated orchestration.

### Phase 0 --- Lock scope, success criteria, and mandatory tooling (2--3 hours)

**Tasks** - Confirm the e-commerce use case and revenue contract. -
Choose the analytics target: ClickHouse if setup is quick, otherwise a
separate PostgreSQL analytics schema. - Choose one LLM provider and
implement a small adapter. - Agree on the four required demo steps:
inject, diagnose, validate, approve/apply/verify. - Select DS-01 as the
first end-to-end scenario. - Create the public GitHub repository and
connect it to Entire; install the Entire CLI, run `entire enable`, and
capture a first checkpoint. - Sign up for neatlogs, set
`NEATLOGS_API_KEY`, and pass `python -m neatlogs doctor --probe`. -
Team leader signs up for cfo.ai through the neatHack link. - Choose the
orchestrator (LangGraph or explicit state machine) together with its
neatlogs instrumentation path. - Start `hackathon.md`.

**Deliverables** - This product brief checked into the repository. - A
short architecture diagram. - Metric contract and first quality
invariants. - Working local development plan and `.env.example`. - A
first Entire checkpoint visible on entire.io. - A neatlogs project
receiving the doctor's test trace. - A cfo.ai workspace ready for the
plan.

**Exit criteria** - Team can explain the source of truth, analytics
target, revenue definition, and human-approval boundary in one minute. -
`entire checkpoint list` shows at least one checkpoint and
`entire status` reports it synced. - The neatlogs doctor reports
`TRACE_VISIBLE` and `TRACE_FINALIZED` as `PASS`. - No feature code is
written before these checks pass; work done outside an Entire-enabled
session leaves no checkpoint.

### Phase 1 --- Infrastructure and deterministic data path (4--6 hours)

**Tasks** - Start PostgreSQL and optional ClickHouse with Docker
Compose. - Create schema and seed data. - Implement the Spring Boot
transaction simulator or a lightweight equivalent if the backend time
budget is at risk. - Implement the Python ETL loop and run logging. -
Create analytics tables and deterministic quality checks. - Display
source/target metrics from an API or simple UI.

**Deliverables** - Reproducible local stack. - Live or periodically
generated transactions. - Working baseline ETL. - Run logs and baseline
reconciliation.

**Exit criteria** - With no fault injected, source and analytics metrics
reconcile. - Reset returns the system to the same baseline. - The ETL
can be rerun without accidental duplication under normal conditions.

### Phase 2 --- Scenario lab and detection (3--5 hours)

**Tasks** - Implement a common fault scenario interface. - Implement
DS-01 refund logic fault first. - Implement DS-02 schema drift and DS-03
join loss if time allows. - Add reset/inject controls. - Implement
deterministic monitors and create incident records. - Capture
before/after metrics and expected failed checks.

**Deliverables** - At least three repeatable fault scenarios, with DS-01
fully working. - A visible alert and incident record. - Ground-truth
evaluator kept separate from agent-visible evidence.

**Exit criteria** - Each scenario reliably reproduces its documented
symptom. - The same scenario can be reset and run repeatedly. -
Detection does not rely on an LLM guessing that a problem exists.

### Phase 3 --- Agentic investigation and root cause (6--8 hours)

**Tasks** - Define typed incident state and evidence schemas. -
Implement supervisor and workflow routing. - Implement source and
pipeline investigators. - Implement contract retrieval. - Implement
hypothesis ranking with evidence references. - Add bounded retries and
escalation when evidence is insufficient. - Record agent events for the
UI. - Instrument the workflow with neatlogs as it is built: the wrapped
LLM adapter, a root span per investigation run, and a span per agent
role and per tool. - Save the first complete DS-01 run as the baseline
("before") trace and record its numbers.

**Deliverables** - Working multi-role workflow for DS-01. -
Investigation results grounded in actual query/log/contract results. -
Root-cause report with evidence IDs and confidence/rationale. - A
neatlogs trace of a full DS-01 investigation, linked from
`hackathon.md`.

**Exit criteria** - The agent identifies the known refund logic fault
from observable evidence. - If the required business definition is
missing, it asks for clarification or escalates instead of inventing a
policy. - Agent steps and evidence are visible in the incident
timeline. - One DS-01 investigation appears in neatlogs as a single
trace with nested role and tool spans, token usage, latency, and cost.

### Phase 4 --- Repair, staging validation, approval, and application (5--7 hours)

**Tasks** - Generate a small patch proposal and diff. - Implement a
staging copy/sandbox. - Run regression tests and data-quality checks in
staging. - Build the repair review UI. - Bind approval to the exact
patch hash. - Implement a constrained repair executor. - Require
server-side validation and approval before applying. - Rerun ETL and
perform post-fix checks. - Capture a recovery run: make an action fail
(a patch that fails staging validation, or a tool error) and keep the
neatlogs trace showing what the agent did next.

**Deliverables** - Patch proposal, validation result, approval UI, safe
apply path, audit trail. - Successful recovery of DS-01 after explicit
approval. - A saved neatlogs trace of a failed action and the agent's
recovery.

**Exit criteria** - An unapproved patch cannot be applied through the
API. - A modified patch invalidates the old approval. - A patch that
fails validation cannot be approved for application. - A successful
application is not marked resolved until post-fix checks pass. -
Rejected proposals remain unapplied.

### Phase 5 --- Frontend polish, alerting, and additional scenarios (3--5 hours)

**Tasks** - Complete dashboard, scenario lab, incident detail, repair
review, and audit timeline. - Add in-app alerting and optionally one
webhook. - Add DS-02 and DS-03 end-to-end. - Add one P1 scenario such as
duplicate ingestion if stable. - Improve error handling and demo
reset. - A team member posts at least one progress update in public
tagged `#neatHack` and links it in `hackathon.md`.

**Deliverables** - End-to-end UI flow suitable for a live
presentation. - Three reliable scenarios, preferably four. - Clear alert
and incident status transitions.

**Exit criteria** - A new viewer can understand the failure, diagnosis,
patch, and approval flow without reading source code. - The demo can be
reset and replayed without manual database cleanup.

### Phase 6 --- Hardening, evidence, and submission (4--6 hours)

**Tasks** - Test happy path, rejection path, validation failure, and
application failure. - Confirm no LLM has unrestricted write access. -
Add timeout and retry limits. - Ensure secrets are in environment
variables, not committed. - Capture screenshots or a backup demo
recording. - Prepare a 3--5 minute walkthrough. - Use the baseline
neatlogs trace to find one concrete weakness (a wasted tool call, a
wrong first hypothesis, excess tokens), fix it, rerun the same scenario,
and record the before/after numbers. - Build the cfo.ai business plan
using the measured cost per investigation; export it to
`docs/business-plan/`. - Confirm the Entire checkpoint link opens for
someone outside the team and note its checkpoint ID. - Record the demo
video, covering the agent and all three tools. - Complete the evidence
list in `hackathon.md` and submit on X inside the submission window.

**Deliverables** - Rehearsed demo. - Known limitations documented. -
Backup path if the LLM provider or external network fails. -
Before/after trace pair with compared numbers. - Recovery-run trace. -
cfo.ai business plan. - Entire checkpoint link and ID. - Demo video. -
Submitted entry.

**Exit criteria** - At least one clean end-to-end run and one
rejection/guardrail run succeed reliably. - Every item under "Evidence
the submission must be able to show" in Section 4 has a link or file
behind it.

**Total planned effort:** approximately 27--40 focused hours, leaving
buffer before the submission window closes at 11:55 PM IST on
12 October. If behind schedule, cut scope in this order: external
notification integration, ClickHouse (use PostgreSQL analytics schema),
Spring Boot simulator complexity, extra scenarios, then UI polish. Do
not cut evidence grounding, staging validation, explicit approval, or
post-fix verification. Do not cut neatlogs tracing, Entire checkpoints,
or the cfo.ai plan either: they are entry requirements, not features.

------------------------------------------------------------------------

## 14. MVP Acceptance Criteria

### Functional

-   [ ] The local stack starts from documented instructions.
-   [ ] E-commerce source data is generated and visible.
-   [ ] ETL writes to a separate analytics target.
-   [ ] Baseline metrics reconcile.
-   [ ] At least three fault scenarios can be injected and reset; DS-01
    is complete end-to-end.
-   [ ] DataTraceMind detects a metric mismatch or failed quality check.
-   [ ] Supervisor orchestrates specialized investigation roles.
-   [ ] Root-cause explanation references concrete evidence.
-   [ ] A repair proposal includes a patch/diff, rationale, risk, and
    tests.
-   [ ] Proposed repair is run in staging.
-   [ ] Human can approve or reject the exact proposal.
-   [ ] Approved fix is applied automatically to the demo environment.
-   [ ] Post-fix verification runs automatically.
-   [ ] Incident is resolved only when checks pass.
-   [ ] Timeline/audit records key workflow events.

### Safety and reliability

-   [ ] Investigation tools are read-only.
-   [ ] No fix is applied without server-side approval verification.
-   [ ] Approval is bound to an immutable patch identity.
-   [ ] Validation failure blocks application.
-   [ ] Ambiguous business rules cause escalation rather than guessed
    financial logic.
-   [ ] LLM outputs are schema-validated.
-   [ ] SQL and patch execution are constrained and logged.
-   [ ] Scenario ground truth is not exposed to the investigating
    agents.
-   [ ] Reset and rollback are documented and tested.

### Demo quality

-   [ ] The audience can see a healthy baseline.
-   [ ] A silent data-quality failure can be triggered on demand.
-   [ ] The dashboard shows the discrepancy.
-   [ ] The investigation shows real evidence and a plausible root
    cause.
-   [ ] The repair diff and staging results are visible.
-   [ ] Approval is explicit and visible.
-   [ ] The final metrics recover and the incident closes.

### Hackathon compliance (mandatory tools)

-   [ ] Every model call and every agent tool call in an investigation
    is captured in a neatlogs trace.
-   [ ] A full DS-01 run is visible in neatlogs as one trace with nested
    role and tool spans, token usage, latency, and cost.
-   [ ] A before/after pair of runs exists, with the numbers compared.
-   [ ] A trace exists of a failed action and the agent's recovery.
-   [ ] The repository is public on GitHub, with commits dated during
    the hackathon.
-   [ ] Entire is enabled on the repository, and an accessible
    checkpoint link and checkpoint ID are recorded.
-   [ ] The cfo.ai business plan covers pricing, costs, revenue, and
    runway and is included in the submission.
-   [ ] The demo video explains the agent and how neatlogs, Entire, and
    cfo.ai were used.
-   [ ] Public posts tagged `#neatHack` are linked in `hackathon.md`.
-   [ ] No API key or other secret appears in the repository, in a
    trace, or in a coding-agent session.

------------------------------------------------------------------------

## 15. Evaluation Plan

Evaluate DataTraceMind against the hidden scenario ground truth and
deterministic checks---not only whether the final LLM explanation sounds
convincing.

For each scenario, record: - Detection success. - Root-cause category
accuracy. - Evidence coverage: did the diagnosis cite the key
evidence? - Repair validity. - Staging test pass/fail. - Regression
check pass/fail. - Approval enforcement. - Post-fix recovery. - Time to
diagnosis and total resolution time.

A simple score can weight correctness and safety more heavily than
speed. For the hackathon, a defensible repair that escalates an
ambiguous issue is better than a confident but unsafe guess.

### Before/after improvement evidence

The hackathon asks for proof that the agent improved: a before-and-after
pair of runs with the numbers compared. Produce it deliberately rather
than hoping two runs happen to differ.

1.  Run DS-01 from a clean reset and save the neatlogs trace as the
    baseline.
2.  Read the trace and pick one concrete weakness.
3.  Change one thing (a prompt, a tool, a routing rule) and note the
    workflow version label on the trace.
4.  Rerun DS-01 from the same reset and seed, and save the second trace.
5.  Record both runs side by side in `hackathon.md`.

Compare numbers that neatlogs reports directly, alongside the
evaluator's verdict: total tokens, cost, end-to-end latency, number of
model calls, number of tool calls, retries and failed calls, and whether
the root-cause category and the repair were correct. An improvement in
cost or speed does not count if correctness or a safety check regressed.

------------------------------------------------------------------------

## 16. Coding-Agent Instructions

Coding agents working on this repository should follow these rules:

1.  Read this document before implementing a feature.
2.  Work in small, testable increments; keep the app runnable.
3.  Implement DS-01 as the first vertical slice before building many
    scenario types.
4.  Keep the transactional source and analytics target logically
    separate.
5.  Keep scenario ground truth separate from agent-visible evidence.
6.  Use typed schemas for incident state, evidence, hypotheses, repair
    proposals, validation results, and approvals.
7.  Keep diagnostic checks deterministic and make them available as
    tools.
8.  Use real query/tool results; do not hardcode a diagnosis or evidence
    payload to make the demo appear intelligent.
9.  Do not grant LLM agents direct, unrestricted database writes or
    shell execution.
10. A repair is not applicable until it has passed staging validation
    and explicit human approval.
11. Bind approval to the exact patch hash; require reapproval if the
    patch changes.
12. Do not mark an incident resolved until post-fix verification passes.
13. Make scenarios deterministic, resettable, and easy to demo.
14. Add tests for both successful and blocked/unsafe paths.
15. Avoid overengineering: no extra microservices, queues, vector
    database, or orchestration infrastructure unless a concrete MVP need
    appears. neatlogs, Entire, and cfo.ai are not "extra"; they are
    required by the hackathon rules (Section 4).
16. Document any deviation from this plan and the reason for it.
17. Route every model call through the neatlogs-wrapped LLM adapter and
    give every agent role and tool a neatlogs span. Do not add a model
    call or tool that bypasses tracing.
18. After changing agent behaviour, run the scenario and confirm the
    trace in neatlogs before calling the task done. Use the trace, not
    a guess, to explain a failure.
19. Work only inside an Entire-enabled session, and commit in small,
    feature-sized steps so that each has a checkpoint.
20. Never put API keys in code, commits, prompts, or trace payloads.
    Read `NEATLOGS_API_KEY` and provider keys from the environment.
21. Do not attempt the human-only steps listed in Section 4 (account
    sign-up, card entry, device-login approval, public posting). Stop
    and ask.
22. Append a dated entry to `hackathon.md` at the end of each working
    session.

### Definition of done for any implementation task

-   Code is committed in the expected module.
-   Relevant tests pass.
-   Errors are handled and logged.
-   UI status matches backend state.
-   No hidden shortcut bypasses validation or approval.
-   README/setup notes are updated when needed.

------------------------------------------------------------------------

## 17. Demo Script (3--5 Minutes)

1.  **Show healthy baseline:** transactions are flowing, ETL is green,
    and source/analytics revenue reconciles.
2.  **Inject DS-01:** trigger the refund logic fault from Scenario Lab.
3.  **Show detection:** the ETL still reports success, but the revenue
    invariant fails and DataTraceMind raises an alert.
4.  **Show investigation:** source and pipeline investigators collect
    evidence; the contract defines how refunds affect net revenue.
5.  **Show diagnosis:** DataTraceMind explains the faulty rule and cites
    the relevant refund IDs, query results, and transformation code.
6.  **Show proposed repair:** display the patch diff, rationale, risk,
    and regression tests.
7.  **Show staging proof:** the validator runs the patch and reports the
    before/after reconciliation.
8.  **Approve:** a human explicitly approves the exact patch.
9.  **Show automatic application:** DataTraceMind applies it in the demo
    environment, reruns ETL, and verifies recovery.
10. **Close incident:** show the audit timeline and final healthy
    metrics.
11. **Show the neatlogs trace:** open the trace of the run just shown
    --- roles, tool calls, tokens, cost --- then the before/after pair
    and the recovery run.
12. **Show the Entire checkpoint:** open a commit's checkpoint and show
    Sessions, then Changes.
13. **Show the cfo.ai plan:** pricing, cost per investigation, revenue
    scenarios, and runway.

Steps 11--13 are not optional extras. The demo video is judged on how
clearly it explains the agent and the three tools, so budget roughly a
minute for them and keep steps 1--10 tight.

If time allows, replay the demo with schema drift to demonstrate that
the same generic workflow can handle a different class of failure.

------------------------------------------------------------------------

## 18. Risks and Mitigations

  -----------------------------------------------------------------------
  Risk                                Mitigation
  ----------------------------------- -----------------------------------
  Too much infrastructure work        Start with Docker Compose and one
                                      app per language; use PostgreSQL
                                      analytics fallback

  LLM diagnosis is plausible but      Require evidence IDs, competing
  wrong                               hypotheses, deterministic checks,
                                      and staging validation

  Financial semantics are ambiguous   Store explicit contracts and
                                      escalate when rules are missing

  Generated SQL/code is unsafe        Use allowlisted tools, constrained
                                      patches, sandbox execution, and
                                      timeouts

  Demo is nondeterministic            Seed data, fixed scenario fixtures,
                                      reset controls, and deterministic
                                      evaluator

  Repair changes after approval       Bind approval to patch hash and
                                      invalidate approval on change

  LLM provider/network fails during   Keep deterministic
  demo                                monitoring/validation working and
                                      prepare a backup recording

  Too many scenarios remain           Prioritize DS-01, DS-02, and DS-03;
  unfinished                          add breadth only after one full
                                      workflow works

  Mandatory-tool evidence is missing  Set up all three tools in Phase 0;
  at submission                       collect links in `hackathon.md` as
                                      they are produced, not at the end

  Agent runs produce no neatlogs      Pass the doctor probe first; with
  trace, or a flat one                LangGraph, pass the callback
                                      handler at graph invocation; flush
                                      before scripts exit

  Work is done outside an Entire      Enable Entire before the first
  session and leaves no checkpoint    feature commit; verify with
                                      `entire checkpoint list`

  Entire repository sync is slow or   Connect the repository at the
  blocked on a GitHub org admin       start; sync can take up to an hour

  cfo.ai sign-up leaves an active     Team leader cancels in Settings →
  paid subscription                   Billing immediately after sign-up

  A secret leaks through the public   Keys only in environment
  repository, a trace, or a public    variables; summaries, not raw
  checkpoint                          payloads, in spans and sessions
  -----------------------------------------------------------------------

------------------------------------------------------------------------

## 19. Final Product Definition

DataTraceMind is successful as a hackathon MVP if it proves one complete,
trustworthy loop:

**Detect a real, reproducible data issue → investigate with multiple
coordinated agent roles → explain the root cause with evidence → propose
a minimal fix → validate in staging → obtain human approval → apply the
approved fix automatically → verify and audit recovery.**

The goal is not to build the largest agent framework. The goal is to
show that agentic AI can make data-pipeline troubleshooting faster while
keeping changes observable, testable, and under human control.
