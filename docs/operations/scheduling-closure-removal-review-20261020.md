# Allocation targets, regeneration and manual removals — review

Local review candidate only. **Not deployed.** Branch: `codex/regeneration-targets`, based on verified production web revision `7c07ba7`. It incorporates the prior undeployed closure/removal work while preserving the deployed performance and session-listener fixes. No migration, production mutation, provider setting change, cron change, or publication is included.

## Actual production failure

A fresh production snapshot was acquired with PostgreSQL read-only transactions and restored into an isolated local database. Reproduction used the verified production web source, not a synthetic closure assumption. Every baseline reproduction rolled back locally.

| Schedule | Production-source reproduction | Local correction |
| --- | --- | --- |
| #21, October 25–31 | Valid | Valid, zero assignment changes |
| #23, November 8–14 | Valid | Valid, zero assignment changes |
| #25, November 22–28 | `WEEKLY_ALLOCATION_COVERAGE_MISMATCH`; draft unchanged | Valid, all 24 remaining assignments retained |
| #29, December 20–26 | Consecutive-day and recurring hard-availability violations; draft unchanged | Valid, 20 of 24 remaining assignments retained; four reassigned |

For #25, ten employees' effective weekly targets total **28 shifts** (nine targets of three and one target of one). Saved Thanksgiving closures on **November 26** at **Andresen (#2), HWY 99 (#4), Longview (#6), and SR 503 (#9)** reduce coverage from 28 to **24 positions**. No store has duplicate coverage demand; each closure removes one position. There are **no locked assignments** in the affected week. Approved full-day PTO for employee #4 runs **November 25–December 1**. Their three assignments before that leave remain feasible and are preserved. This is not a missing leave authorization or permission problem.

The old allocator unconditionally compares the sum of employee allocations with available positions, rejects 28 versus 24, and returns `UNRESOLVED`. Its regeneration wrapper then raises inside a savepoint because an existing plan exists. That explains the truthful “draft left unchanged” message. The joint solver and approval gate also demanded exact allocations, so bypassing the first check alone would not solve the problem.

After correction, employees #1, #5, #6 and #116 each retain two assignments against a target of three in #25; the other six retain their targets. These are allocation shortfalls, not coverage vacancies. Source allocations remain unchanged.

Christmas closures on **December 25** produce the same 24-versus-28 capacity difference. That week also has existing invalid consecutive-day/availability combinations and no overlapping approved PTO or locked assignments. The corrected local plan changes shifts #873 (SR 503, December 22), #875 (HWY 99, December 23), #879 (HWY 99, December 24), and #887 (HWY 99, December 26), while retaining every other remaining assignment. The independent policy evaluator validates the complete result, including adjacent authoritative weeks.

Private reproduction, employee-level constraints, exact before/after assignments, and preservation fingerprints are under the enclosing workspace's `output/regeneration-targets-20261020/` directory. Real employee names and the production dump are excluded from Git. The production-copy acceptance was rerun from a second fresh restore against the review candidate and reproduced the same zero/zero/zero/four assignment-change counts.

## Correction and scheduling behavior

- Initial generation, single-week regeneration and explicit horizon regeneration use the same constraint planner. The unconditional exact-allocation fallback is removed.
- Approved PTO, recurring hard availability, employee/store eligibility, operating hours, overlap, maximum paid hours, consecutive-day/rest restrictions, protected assignments and special-store participation remain constraints.
- Maximize filled operating positions first, then required Lead/traveler coverage. During repairs, preserve as many existing assignments as possible. Optimize remaining distribution by squared deviation relative to weekly targets, then rotation/weekend/preferences within those results. A timeout is inconclusive; it is not proof that staffing is impossible.
- Allocations are **targets rather than minimums or maximums**, including one-shift reserve targets. Coverage may place someone above target when hard rules permit it; no allocation configuration is rewritten. A zero target is not a substitute for PTO, ineligibility or hard unavailability.
- The old joint solver also assumed exactly one special-store traveler assignment per week. The existing policy and legacy acceptance case allow additional eligible rotation coverage when primary staff are constrained. That derived solver quota is removed; the open-week traveler requirement, store participation, hours and consecutive-day rules remain enforced. A five-day special-store test with approved primary PTO verifies two legal rotation assignments without inventing a hard traveler cap.
- Keep unfillable positions in a saved, reviewable draft, with explicit vacancies and missing required roles. Safe partial drafts receive snapshots and audits. Locked hard-rule conflicts still require an explicit correction and roll back atomically; regeneration never changes the lock to resolve them.
- Complete coverage and all publication requirements still gate approval/publication. Allocation shortfalls alone do not block them. Incomplete output remains a draft, including when publication-with-warnings permission exists. Fully closed stores require no coverage or weekly traveler.
- The stale-plan signature normalizes timestamps to UTC, avoiding false conflicts when a PostgreSQL reload changes the timezone representation of the same instant.

## Manual removal

Authorized Admin/Manager management can choose **Remove assignment** on an unlocked shift through the existing scoped, versioned, CSRF-protected update endpoint. Locked rows must first be explicitly unlocked. The unlock control remains available immediately after a browser update as well as after a page reload. Employee-dependent Lead/Double Coverage flags are cleared with the employee; legacy supplemental links are detached safely where necessary.

Removal leaves a visible, intentionally unfilled position protected from regeneration until explicitly assigned or unlocked. Closed dates require no replacement. Open-store vacancies remain coverage warnings and block publication. Save/reopen, single-week/horizon regeneration and replacement-revision cloning preserve the removal. The existing audit records before/after employee IDs, actor, time, period/version and removal intent. Published source schedules remain read-only and retain the editable-revision workflow.

## Explanations

Diagnostics separate `ALLOCATION_SHORTFALL`, `UNFILLED_COVERAGE`, protected hard conflicts and inconclusive search. They carry employee names/IDs, store/date/shift identifiers, targets versus planned counts, dated closure context and policy rejection reasons. Vacancy reasons are evaluated against the complete proposed week, so hours and consecutive-day interactions are included. The board uses checksum-verified, current-version generation evidence to explain coverage warnings after save/reopen; it does not show an old diagnosis after another input change. Existing store/employee visibility scopes filter that detail.

## Verification

See final test results appended below. All mutation tests used disposable synthetic PostgreSQL databases or the isolated production copy. Production access was read-only.

The production-copy acceptance saved all four reviewed weeks, reopened an actual copied assignment after removal, regenerated it without replacing the vacancy, and verified that approval was blocked. **17 protected data sets retained identical fingerprints**, covering employees/profiles, eligibility, weekly allocations, hard availability/store preferences, approved PTO, hours/closures, coverage configuration, approvals, attendance, original audit/snapshot rows, all other schedule periods and all assignments outside the four regenerated weeks.

Tests cover ordinary and closure weeks, all-week closure including special stores, partial/full-week approved PTO, manual removals and reopen, hard availability/hours/consecutive restrictions, locks, protected hard conflicts, stale plans, missing coverage, approval/publication, HTTP authorization/CSRF/versioning, historical schedules, immutable snapshots, and the nine-week anonymized production roster. The existing inventory-access callout test used an October 20 event time but the live clock after shift end; its test clock is now frozen to that declared event time. No inventory, callout or authorization runtime is changed. Older assertions that required regeneration failure solely because targets could not be met were changed to assert covered/legal output or explicit vacancies; hard-rule rejection tests remain authoritative.

## Operational review

The provider read showed web `7c07ba7` and cron `1b26a8a`. Neither was changed. This candidate retains the newer web changes; release planning must align approved source identities deliberately. This report authorizes no deployment.

## Final results

- **741 passed** in the clean combined scheduling regression run (217.95 seconds, six test workers). This includes all `test_v2_scheduling_*` modules, weekly allocations, horizon service/solver, business hours, PTO workflow and the new regeneration-target acceptance cases. The 18 warnings are existing framework deprecations.
- Both JavaScript assignment/removal suites passed; JavaScript syntax, Python compilation and `git diff --check` passed.
- The final fresh production-copy run passed all four actual schedule cases and the audited removal/save/reopen/regeneration/publication-blocking sequence. Its 17 protected fingerprints are identical before/after.
- No database migration is required. No production data, provider configuration, cron configuration or deployed source was modified.

Logs and private data-preservation receipts remain in `output/regeneration-targets-20261020/` in the enclosing workspace. The final clean log is `verified-regression.log`; the production-copy receipt is `copy-acceptance.private.json`. Backups, employee details and credentials are not part of the commit.
