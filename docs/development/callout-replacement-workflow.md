# Call-out and replacement workflow

Canonical base: `a80faccd222f7a9d71c86e43eb34812af0b24690` on
`codex/admin-v2-canonical-integration`. Local development only.

## Authoritative facts

| Fact | Representation |
| --- | --- |
| First published employee | `published_provenance`: same-week revision ancestry, reversed completed transfers after publication, and publication-audit fallback; broken/inconsistent chains explicitly unknown |
| Effective assignment | `ScheduleShift.employee_id`; genuine prospective transfers may change it |
| Absence | `ScheduleAttendanceEvent` CALLED_OUT / NO_CALL_NO_SHOW; actor, occurrence time, recording time and corrections retained |
| Replacement agreement | `ScheduleCoverageCommitment`; one active commitment per shift, prior canceled/superseded records retained |
| Actual worker | Shared attendance resolver; COVERED_SHIFT is an assertion of actual coverage, never created by a commitment |
| Scheduled Lead | Published `is_lead_of_day` designation |
| Current company-wide Lead | Scheduled Lead without contradictory evidence, or today's explicit LeadDutyOutcome; absence exposes reassignment needed |
| Completed Lead credit | Same LeadDutyOutcome becomes CONFIRMED after the business day; CURRENT is never completed credit |
| Longview credit | Existing weekly attendance/presumptive resolver; commitments never create credit |

Copies from another week begin a new publication lineage. Corrections never
rewrite the published assignment. A commitment differing from actual coverage is
preserved rather than silently corrected to match reality.

## Operational workflow

Normal shifts and normal Lead days: no entry.

Advance call-out: open Attendance, default Called out, save once. The board shows
Coverage needed immediately. Add an agreed replacement in the same dialog and
save once to create separate absence and commitment records atomically.

When the outcome is known: one dialog can record the call-out plus actual worker.
When an absence is already recorded, the dialog selects replacement-only entry
rather than asking for a duplicate absence. Actual coverage is unavailable before
the shift finishes. NCNS and lateness cannot be asserted before the shift starts.
The service layer enforces these boundaries independently of the browser and rejects future occurrence/report timestamps, even on past shifts.

Replacement changes supersede the previous commitment; cancellation requires a
reason. Both retain actor/time/reason history. Commitment selection reuses server
assignment evaluation, including other commitments, and does not offer an
employee-side acceptance flow. Above-threshold commitments require an
Admin/Manager note. This is not a candidate ranking engine.

Today’s scheduled Lead call-out removes their operational presumption. An
Admin/Manager can record the eligible current Lead in one separate save. This
names one person for the entire company, independent of work replacement or store.
The same record supplies one completed responsibility-day after today passes.
Historical Lead corrections retain the existing ability to name an inactive person.

Historical actual coverage accepts inactive or no-longer-schedulable employees
and warns about planned overlaps. Real identity, employment-date boundaries,
Never-store override reasons, and contradictory actual-work evidence remain
validated. Two incompatible explicit work assertions remain unresolved until
corrected; they are not discarded to fit planned assignments.

Transfers revalidate effective revision, date, assignment, eligibility and
exception evidence under locks at acceptance and approval/completion. Passed,
archived, superseded or exception-bearing shifts cannot be rewritten by stale
requests. Existing Lead restrictions and schedule warning rebuilding remain.

## Schema and safety

One narrow table, `schedule_coverage_commitments`; no new attendance, employee,
schedule-assignment, notification, or general event system.

Migration graph:

```
20260930_0029 -> 20260930_0030 -> 20260930_0031 (only head)
```

Empty downgrade is supported; any retained commitment prevents destructive
downgrade. The strict runtime schema gate advances to 0031. Only disposable local
PostgreSQL databases are migrated during validation.

No automatic discipline, employee self-service, external notifications, Square
writes, deployment, Render access, production migration or production data changes.

## Acceptance evidence

Disposable localhost preview, four synthetic stores and synthetic employees:

- Tomorrow's Parker call-out immediately showed Coverage needed.
- Cameron's commitment showed Parker as original, Cameron as committed, and
  actual coverage not established.
- Changing commitment to Lexi retained Cameron's superseded record.
- Historical combined call-out/actual coverage saved separate events and displayed
  Cameron as committed while Lexi was actual worker.
- Today's Parker call-out showed company-wide Lead unresolved/reassignment needed;
  recording Mikey immediately displayed the current handoff.
- Historical actual coverage by inactive Former Lexi succeeded.
- Stale transfer approval visibly failed and retained Parker’s assignment.
- A 390 × 844 viewport supported the single-save advance call-out plus commitment;
  dialog controls and actions have 44-pixel minimum heights.

No real email or Square traffic was needed. Test results and final verification
are recorded below after the final run.

## Changed files

- `app/models.py`
- `app/routers/v2_scheduling.py`
- `app/schema_contract.py`
- `app/services/v2_scheduling_assignments_service.py`
- `app/services/v2_scheduling_attendance_service.py`
- `app/services/v2_scheduling_board_service.py`
- `app/services/v2_scheduling_exception_service.py`
- `app/services/v2_scheduling_lead_duty_service.py`
- `app/services/v2_scheduling_policy_service.py`
- `app/static/v2/scheduling.css`
- `app/static/v2/scheduling.js`
- `app/templates/v2/scheduling/_shift_card.html`
- `app/templates/v2/scheduling/_shift_dialog.html`
- `docs/development/callout-replacement-workflow.md`
- `migrations/versions/20260930_0031_coverage_commitments.py`
- `tests/test_canonical_migration.py`
- `tests/test_coverage_commitment_migration.py`
- `tests/test_schema_migration_postgres.py`
- `tests/test_square_cost_workflow_boundaries.py`
- `tests/test_v2_digital_signage.py`
- `tests/test_v2_lead_duty.py`
- `tests/test_v2_scheduling_exceptions.py`
- `tests/test_v2_scheduling_foundation.py`

## Final focused validation

- Exception workflow, canonical migration graph, fresh/upgrade/stamp contracts,
  commitment downgrade guard and Lead migration: **57 passed, 0 failed**,
  64.02 seconds (`/tmp/callout-final-focused-confirmed.log`).
- Attendance/Longview foundation selection: **26 passed, 0 failed**, 287 outside
  selection deselected, 29.47 seconds (`/tmp/callout-final-attendance.log`).
- The broad Scheduling/repair/A1/Employee Access/HR/Ordering/migration run passed
  583 cases and exposed an additional old schedule-table count assertion. That
  assertion was corrected for the one new table and passed in the final migration
  run above. No failure is attributed to the baseline.
- Earlier failures reflected the intentionally changed advance-call-out and
  same-day-Lead contracts, deterministic dates for prospective transfers, a
  template compatibility bug, and old schema/UI assertions. These were corrected;
  no test was skipped to hide a failure.
- JavaScript syntax and `git diff --check` passed. Exactly one Alembic head: 0031.
- Both disposable preview databases were explicitly verified removed.

## Complete repository verification

**1,165 passed, 0 failed, 1 skipped; 7 subtests passed**, 443.99 seconds.
Command: `TEST_POSTGRES_ADMIN_URL=<disposable local PostgreSQL administrator URL>
PYTHONPATH=. .venv/bin/pytest -q -rs --junitxml=/tmp/callout-final-suite.xml tests`.

The sole skip is the opt-in private R2 integration
`test_real_r2_private_delivery_and_authentication_separation`; no real-R2
credentials or integration flag were enabled. Three non-failing deprecation
warnings concern Starlette/AnyIO and FastAPI lifecycle APIs.

Scheduling foundation/repair, Attendance, Longview weekly fairness, company-wide
Lead, Employee Access, HR/shared Employee profiles, Ordering, A1 snapshots and
migrations all pass. Operational exception tests also assert zero automatic
disciplinary points. Test results are independent of deployment/production data.

No push, main merge, deployment, production migration, production data change or
Render access/change occurred. The permanent Square write boundary is unchanged.
The local commit is made only after the complete suite and acceptance checks pass.
