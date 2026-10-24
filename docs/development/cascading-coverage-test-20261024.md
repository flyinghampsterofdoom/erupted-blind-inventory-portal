# Cascading coverage test — Micah → Cameron → Mikey

## Verdict

**The existing implementation does not fully pass the requested scenario.**
Its effective schedule and live operational authorization support the completed
cascade correctly. The ordinary call-out form cannot express Cameron being
covered without asserting an attendance exception, so that path creates a false
call-out and fails the requested reversals. A separate warning calculation also
reports an overlap after the effective conflict has been resolved.

This was testing only. No application code, migration, production schedule,
production employee, permission policy, or approval workflow was changed.
Corrections below are proposals only.

## Environment and method

Tested the existing local candidate in
`/Users/justinrawlinson/Developer/Erupted/worktrees/callout-coverage-redesign`
on branch `codex/callout-coverage-redesign-20261024`, based on `5c71129` plus the
previously implemented local redesign. This does not establish the state of a
deployed production build.

Each scenario used a fresh disposable localhost PostgreSQL database, fully
migrated through `20261024_0049`. Synthetic Micah, Cameron, and Mikey each had
their own active STORE account linked to their Employee identity. A separate
active Lead logged in through the real password and session routes. No
permission override was added. Mikey had no scheduled shift; his existing home
store association did not grant him off-duty operational access.

The synthetic published assignments were exactly 08:45–22:15 at Longview for
Micah and Andresen for Cameron. No unpaid break was specified, so each shift
was 13.50 hours. The operational clock alone was fixed at noon Pacific, within
the tested shift; identities, login cookies, CSRF, permissions, scheduling
mutations, working-store resolution, and database writes used actual application
paths. DB/session dependencies pointed to the disposable database; authentication
and authorization were not overridden.

Four runs tested two Step 2 inputs, each with both cancellation orders:

1. **Coverage-only API:** the existing attendance route receives
   `{"commitment_employee_id": <Mikey>}` without `event_type`. This records
   Cameron being covered without falsely recording his absence.
2. **Ordinary form payload:** the same route receives the payload emitted by the
   current form, including its default `event_type: "CALLED_OUT"`. This is a real
   backend request matching the form's current code, not a browser submission.

All runs continued through every reversal before asserting failures, preserving
evidence for successful and unsuccessful checks. Both coverage saves and both
cancellations required no notes, approval flags, or extra confirmations.

## Completed-chain results

| Employee | Effective assignment | Effective hours | Live employee access |
| --- | --- | ---: | --- |
| Micah | Absent | 0.00 | No working store |
| Cameron | Longview | 13.50 | Longview |
| Mikey | Andresen | 13.50 | Andresen |

Both input paths produced this effective result, with 27.00 total effective
hours and one effective assignment at each store. Cameron was not double-counted
after Mikey took Andresen. Original published shift rows remained byte-for-byte
equivalent at the column-value level, and both original cards remained visible.
The two replacements had distinct purple coverage cards linked to their source
shifts. No actual-work assertion was invented from a coverage assignment.

The initial intermediate state was permitted: after Step 1, Cameron retained
both effective assignments and 27.00 effective hours while Longview was selected
as his operational working store. The overlap warning did not block saving.

### Actual backend authorization

The employee sessions existed before either coverage change and kept their
original login cookies throughout. After completing the cascade:

- Cameron opened inventory and saved a real synthetic Longview chore record.
- Mikey opened inventory and saved a real synthetic Andresen chore record.
- Database checks verified the written work records' store associations.
- Cameron's prior Andresen write using its old context was rejected with 409.
- Cameron writing Andresen with his current Longview context was rejected with 403.
- Mikey writing Longview with his current Andresen context was rejected with 403.
- Both employees were denied management session oversight and user administration
  with 403. Their STORE roles and home-store associations stayed unchanged.

The access resolver itself therefore passed the completed-chain authorization
requirements. It did not grant both stores to Cameron once the chain was complete.

## Reversals

### Coverage-only API input

Both cancellation orders produced the expected hours and access:

| State | Micah hours | Cameron hours | Mikey hours | Working-store result |
| --- | ---: | ---: | ---: | --- |
| Complete chain | 0.00 | 13.50 | 13.50 | Cameron Longview; Mikey Andresen |
| Cancel Mikey first | 0.00 | 27.00 | 0.00 | Cameron Longview with Andresen overlap; Mikey off |
| Then cancel Cameron | 0.00 | 13.50 | 0.00 | Cameron Andresen; Mikey off |
| Cancel Cameron first instead | 0.00 | 0.00 | 13.50 | Cameron off while Mikey still covers Andresen |
| Then cancel Mikey | 0.00 | 13.50 | 0.00 | Cameron Andresen; Mikey off |

When Mikey was cancelled first, the overlap warning correctly remained while
Cameron again held both effective assignments. Temporary access was removed
when the corresponding assignment ended, and former coverage-store writes were
denied. Micah's call-out remained active.

### Ordinary form input

The completed chain initially appeared correct, but the form also recorded
Cameron as CALLED_OUT. Cancelling Mikey therefore left Cameron at 13.50 Longview
hours instead of restoring his Andresen responsibility and 27.00 intermediate
hours. After both cancellations, Cameron had **0.00 effective hours and no
operational store**, rather than his original 13.50-hour Andresen assignment.
Inventory returned 409 because that false absence still removed him from work.

Cancelling in the opposite order had the same incorrect final outcome. Mikey
correctly returned off duty, Cameron's Longview access was removed, and Micah's
real call-out remained recorded. The missing Andresen return was caused by
Cameron's false absence, not an access privilege leak.

Both coverage agreements, both cancellations, the actor, timestamps, exact
intervals, original/replacement employees, stores, and source shifts remained in
audit history. The ordinary-form path's additional false attendance event was
also retained, which is precisely why this is a data-semantics defect rather
than a purely visual issue.

## Defects and smallest proposed corrections

### 1. Ordinary form conflates relief coverage with absence

`app/templates/v2/scheduling/_shift_dialog.html:68` offers only Called out, Late,
Left early, and No-show. `app/static/v2/scheduling.js:537` defaults a shift without
an attendance event to CALLED_OUT, and its submit handler at line 844 always
sends that selected event type. Cameron therefore acquires a false absence when
the Lead simply arranges relief for Andresen.

Coverage cancellation correctly preserves independent attendance evidence
(`cancel_commitment` in `v2_scheduling_exception_service.py:319`). Consequently,
the false Cameron call-out survives, and `effective_shift_segments` at line 185
correctly continues to exclude an employee with an active absence.

**Smallest proposal:** expose the already-supported coverage-only operation in
the existing form, for example a neutral “Covered / reassigned” status that sends
no attendance event. Keep genuine absences explicit and independent. This needs
no new approval, mandatory note, database table, or permission. Do not fix this
by indiscriminately voiding attendance when coverage is cancelled: that would
erase Micah's genuine call-out. Existing wrongly recorded absences would require
their own explicit audited correction.

### 2. Completed chain retains a false overlap warning

Both API and ordinary-form runs reported an EMPLOYEE_OVERLAP warning for Cameron
after the complete chain, despite his effective hours being only Longview's
13.50. At `app/services/v2_scheduling_coverage_service.py:611`, warning generation
evaluates every original shift against the effective assignment set. It still
proposes Cameron as the worker of his retained Andresen source card, detects his
real Longview coverage, and warns about a responsibility that Mikey has already
replaced. The overlapping original card is historical intent at that point.

**Smallest proposal:** for published schedules, derive overlap warnings from the
same effective intervals used by hours and operational access. Keep historical
cards intact. Recompute warnings through the existing board/coverage paths so
the warning clears after Step 2, returns if Mikey is cancelled first, and clears
again after Cameron's Longview coverage is cancelled. Draft scheduling rules and
publication approval requirements need not change.

Neither proposed correction was implemented during this test request.

## Reproduction and evidence

New test only: `tests/test_cascading_coverage_acceptance.py`.
Run with the existing test runtime and a disposable localhost PostgreSQL admin:

```sh
ENVIRONMENT=test TEST_POSTGRES_ADMIN_URL='postgresql+psycopg:///postgres' \
CASCADE_EVIDENCE_DIR='/Users/justinrawlinson/Developer/Erupted/output/cascading-coverage-test-20261024' \
PYTHONPATH=. \
/Users/justinrawlinson/Developer/Erupted/output/lead-stocktake-unlock-20261023/test-venv/bin/python \
-m pytest -q tests/test_cascading_coverage_acceptance.py --tb=short
```

The regression cases intentionally remain failing against the unchanged
implementation. Two coverage-only runs each fail solely on the false completed
overlap warning. The two ordinary-form runs additionally fail on Cameron's false
absence and its hour/access consequences during reversal. There were no harness
errors or skipped scenarios.

Machine-readable snapshots, checks, and results are in
`/Users/justinrawlinson/Developer/Erupted/output/cascading-coverage-test-20261024/`:
four mode/order JSON files plus `results.log` and `results.xml`. Session tokens
are not included in the evidence. All disposable databases are removed by the
test fixture, including failed-test teardown. Application diff fingerprints
before and after testing are identical.
# Follow-up

This report retains the original test findings. The user subsequently approved
both corrections; see [the final candidate report](cascading-coverage-candidate-20261024.md)
for the corrected implementation and rerun evidence.
