# Release 0031 execution gate validation

This bounded patch is a direct child of approved candidate
`32763bd1c33a9d70c948c7bf3187a69fc239ccce`. That old SHA is superseded.
The new candidate is the commit containing this record; the final handoff supplies
its full SHA. No dependencies, runtime pins, migration files or migration head
changed. Recovery remains `f66c988d2bad8ffaec1af9d4a8e81dc3bb3b0e66`.

## Entry paths and boundaries

A fresh read-only Render inspection confirmed production and validation cron both
use `PYTHONPATH=. python scripts/run_schedule_automation.py`; both remain suspended,
with auto-deploy off and the execution variable absent. No Render state was edited.
The Python script calls `main()`, selects an active Admin/Manager actor, invokes
`run_schedule_automation`, and commits the caller-owned transaction. The HTTP
`POST /v2/scheduling/api/automation/run` route also invokes that service. Repository
search found no other production callers of the automation tick.

The new script checks process permission before importing settings, schema or DB
modules. When explicitly enabled it verifies schema, then follows the existing
actor/transaction path. The service checks permission before advisory locking or
any SQL; direct service calls and the manual HTTP tick therefore cannot bypass it.
Manual schedule editing/generation remains governed by its existing authorization
and business rules; it is not an automation tick. Arbitrary operator SQL or modified
code is outside this application execution control.

The gate reads process environment per invocation. Only trimmed, case-insensitive
`true`, `1`, `yes`, `on` enable work. Every other value disables it. Disabled cron
returns 0 and prints `SCHEDULE_AUTOMATION_DISABLED`. Disabled service/API results
have `ok=false`, `status=disabled`, `execution_enabled=false`, and no generated,
published or blocked IDs. It does not misrepresent skipped work as successful work.
No SQL means no schedule, fairness, attendance or coverage mutation can occur in
that service call. The common guard does not alter enabled scheduling logic.

## Deterministic proof

- 30 disabled subprocess cases cover exact Render script, Python module, and
  release shell wrapper with absent, empty, false, zero, no, off, invalid, truthy,
  two, and uppercase false values.
- Direct-script/module tests install an import sentinel which fails on attempts
  to import settings, schema, DB, automation service, SQLAlchemy or psycopg. The
  shell wrapper uses deliberately unparseable DB configuration. All exit 0 with
  explicit disabled/no-work results.
- 18 enabled subprocess cases cover six accepted spellings across the three
  entry paths. All reach schema validation, and the deliberately unreachable
  database fails there before automation. None claims disabled execution.
- 10 shared-boundary cases use a DB sentinel that rejects all DB operations;
  direct service and the actual HTTP handler both return the disabled result.
  The handler's result merging cannot replace `ok=false` with `ok=true`.
- The existing real PostgreSQL generation/publication/hold/retry test now
  explicitly opts in with `true`; all prior assertions are retained, proving
  normal generation/publication and business constraints still work when enabled.
- Recovery's unchanged exact script was executed with absent, false and true
  values against an unreachable DB: all exit 0 with `RECOVERY_DISABLED`. Calling
  recovery's service with true rejects immediately. Its permanent protections
  are independent of service suspension or environment gate, so no recovery
  artifact change or new full recovery rehearsal is required.
- The documented baseline-compatible transition command was verified locally
  for seven disabled values with invalid database configuration. Production
  environment verification remains a mandatory later release gate.

## Scope and validation record

The source change touches `app/services/scheduling_execution_control.py`,
`app/services/v2_scheduling_policy_service.py`, `scripts/run_schedule_automation.py`,
`scripts/release/cron.sh`, `tests/test_release_entrypoints.py`, and
`tests/test_v2_scheduling_foundation.py`. The runbook, this validation record and
its JSON evidence are the only documentation additions/changes.

Final validation: **58 focused gate tests**, **17 lifecycle tests**, **444 affected
scheduling tests**, and **1223 full-suite tests plus 7 subtests passed**. The sole
skip is optional real private-R2 integration. `pip check` passed. Three existing
deprecation warnings remain. Detailed validation totals are recorded in
[evidence/execution-gate-0031-validation.json](evidence/execution-gate-0031-validation.json).
All checks use the approved fresh Python 3.10.13 hash-locked release environment.

No push, deployment, migration, resume, auto-deploy change, credential creation or
production mutation occurred. The next authorized deployment must establish the
false execution setting **and** the enforcing command on the old baseline, verify
disabled execution in the actual service environment, and then rerun every release
gate. This local patch does not claim the live baseline already gained protection.
