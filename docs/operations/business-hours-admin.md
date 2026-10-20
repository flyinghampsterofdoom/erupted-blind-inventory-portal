# Dated business hours for Admin

Local implementation only; not deployed. No schema migration or production holiday entries.

## Admin workflow

1. Open Scheduling → Business hours (also linked from Scheduling rules).
2. Choose a calendar date and selected stores or all active stores in your authorized scope.
3. Name the event, such as Thanksgiving or Christmas Eve.
4. Choose Closed all day, or Different hours and enter the opening and closing times.
5. Save, then review the listed draft or published assignment conflicts.

Each record affects only one date and store. All-store submission is atomic and does not overwrite existing exceptions: use Edit for those. Edit and Restore normal hours check the record timestamp to prevent lost updates. Restoring normal hours deactivates the exception and retains its audit history. Exceptions are not recurring holidays.

## Scheduling behavior

- Closed dates have no generated store positions or double-coverage demand. Assigned and unassigned manual shifts on them are prohibited.
- Different hours use the special closing time and existing 15-minute preparation rule, including when ordinary shift templates have another closing time. Manual assignments must begin at that preparation time and end by the special closing time.
- Review, approval and publication revalidate those constraints. Existing assignments remain untouched when an exception is saved; warnings identify conflicts and the board displays the dated exception, including before a draft exists.
- A changed exception advances overlapping draft versions, preserving old approval evidence while requiring fresh approval. Published schedule metadata and assignments remain unchanged; corrections use the existing authorized workflow.
- Weekly employee allocations are not prorated. A closure can make exact allocations incompatible with available positions. Resolve that separately through authorized allocation changes. Unresolved regeneration preserves a previously populated/generated draft under the accompanying rollback safeguard.
- Authentication, Admin role, the existing special-hours permission, store scope, CSRF, scheduling locks and audit events apply. No employee availability, PTO, payroll or ordinary operating-hour records are changed.

## Verification (2026-10-20)

PostgreSQL integration checks use disposable synthetic databases migrated to the deployed schema (0041), without production connections. The focused suite covers access denial, CSRF, scope and atomic batches, duplicates, invalid hours, edit concurrency, restore/history, draft version invalidation, preserved published assignments, manual exclusion, generated special end times, closed double coverage, publication validation, empty-week board display and rollback without prorating. Existing generation and publication regressions are included.

Evidence directory: `output/business-hours-admin-2026-10-20/` in the enclosing workspace. `final.log` records the final focused run; `admin-preview.png` shows the synthetic UI. JavaScript syntax checks passed.

One broader legacy fixture, `test_unmet_double_coverage_is_store_day_warning_and_preserves_valid_assignment`, expects an unresolved regeneration to return normally. The earlier rollback safeguard now raises and preserves the draft. Reproduced using the committed scheduler at `bea41de` (`base-failure.log`, `check_base_failure.py`); not introduced by this feature and not changed here.

Deployment and actual holiday dates/hours remain owner decisions. Do not infer Christmas Eve hours from synthetic preview examples.
