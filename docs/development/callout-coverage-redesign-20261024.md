# Call-out and coverage redesign — local review

Local branch: `codex/callout-coverage-redesign-20261024`.
Base: `5c71129` (latest released Lead inventory correction).
Production has not been deployed or migrated by this work.

## Ordinary workflow

Use the Call-out / coverage action directly on an assigned shift. The employee,
date, hours, and store are populated automatically. Choose Covered / Reassigned
(the default for an unmarked shift), Called out, Late, Left early, or No-show;
select an optional replacement; add optional notes; Save. Neutral reassignment
records coverage alone and preserves any independently recorded call-out.
Coverage defaults to the complete shift. An optional hours disclosure supports
partial relief and its unpaid break. History remains available without entering
the ordinary save flow. Corrections and cancellation accept optional notes.

Owners/Admins, legacy Managers, and Leads use their existing attendance
capability and store-scope boundary. Explicit capability denials still apply.
Existing Lead policy authorizes all active stores; this change adds no new
per-store ACL and gives Store employees no scheduling administration permission.
The selected store scope is checked at the HTTP boundary, and service mutations
also validate authorization. Coverage confers neither Lead designation nor any
administrative permission on a replacement.

## Sources of truth

No duplicate shift table or access-grant system was introduced.

| Fact | Existing authoritative record / projection |
| --- | --- |
| Original published assignment | `ScheduleShift` and its publication lineage |
| Absence or attendance exception | `ScheduleAttendanceEvent` |
| Replacement and optional partial hours | `ScheduleCoverageCommitment` |
| Effective intervals | Shared `effective_shift_segments` resolver |
| Personal/company cards | Published schedule projection, including a distinct coverage card |
| Working store and employee backend access | Live `inventory_working_store_service` using the same effective intervals |
| Actual work | Existing attendance evidence resolver; coverage does not assert actual work |
| Audit | Existing attendance and coverage audit events with actor, timestamp, source shift, store, original and replacement employee, interval, and retained corrections |

The earlier personal schedule API used a single expected employee per shift;
the old work-store resolver used that employee with the entire original interval.
Those paths could hide the original card and overextend partial coverage. They
now use the same interval projection as the board and published company view.

The original card stays under the original employee with the existing `!`
indicator. The replacement gets a purple COVERAGE card containing the store,
hours, covering employee, and source link. Original, effective, and actual hours
are represented separately. An absence contributes no effective or worked
hours to the original employee. An assignment alone never establishes actual
hours. Published labor estimates use effective intervals and each worker's
existing effective-dated compensation rate.

Current coverage takes priority over a conflicting ordinary work-store context
during its assigned interval. Existing assignments and their records remain.
After cancellation, reassignment, or expiry, the live context resolves the
remaining legitimate assignments. Home store, principal role, permission
overrides, approved PTO, and historical access grants are unchanged. Assignment
fingerprints and the employee context version reject stale operational writes;
current sessions receive the new context without signing in again.

The optional V2 daily-log/current-store workflow retains its pre-existing
permission model. Employee inventory, counts, chores, and other `/store` work
continue through the scheduled working-store authorization boundary.

## Warnings and history

Published overlap warnings compare effective employee intervals, including
replacement coverage and residual intervals from partial relief. Historical
original cards remain visible without creating phantom overlap warnings.
Draft overlap warnings continue to evaluate the proposed draft assignments.

Overlapping assignments, approved PTO, overtime, hour limits, consecutive days,
availability, and store scheduling preferences are warnings for authorized
coverage. They require no exception checkbox, approval note, or confirmation.
They do not edit PTO or the replacement's other assignments. Inactive or
out-of-employment identities and genuine role/store authorization restrictions
remain binding.

Changing coverage supersedes its previous retained record. Changing status
voids the earlier status with an automatic audit reason. Cancelling coverage
retains its full record. Existing explicit actual-work evidence remains
independent from a replacement agreement and is never silently rewritten.

## Migration and review

Migration `20261024_0049` follows `20261021_0048`. It adds LEFT_EARLY to the
existing attendance enum and three nullable interval fields to the existing
coverage table. NULL preserves legacy full-shift coverage without backfill.
Check constraints validate interval order and nonnegative breaks. Downgrade
refuses to discard partial-coverage or LEFT_EARLY history.

No CORE integration, external notification, Square write, permanent store
reassignment, new approval flow, or production operation is required.

## Acceptance evidence

`tests/test_coverage_operational_access.py` contains a complete authenticated
two-client HTTP scenario: a Lead records Micah's call-out and Cameron's
replacement, Cameron's existing session changes from Andresen to Longview,
both schedule cards and effective hours appear, Cameron writes an actual
Longview chore record and opens inventory, unrelated administration is denied,
and subsequent reassignment/cancellation restores prior access and rejects
stale writes. It verifies that the login cookie, original assignments, saved
work, and audit records survive the workflow.

Additional integration tests cover role and scope denials, all four statuses,
partial relief, overlapping shifts, PTO and policy warnings, cancellation,
repeated replacement changes, retained migration data, interval expiry,
published revisions, labor rates, and original/effective/actual projections.
The browser preview uses four synthetic stores and synthetic Micah/Cameron
identities only.

## Final validation

The approved cascade corrections and final deployment-candidate validation are
documented in [Cascading coverage candidate](cascading-coverage-candidate-20261024.md).
The runs below describe the earlier redesign validation.

| Run | Result |
| --- | --- |
| Final coverage, authenticated HTTP, operational access, projection, migration, exception, and schedule-clear suites | **150 passed**, no skipped cases; 190.76 seconds |
| JavaScript suites, including candidate query composition and commitment/actual-worker editor separation | **17 passed**, no skipped cases |
| Card renderer compatibility after repair | **10 passed**, 303 outside the focused selection deselected |
| Ordering routes and permanent Square write/cost boundary regressions | **35 passed** |
| Canonical migration, explicit role/scope/CSRF, and API acceptance focused run | **32 passed** |

The broader three-batch sweep exercised every repository Python test file:
2,260 passed, 11 initially failed, 344 skipped, and 7 subtests passed. Ten failures
identified a renderer compatibility issue when a minimal caller omitted the new
hour fields; unknown hours now remain unknown in both server and browser
renderers, and all ten tests pass. The remaining failure was an outdated test
expectation omitting the already-existing read-only `/v2/catalog/search`
endpoint. Its assertion was corrected without changing the gateway or permanent
Square policy; all 35 related regression cases pass. These repairs were verified
with focused reruns; the complete sweep was not repeated afterward.

The 344 broader skips are 342 separately configured opt-in local PostgreSQL
checks and two real hosted R2 integrations. They do not include the requested
coverage acceptance cases. No real hosted integration credentials were used.
The only warnings in the coverage run are existing Starlette/AnyIO and FastAPI
lifecycle deprecations. Python compilation, JavaScript syntax, and
`git diff --check` also pass.

Final review added regressions for partial-shift logical counts, incompatible
intermediate revisions and later reversions, current employee absence markers,
LEFT_EARLY conflicting with full-shift assertions, removal of the final coverage
card through draft editing, and immediate access revocation when employment
eligibility ends. Old coverage cannot regain authority through a later revision
that happens to restore the earlier store or times.

Evidence is in
`/Users/justinrawlinson/Developer/Erupted/output/callout-coverage-redesign-20261024/`:
`final-coverage.log/xml`, `javascript-final.log`, `renderer-regression.log`,
`square-regression.log`, the three `final-suite-*.log/xml` files, and the
`acceptance-board.png` and `simple-callout-form.png` browser screenshots.
The synthetic local preview is available at `http://127.0.0.1:8794` while its
loopback process remains running. Preview logins and disposable database
metadata are in `preview.json` in that evidence folder.

No push, PR, merge, production deployment, production migration, production data
change, real notification, Square request, or CORE dependency was introduced.
