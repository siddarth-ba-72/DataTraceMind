# neatHack build log — DataTraceMind

Running log of what was built, what was actually tested, what failed, and
where the evidence is. Newest session first. Times are IST.

## Evidence checklist

| Required evidence | Status | Where |
|---|---|---|
| Code: public repo, commits dated in the hackathon | Commits made from 13:10 on 10 Oct on branch `build/ds01-vertical-slice`; **not pushed yet** | `git log` |
| neatlogs: traces from the project | Two full DS-01 investigations exported; **not yet opened in the dashboard by a person** | Trace IDs `d931c993a7f032e18558ce61ea460904` (v1), `b4b2e5507d8a139ae2711464076cb11b` (v2) |
| Entire: checkpoint link and ID | Every commit on the branch carries an `Entire-Checkpoint` trailer; links work after the branch is pushed | Table under Session 1 |
| The agent improved: before/after pair | Two runs recorded (v1, v2). **The difference is small and within what one run each can show; not yet a convincing improvement** | "Before/after" under Session 1; `evaluation/results/` |
| Recovery from failure | Covered by tests with a scripted provider; **no traced real run yet** | `tests/test_workflow.py` |
| cfo.ai business plan | Not started. Measured cost per investigation is exported and ready to upload | `business-plan/investigation_costs.csv` |
| Building in public: `#neatHack` posts | None yet | – |
| Demo video | Not recorded | – |

## Session 2 — 10 October 2026, about 18:35–19:55

Coding agent: Claude Code (Opus 5.5). Nothing from this session is committed
yet; the changes are in the working tree on `main`.

### What was built

- **The Scenario Lab is its own page** (`/lab/`), separate from the incident
  console (`/`). Traffic, fault injection, and reset moved to the lab; the
  console keeps data trust, incidents, repair review, and the audit log, and
  links to the lab.
- **Every scenario is explained in the lab** before it is injected: what the
  lab changes, what you will see, which checks should fail, why it matters,
  and how the incident should end. After injecting, a report compares the
  checks that failed with the ones expected.
- **Six more scenarios**, nine in total: DS-04 duplicate ingestion on retry,
  DS-06 new status value, DS-07 timezone boundary, DS-09 nullable amounts,
  DS-16 partial refunds over-subtracted, DS-17 failed payments counted. DS-06
  and DS-09 are meant to end in an escalation, not a patch.
- **Live traffic reworked**: a rate (light, steady, busy), counters of what
  the stream wrote, a one-off batch, and a status route. The start and stop
  routes are unchanged.
- **Staging validation replays the load.** A fresh staging load is clean with
  any load mode, so it could not tell an append-only patch from a correct one.
- **Monitor**: duplicate business keys now classify an incident ahead of the
  revenue mismatch they cause.
- **Console redesign**: sidebar and top bar, incident list and detail pages,
  KPI tiles, a lifecycle stepper, light and dark themes on one set of tokens.

### What was tested

- `pytest`: **130 passed**. New tests cover each new scenario's documented
  symptom, the two escalation endings, the staging replay rejecting a patch
  that leaves the load append-only, the traffic routes, and that no module on
  the agent's side imports the scenario package.
- Both pages rendered in headless Chrome against a second instance on a
  scratch database: baseline, DS-04 injected, awaiting approval, resolved,
  DS-06 escalated, traffic running; light and dark; 1440 and 820 pixels wide.
  The investigations in those checks used the scripted provider.
- **One real DS-01 run, end to end, through the browser** (Claude in Chrome
  on the main instance, at the user's request): reset in the lab, live
  traffic started, rate changed, a batch sent and traffic paused, DS-01
  injected, the lab's link followed to the console, **Investigate**, the
  repair approved, and the lab reset again. No console errors on any page.

  | Measure | Value |
  |---|---|
  | Outcome | `awaiting_approval`, then approved → applied → verified → `resolved` |
  | Root-cause category | `refund_logic`, 0.99 confidence — correct |
  | Repair | `refund_statuses: [COMPLETED]` → `[SUCCEEDED]`, one line — correct |
  | Staging validation | passed: 10 of 10 regression cases, 12 of 12 quality checks, replay of 882 rows clean |
  | Post-fix verification | ETL run 48 `SUCCESS` on transformation version 3, 12 of 12 checks |
  | Detected → resolved | 3 min 4 s (investigation 86.7 s) |
  | Role turns / tool calls | 4 / 10, none failed, no retries |
  | Tokens | 286,565 input (204,288 cached), 3,783 output |
  | Estimated cost | $0.24 at API list prices |
  | Trace ID | `e39a117e7660a88efce0d2a85a42e5ec` |

  The approval was entered by the coding agent under the name
  "Claude E2E test", as part of the test the user asked for. It exercises the
  approve, apply, and verify path with a real model's patch; it is not a
  person's review of that patch. The environment was reset afterwards; the
  incident record and database dumps from before and after the run are in
  `docs/backups/` (git-ignored).

### Not tested

- **The new scenarios with the real model.** Only DS-01 was run with Codex.
  None of the six new scenarios (nor DS-02 and DS-03) has been diagnosed by
  it, so their briefings describe the intended ending, not an observed one.
- In the browser, only the DS-01 path and the traffic controls were clicked
  through. The other scenarios' injected, escalated, and resolved pages were
  checked from screenshots of states set up through the API.
- The trace above was not opened in neatlogs.
- The recorded demo video shows the earlier single-page dashboard.

### What failed or surprised, and what was done

- DS-04 could not be injected in one step: deploying a new transformation
  version forces a full rebuild, which is clean with any load mode. Scenarios
  can now take a second step after the first run; the lab performs both runs.
- For the same reason staging validation passed a patch that left
  `load.mode: append`. The replay step was added and a test pins it.
- A new traffic test compared two counts with the stream still running and
  failed once. It now compares against the count returned when traffic stops.
- The first look at the lab on the main instance failed: the frontend had
  been rebuilt but the server process still held the old backend, so the page
  hit a missing route and then crashed on a missing field. The server was
  restarted, and the lab now says the server is out of date instead of
  failing when the API lacks what it needs.
- That instance had an incident open with no scenario recorded. Injecting on
  top of it would have opened nothing, because the monitor keeps one incident
  open at a time. The lab now reports that state and blocks injection until a
  reset.

## Session 1 — 10 October 2026, 12:50–14:25

Coding agent: Claude Code (Opus 5.5), inside an Entire-enabled session.

### What was built

Phases 1 to 4 of the product brief, and a first pass at the Phase 5 dashboard.

- **Data path.** PostgreSQL with an `oltp` source schema and an `analytics`
  target schema; a seeded Python simulator; an ETL that extracts from one
  repeatable-read snapshot, transforms through a versioned transformation
  file, and loads idempotently; twelve deterministic quality checks derived
  from the contracts.
- **Scenario lab and detection.** DS-01 (refund logic), DS-02 (schema drift),
  DS-03 (join loss). The monitor opens one incident per unresolved problem
  with a severity from fixed thresholds.
- **Agent workflow.** Twelve read-only investigation tools behind an MCP
  server; a Codex adapter that fails a turn unless its required tool calls
  succeeded; a supervisor state machine with bounded retries and escalation.
- **Repair lifecycle.** Hashed, immutable proposals; staging validation;
  approval bound to the patch hash; a guarded executor; post-fix verification
  with automatic rollback.
- **Dashboard.** Overview, scenario lab, incident page with evidence and
  repair review, audit log.
- **Evaluator.** `python -m datatracemind.evaluation DS-01` scores a real run
  against the scenario's answer key.

### What was tested

- `pytest`: **100 passed** in about 65 s. Uses a separate test database and a
  scripted stand-in provider, so it spends no model usage. Covers DS-01,
  DS-02, and DS-03 end to end, and the blocked paths: apply without approval,
  a patch changed after approval, a patch that failed validation, rejection,
  an agent role name used as approver, and a failed post-fix verification.
- **One real DS-01 investigation** with Codex (`gpt-6-sol`, medium effort),
  workflow version `v1`, from a clean reset:

  | Measure | Value |
  |---|---|
  | Outcome | `awaiting_approval` (stopped at the human decision, as designed) |
  | Root-cause category | `refund_logic` — correct |
  | Repair | `refund_statuses: [COMPLETED]` → `[SUCCEEDED]`, one line — correct |
  | Staging validation | passed: 10 of 10 regression cases, 12 of 12 quality checks |
  | Apply before approval | refused |
  | Duration | 70.1 s (detected → diagnosed: 46.9 s) |
  | Role turns / tool calls | 4 / 11, none failed, no retries |
  | Tokens | 219,595 input (112,384 cached), 3,874 output |
  | Estimated cost | $0.28 at API list prices (the plan is flat-rate; nothing was charged) |

- **Dashboard** opened in Chrome against that incident: overview, chart
  tooltip, incident page, repair review, and audit log render with no console
  errors. Checked in dark mode only.

### Not tested

- The approval → apply → verify path with a **real model's** patch. The
  incident above was left at `awaiting_approval`: approving is a human
  decision. The same path passes in tests with scripted patches.
- DS-02 and DS-03 with the real model.
- The neatlogs trace in the dashboard. The exporter reported no failures, but
  reading a trace back needs `neatlogs auth login --device`, a human step.
  Nesting was checked locally without export: role spans on worker threads
  share the run's trace ID.
- Light mode, and narrow screens.
- Running in a container or on Linux.

### What failed or surprised, and what was done

- The first schema-drift test passed when it should have failed: an
  incremental run with no changed rows never queried the renamed column.
  The extraction queries now run on every pass.
- `mcp` 2.x hides the text of any exception except its own `ToolError`. Tool
  refusals ("column does not exist; columns are …") are now raised as that
  type so the model can correct its call; other exception text stays hidden.
- Codex reports a tool-level error with `error: null` and the message in the
  result text, not in `error.message` as the provider notes assumed. The
  adapter now reads both.
- `mcp_servers.dtm.env_vars` forwards named environment variables to the tool
  server (verified: the server used the database named in the forwarded
  variable). This avoids putting a connection string on the command line.
- The pipeline lock was per process, so the API's ETL worker and a script
  such as the evaluator could interleave loads on one database. It now also
  takes a PostgreSQL advisory lock. Found by reasoning before the second
  evaluation run, not by a failure; a test covers it.

### Weaknesses seen in the baseline run (candidates for the before/after change)

1. The pipeline investigator read `etl/pipeline.py` and the interpreter source
   in addition to the transformation file: 6 tool calls and 86,868 input
   tokens, the most expensive turn, for an incident the transformation file
   and its history already explain.
2. The analyst cited ten of twelve evidence items as support instead of the
   few that carry the argument, and widened the implicated component to
   include `etl/pipeline.py`.
3. The analyst reported 100% confidence.

### Before/after: workflow v1 → v2

One change, taken from weakness 1 above: the pipeline investigator's prompt
now says to read pipeline source code only when the transformation file and
its history do not explain the failed checks. Same scenario, same reset and
seed, same model and effort. One run per version.

| Measure | v1 (before) | v2 (after) |
|---|---|---|
| Root-cause category correct | yes | yes |
| Repair correct, staging validation passed | yes | yes |
| Apply before approval refused | yes | yes |
| Role turns | 4 | 4 |
| Tool calls (failed / retries) | 11 (0 / 0) | 10 (0 / 0) |
| Input tokens, total | 219,595 | 230,636 |
| Input tokens, uncached | 107,211 | 98,028 |
| Output tokens | 3,874 | 3,951 |
| Estimated cost, API list prices | $0.2756 | $0.2621 |
| Duration | 70.1 s | 75.1 s |
| Evidence items cited as support | 10 | 9 |
| neatlogs trace ID | `d931c993a7f032e18558ce61ea460904` | `b4b2e5507d8a139ae2711464076cb11b` |

**Reading it honestly:** correctness and the safety checks did not regress,
and the pipeline investigator dropped one read it did not need (the
interpreter source). But it still read `etl/pipeline.py`, total input tokens
and duration went up, and the estimated cost fell by under 5%. With one run
per version that is within run-to-run variation. This pair does not show a
clear improvement and should not be presented as one.

Why the change underdelivered: the investigator reads `etl/pipeline.py` to
explain the failed `declared_status_consistent` check (why the run said
SUCCESS), which the transformation file cannot explain, so the new
instruction did not apply to it. Most of the token count is Codex's fixed
overhead per model round trip, so the number of round trips matters more than
which file is read.

Better candidates for the next change:

1. Present `declared_status_consistent` to the roles as context ("the run
   reported SUCCESS although checks failed") rather than as a failed check to
   explain.
2. Give each investigator one composite tool that returns its standard
   starting evidence in a single call, cutting round trips.

Run each version more than once before comparing.

### Commits and Entire checkpoints

Link format once pushed:
`https://entire.io/gh/siddarth-ba-72/DataTraceMind/commit/<commit>`

| Commit | Subject | Checkpoint ID |
|---|---|---|
| `1586fce5` | Track Entire configuration | `01M4JC31XBG5KVAZJ40KW03ZAZ` |
| `c6a7714e` | Add source schema, simulator, ETL, and quality checks | `01M4JC34TY7PVQZMZ4RSR6R930` |
| `eb080a60` | Add scenario lab, detection, incident store, and API | `01M4JC373DVME39XHCGK4Q5N9M` |
| `3b17cf74` | Add read-only investigation tools, MCP tool server, and Codex adapter | `01M4JD770W9AZS179A1XKV4WCY` |
| `aa18140d` | Add supervisor workflow and the repair lifecycle | `01M4JD79M2NQEF9Y9MYB1GYCWX` |
| `db2e9502` | Add scenario evaluator and record the first real DS-01 run | `01M4JDHC7V0EP4R9GJA1G5FA37` |
| `e99cac2` | Add the dashboard, cost export, and clearer incident summaries | `01M4JEMHSZXEYRFTBE69X146GZ` |

The commit that adds this log has a checkpoint too; it is not listed because
its ID exists only after the commit is made (`git log` shows it).

### Decisions made this session

- Analytics target: a PostgreSQL schema (the brief's fallback to ClickHouse).
- Simulator: Python, behind the same `oltp` schema a Spring Boot one would use.
- The transformation is a declarative, versioned file. A repair is a
  schema-checked change to that file, never generated code or SQL.
- Investigations start from a button by default (`DTM_AUTO_INVESTIGATE=1`
  starts them on detection), to conserve the shared Codex allowance.

### Next

1. Human: push the branch; open the trace in neatlogs; approve INC-0001 in the
   dashboard to exercise apply and verify with a real patch.
2. Make one of the two changes listed under "Before/after", bump the workflow
   version to `v3`, and compare over several runs per version.
3. Capture a traced recovery run.
4. Run DS-02 and DS-03 with the real model.
5. Container image and deployment; cfo.ai plan; demo video.
