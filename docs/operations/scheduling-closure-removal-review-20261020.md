# Scheduling closures and manual removals — review candidate

Status: local corrections only; **not deployed**. Base: `1b26a8a` (the consolidated deployed scheduling source). Branch: `codex/scheduling-closure-removals`. No database migration is required.

## Root causes

Coverage materialization already omitted dated closures, but both assignment solvers and final approval still required exact normal weekly allocations. An entirely closed week also hit a zero-position validation error.

The assignment-change dialog required a replacement. The ordinary shift editor could submit an empty employee, but removing a Lead/Double Coverage employee left designation flags that violated database constraints. Regeneration also modeled every position as assigned, including intentionally empty locked positions. Published-revision cloning discarded manual lock metadata.

## Corrections

- Add **Remove assignment** to shift actions for authorized administrators/managers. Save an empty, manually locked position through the existing versioned, scoped, CSRF-protected edit endpoint. Clear employee-dependent designations and record before/after employee IDs plus removal intent in the existing audit event.
- Preserve empty manual positions through single-week and joint regeneration, save/reopen, and published-revision cloning. Legacy supplemental positions become ordinary vacancies; their same-draft supplemental pointer is detached so later closures can remove obsolete automatic source positions.
- Bound weekly allocation relief by actual coverage positions removed by active dated closures. Explicit manual vacancies permit planning around the saved decision, without waiving operating coverage at approval. Ordinary targets and future weeks remain unchanged; over-allocation remains prohibited.
- Preserve valid remaining assignments during repair. Fresh reduced-capacity generation attempts to spread shortages fairly; joint planning optimizes allocation equity after assignment stability. Closure weeks do not force a special-store traveler to displace an existing employee.
- Keep PTO, hard availability, store/location rules, hours limits, consecutive-day restrictions, and required operating coverage authoritative.
- Label open coverage, intentionally unfilled positions, and closed dates separately. Allocation warnings explain closure-related shortages while retaining normal target counts. A fully closed week can have zero shifts and receive approval.

## Verification

All database tests used fresh, UUID-named disposable databases on local PostgreSQL with synthetic employees, assignments, PTO, and business-hours exceptions. No hosted database/provider calls, production record changes, or deployment commands were used.

- Original-source reproduction: **1 passed**; the existing regression test confirms that a closure previously caused regeneration to raise and preserve the draft.
- Scheduling regression suite: **189 passed** across business hours, exact coverage, horizon solver/service, dated allocations, PTO workflow, repair, snapshots, and pre-opening rules.
- Acceptance and existing API/versioning checks: **13 passed** (10 new acceptance cases plus 3 existing permission/CSRF/versioning tests).
- Additional final cases exercise fresh closure fairness and removal of legacy supplemental coverage followed by closure: **3 passed**.
- Final candidate rerun: **18 passed** (all 10 new acceptance cases and all 8 horizon solver tests).
- Both JavaScript handler suites passed; Python compilation, JavaScript syntax checking, and `git diff --check` passed.

The acceptance cases cover employee removal, save/reopen, one-store/day closure, all-store/day closure, multiple closed days, an entirely closed week, insufficient available shifts, approved PTO, regeneration preservation, unchanged following-week targets/enforcement, closure-shortage approval, remaining-assignment stability, Lead denial, Admin success, CSRF enforcement, server-rendered labels, and published revision lock preservation.

The old business-hours test expecting every closure shortage to fail was updated to the requested behavior; it still asserts that normal allocations remain unchanged. Normal-week mismatch and hard-rule tests retain their failure expectations.

## Review and operational limits

Operating vacancies remain saved drafts with coverage warnings. Required operating coverage still blocks approval/publication until resolved; a manual removal does not waive that requirement. Closed positions require no coverage. Published schedules retain the existing replacement-draft workflow; published source records stay authoritative until the replacement is approved and published.

Fairness search remains bounded and may be inconclusive on a larger constrained roster. Failed regeneration continues to roll back atomically. Testing used synthetic fixtures, not a production roster simulation or a deployed-browser session.

Review artifacts and test logs are in the enclosing workspace's `output/scheduling-closure-removals-2026-10-20/` directory. Deployment requires explicit approval.
