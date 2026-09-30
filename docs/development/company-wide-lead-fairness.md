# Company-wide Lead responsibility and fairness

Starting canonical tip: `f2b8cc10a5436925e0ccc4a8d87f13e69b8e0000`.
Branch: `codex/admin-v2-canonical-integration`.

## Business boundary

There is exactly one required company-wide Lead responsibility per scheduling day,
covering all four stores regardless of the Lead's physical location. The existing
`(schedule_period_id, shift_date)` designation uniqueness is unchanged. There are
no store/regional Leads, extra staffing requirements by store, hours, minutes,
fractions, shared credits, or opening/closing responsibilities. Multiple capable
employees or multiple shifts do not create additional responsibility-days.

The future pool is dynamic: all currently eligible, Lead-capable employees legally
scheduled for consideration. No fixed number of Leads is assumed. Inactivation,
loss of capability, PTO, employment cutoffs and other hard constraints affect
future eligibility, never historical attribution. New Leads have observed burden,
possibly zero; no extra shifts are created to make them catch up.

## Resolver and evidence

`v2_scheduling_lead_duty_service.lead_duty_facts` resolves effective revisions at
company/day level before attributing any employee. Historical intent comes from
the latest published revision, including previously published archived periods.
Future intent uses the same shared effective-period selection as other Scheduling
assignments: published preferred, otherwise latest draft, with the target editing
context authoritative even when empty. Shift lineage retains attendance evidence
on copied historical assignments of the same employee and date.

- Future designation: reservation, not completed work.
- Past published designation without contradictory work evidence: presumptive
  Lead credit. Explicit WORKED_AS_SCHEDULED confirms work, not independently Lead
  responsibility, so it does not create a second or confirmed Lead credit.
- Absence, work replacement, unknown partial duration or conflicting attendance:
  unresolved Lead outcome with no attributed completion. Lead capability and
  location never turn a replacement into the responsible Lead automatically.
- Explicit PERFORMED resolution: confirmed company-wide responsibility for the
  named employee, overriding scheduled intent and ordinary work ambiguity.
- Explicit UNCOVERED resolution: known no-Lead outcome, no employee credit.
- Explicit UNRESOLVED resolution or missing historical designation: unknown, not
  a manufactured confirmed zero-work assertion.

Completed duty and explicit historical resolution require the business day to have
passed in the configured organization timezone. Ordinary normal days require no
additional management action. Explicit resolutions are authoritative statements
about responsibility; ordinary work facts are neither replaced nor deleted.

## Fairness

For target date T use `[T - 84 days, T)`. Resolve one fact per responsibility-day.
Count confirmed and presumptive historical days once each, plus effective earlier
reservations once each. Separate provenance remains visible in diagnostics and the
employee policy page. Sort by combined count, oldest last duty/reservation within
the window, employee ID, then shift ID. Empty history sorts earliest. Manual valid
selections remain authoritative. Chronological generation adds each selection as a
reservation before selecting later dates; regeneration removes stale automatic
output instead of accumulating phantom burden.

A default future manual reconciliation uses today's planning cutoff, not a future
week start that would incorrectly turn earlier draft reservations into history.
Explicit retrospective planning contexts remain supported for existing workflows.
Old burden ages out of ranking, not out of the audit record.

## Exception workflow and permissions

The Board's Attendance experience shows scheduled Lead, ordinary attendance and
Lead outcome separately. The compact “Who was Lead?” action supports a named actual
Lead, no coverage, or unresolved, with a required reason. The actual Lead can be
someone already working elsewhere, independently of the ordinary work replacement.
Historical employee choices include inactive/formerly capable employees so facts
can be corrected without inventing identities or rewriting eligibility.

`scheduling.lead_duty.resolve` defaults to Admin/Manager and is separate from the
Lead-accessible attendance-recording capability. Routes enforce feature access,
capability, evidence-store authorization and CSRF; the service also checks the
resolution capability. Corrections supersede the active row with actor, time and
reason while retaining every prior row. Voids preserve audit history and recompute
from remaining evidence: an outstanding call-out returns the day to unresolved.
Published historical designation edits are rejected in favor of this workflow.
Past draft intent can still be edited without claiming completed operational work.

Readiness reports unresolved/uncovered historical responsibility-days. Board cards
and Attendance show provenance; existing generation diagnostics explain combined
burden and deterministic selection. There is no new daily certification dashboard.

## Longview and constraints

Longview still requires one eligible Vancouver traveler per week when legally
feasible; Lead still requires one eligible company-wide Lead per day. One person
may satisfy both. Staffing repair remains bounded and lock/eligibility aware.
Final traveler repair preserves daily Lead staffing, then designations and warnings
are reconciled. The Lead-preservation check now verifies another Lead's actual
assignment eligibility rather than relying on a capability flag alone. An inactive
or unavailable employee cannot justify removing the only eligible Lead.

Existing staffing and final validation surface impossible combinations as explicit
unmet traveler/Lead diagnostics rather than illegal assignments or silent success.
No new per-store Lead shortage is introduced. Shared revision selection was
extracted without changing the Longview ranking or attendance semantics.

## Schema and deployment boundary

```
20260930_0029
    ↓
20260930_0030
```

One new `lead_duty_outcomes` table stores business date, constrained outcome,
optional actual employee, source schedule-shift evidence, required reason,
recording principal/time and void principal/time/reason. A partial unique index
allows at most one active company-wide resolution per date. A transaction-level
date lock serializes first insert and correction, including dates without a row.
Normal days and reservations do not need rows. No attendance ledger is duplicated.

The strict startup schema gate now expects 0030. Upgrade is additive from 0029 and
works on a clean database. Empty-table downgrade is permitted; any retained outcome
(including voided audit history) blocks destructive downgrade. Production schema
compatibility assumptions remain unchanged: validation/migration is explicit and
startup does not auto-migrate. No production upgrade, stamp, backfill or data access
was performed.

## Intentional regression expectation changes

The discipline-neutrality test now takes its Lead fairness comparison after its
lateness event, as it already did for Longview. Attendance can change qualification;
adding/reversing discipline points still cannot change fairness. Schema assertions
advance the head to 0030 and inventory to 171 tables. The merge-topology test still
verifies the unchanged two-parent 0029 merge, now followed by 0030.

## Acceptance evidence

All validation uses uniquely named disposable PostgreSQL databases on localhost;
the test application's default DATABASE_URL is deliberately unreachable. Browser
acceptance used synthetic employees/stores on loopback, disabled Square credentials,
and mocked outbound email. No real email was sent.

Browser checks passed:

- Normal published Parker designation displayed presumptive company-wide Lead.
- All four stores had shifts, with only one designated Lead per scheduled date.
- Future Sunday selected Lexi at Longview; Tuesday selected Mikey at HWY 99,
  demonstrating earlier reservations and location-independent responsibility.
- Parker call-out produced unresolved Lead duty; Cameron covering the work shift
  did not inherit Lead credit.
- Explicit Mikey resolution produced confirmed Lead while Parker's assignment and
  Cameron's attendance remained unchanged.
- Void returned to unresolved; no-Lead and superseding unresolved outcomes retained
  complete correction history.
- Readiness showed the historical Lead exception and satisfied weekly Longview
  traveler information together.
- Board, Attendance and the styled compact resolution dialog rendered correctly.
- A normal synthetic employee login displayed published My Schedule, including its
  Lead designation, without management resolution controls.

The synthetic preview was stopped and its database removed. Render and its cron
quarantine were not accessed. No push, merge, deployment or production migration
is part of this development commit.

## Business invariant review

1. One company-wide Lead responsibility exists per required scheduling day.
2. Its scope is all four stores, independent of physical assignment location.
3. No per-store Lead requirement or shortage was added.
4. The eligible Lead pool has no hard-coded size.
5. Fairness selects among currently eligible, capable scheduled candidates.
6. A business date contributes at most one completed Lead credit.
7. Other capable employees working that date create no additional duty.
8. Ordinary work replacement never transfers Lead responsibility automatically.
9. Normal published days require no additional Manager confirmation.
10. Explicit responsibility corrections preserve ordinary attendance and source intent.
11. No hours, minutes, fractions, shared duties, opening/closing duties or regional
    responsibility scopes were introduced.

## Final validation (September 30, 2026)

- Focused Lead module: **34 passed**, zero failures/skips.
- Combined Lead/Longview modules: **54 passed**, zero failures/skips.
- Migration integration and topology: **14 passed**, zero failures/skips.
- Final full-suite Scheduling subset: **403 passed**, zero failures/skips.
- Complete repository suite: **1,122 passed**, zero failures, **one optional live
  R2 skip**, and **seven subtests passed**, in 396.10 seconds.
- The R2 test requires `RUN_REAL_R2_TESTS=1`; no live object-storage integration was
  requested or exercised. Three dependency deprecation warnings remain.
- `pip check`, Python compilation, `git diff --check` and the single Alembic head
  check passed. These test groups overlap and must not be added together.

The preceding full run exposed the expected table inventory change (170 → 171).
That assertion was corrected; no failure was dismissed as pre-existing. The
final full run includes the final future-reconciliation, eligible-other-Lead and
missing-history regressions. No required validation remains outstanding.
