# neatHack build log — DataTraceMind

Running log of what was built, what was actually tested, what failed, and
where the evidence is. Newest session first. Times are IST.

## Evidence checklist

| Required evidence | Status | Where |
|---|---|---|
| Code: public repo, commits dated in the hackathon | Commits made from 13:10 on 10 Oct on branch `build/ds01-vertical-slice`; **not pushed yet** | `git log` |
| neatlogs: traces from the project | One full DS-01 investigation exported; **not yet opened in the dashboard by a person** | Trace ID `d931c993a7f032e18558ce61ea460904` |
| Entire: checkpoint link and ID | Every commit on the branch carries an `Entire-Checkpoint` trailer; links work after the branch is pushed | Table under Session 1 |
| The agent improved: before/after pair | Baseline ("before") run recorded; **"after" run not done yet** | `evaluation/results/20261010-133338-DS-01-v1.json` |
| Recovery from failure | Covered by tests with a scripted provider; **no traced real run yet** | `tests/test_workflow.py` |
| cfo.ai business plan | Not started. Measured cost per investigation is exported and ready to upload | `business-plan/investigation_costs.csv` |
| Building in public: `#neatHack` posts | None yet | – |
| Demo video | Not recorded | – |

## Session 1 — 10 October 2026, 12:50–14:10

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

- `pytest`: **99 passed** in 65 s. Uses a separate test database and a
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

### Weaknesses seen in the baseline run (candidates for the before/after change)

1. The pipeline investigator read `etl/pipeline.py` and the interpreter source
   in addition to the transformation file: 6 tool calls and 86,868 input
   tokens, the most expensive turn, for an incident the transformation file
   and its history already explain.
2. The analyst cited ten of twelve evidence items as support instead of the
   few that carry the argument, and widened the implicated component to
   include `etl/pipeline.py`.
3. The analyst reported 100% confidence.

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
2. Change one thing from the weaknesses above, bump the workflow version to
   `v2`, rerun DS-01, and record the before/after numbers.
3. Capture a traced recovery run.
4. Run DS-02 and DS-03 with the real model.
5. Container image and deployment; cfo.ai plan; demo video.
