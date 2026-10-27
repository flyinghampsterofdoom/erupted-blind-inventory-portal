# Owner scheduling approval and shift Lead designation

Implemented locally on `codex/scheduler-owner-lead-20261026`, based on main `8f66349`.
No deployment, production database migration, or permanent employee-role change was performed.

## Behavior

Authorized Owner approval resolves missing company-wide Lead availability during
approval/publication and records the accepted warning in both lifecycle audit
events. Accepted employee requests, including legacy requests, execute on Owner
approval without a second Lead attestation. Permanent Leads may review eligible,
consented transfer/hour requests. Explicit permission denials, recipient consent,
stale-version checks, eligibility, store scope, structural integrity, PTO protections,
and separately authorized operational exceptions remain binding.

Shift and coverage records now have an `is_shift_lead` designation, independent
of permanent employee/account roles and the existing company-wide Lead of the Day.
Owner controls appear in shift creation/editing, Store Shift placement, movement,
both sides of swaps, and call-out coverage. Choices are Keep, Assign, and Remove.
Designated work cards show **LEAD** on the scheduler and employee schedule.

Moving a designated shift prompts review of the original location and interval,
with an optional existing shift to receive Lead duty in the same save. Store swaps
provide this for both sides. The backend locks and revalidates backup assignments;
invalid backups roll back the complete operation. A non-Owner cannot transfer or
extend temporary authority to another employee, location, date, or interval.

## Authorization and transactions

Temporary authority is resolved from effective current published assignments on
every action. It applies only to attendance/coverage and resource-scoped transfer
review responsibilities, during the designated interval at its location. Existing
transfer timing/consent rules still apply. It never grants schedule generation,
publication, access management, employee administration, attendance-point management,
or company-wide Lead-duty resolution. Explicit permission denials remain effective.

Coverage uses its own designation and exact interval; relief does not inherit the
absent employee's authority. End time, removal, cancellation, absence, archival,
schedule clearing, and superseding publication remove the effective grant without
a cleanup job. Draft and historical assignments do not grant temporary authority.
Scheduler-only operational visibility does not change inventory or administrative
store scope. Account roles and employment capabilities are never updated.

Assignment, designation, optional backup, approval/publication, notifications, and
audit writes use the existing scheduling lock and transaction/savepoint boundaries.
Late failures roll back the complete operation even if a caller subsequently commits.
Lead changes are captured in assignment audits and immutable lifecycle snapshots;
coverage designation changes and cancellations retain their audited history.

## Database and compatibility

Release migration `20261026_0052` follows the deployed purchasing migration `20261026_0051`, adds false-by-default Boolean
columns on `schedule_shifts` and `schedule_coverage_commitments`, and requires an
employee for a designated shift. No employee, account, or role backfill occurs.
The schema contract and schema-head tests advance together. Downgrade refuses to
drop populated Lead-designation evidence; a release needs a schema-compatible rollback.

Existing immutable snapshots remain untouched. Pending approvals captured before
the new designation field may need reapproval before publication. The combined
completion service already preserves old evidence and creates a fresh attestation
when operational content no longer matches an earlier approval.

## Qualification

Tests use disposable local PostgreSQL databases upgraded through the real Alembic
chain. The acceptance suite covers all nine requested scenarios, plus explicit
permission denials, wrong location/date/interval, inactive employees, failed audit
writes, late failures, legacy requests, both sides of published store swaps, and
real authenticated HTTP access before and after duty expiration.

| Qualification | Result |
| --- | --- |
| Final acceptance suite | 28 passed |
| Focused Lead/transfer/publication/foundation checks | 60 passed, 280 deselected |
| Broader regression run | 232 passed, 1 skipped |
| Scheduler and employee-schedule JavaScript | 15 passed |
| Python compilation, JavaScript syntax, diff whitespace | Passed |

Recorded run results are in `evidence/scheduler-owner-lead-*.txt`. Runs overlap;
counts must not be added as a count of unique tests. One broader regression test
was skipped. Existing dependency deprecation warnings are retained in the logs.

Reproduce using `TEST_POSTGRES_ADMIN_URL` pointed at a **disposable local test
server**, then:

```sh
python -m pytest -q tests/test_scheduler_owner_shift_lead.py
python -m pytest -q tests/test_scheduler_owner_shift_lead.py tests/test_v2_scheduling_foundation.py -k 'owner or lead or transfer or publication or capability'
node --test tests/js/scheduling*.test.cjs tests/js/employee_schedule.test.cjs
```

Broader regression qualification includes atomic employee swaps, call-out backend
and HTTP behavior, operational access, combined completion, lifecycle integrity,
manual movement, snapshots, company-wide Lead duty, migration/schema contracts,
Square cost boundaries, and digital signage. Production release qualification and
deployment remain separate from this local implementation.

## Release integration

The release candidate is integrated onto production web `d56aa1bf38d5880dcdf2fb09037a360402d41038`. The original local migration number 0051 was already used by the released catalog policy migration; the scheduler migration is renumbered to 0052. The catalog migration and purchasing code are preserved. New exact-candidate CI, migration, artifact, cron, and rollback qualification are mandatory before deployment.
