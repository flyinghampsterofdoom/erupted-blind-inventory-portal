# Longview weekly rotation

Canonical baseline: `58d9249d4443192fb77abf50d97d2978cbc4ae16`.

## Behavior

Each active special-store policy represents the existing Longview policy. Before
ordinary generation, reserve one existing ordinary Longview coverage position for
a legal Vancouver ROTATION employee, even when PRIMARY staff could fill it.
No additional position is created by the traveler reservation/repair service.
The pre-existing weekly-target completion pass remains independent and can add
supplemental labor to satisfy configured employee targets.

Vancouver home stores are the repository's business identities HWY 99, Andresen,
and SR 503 (case, spaces, and punctuation normalized). An explicit home-store
foreign key must resolve to one of those active stores. Missing or other home
stores are excluded and reported; being non-PRIMARY is insufficient. Membership
also requires active employment/Scheduling, valid roster status, ROTATION state,
no conflicting PRIMARY profile, no standing Never restriction, and the existing
inclusive employment cutoff. No new effective-dated membership system is added.
PTO, availability, overlaps, rest, consecutive days, and hours are evaluated for
each proposed shift rather than deleting membership or history.

Weekly traveler planning searches legal employee/position combinations, respects
manual locks, and creates no attendance. Lead and workload repair run normally.
A final safe traveler repair may replace an unlocked assignment, without removing
the sole Lead-capable employee. The final schedule is validated again. Unmet
requirements appear in readiness, generator diagnostics, board warnings, and
existing actionable-warning counts. No waiver mechanism was added.

## Fairness and evidence

The normal unit is one completion per employee per Sunday-start week. Additional
shifts in the same week retain their evidence but do not multiply rotation credit.
Multiple qualifying workers can each receive a completion in the same week.
Historical credit does not disappear when current employment or membership changes.

The primary planning key is the latest of the last qualifying historical week and
the last earlier planned week. No history/reservation sorts first. Existing target,
work-pattern, and preference scores break ties, followed by stable employee and
shift identities. Lifetime counts do not dominate recency. Published and effective
draft reservations move an employee behind other overdue candidates when later
weeks are generated; they never become attendance events. Regeneration removes
unlocked output before ranking and preserves fixed commitments.

A shared attendance resolver returns:

- `PRESUMPTIVE_SCHEDULED` / `PRESUMPTIVE_PUBLISHED`: effective past publication
  without active contradictory evidence. This is scheduling inference, not an
  independently confirmed time-clock fact.
- `CONFIRMED_SCHEDULED` or `CONFIRMED_REPLACEMENT` / `CONFIRMED_EVENT`: consistent
  full WORKED_AS_SCHEDULED or COVERED_SHIFT assertions.
- `CONFIRMED_ABSENCE`: call-out/NCNS without established replacement; no worker credit.
- `UNRESOLVED`: conflicting assertions, conflicting event-bearing revisions, or
  an attendance exception whose substantive duration is unknown; no guessed credit.
- `PENDING` / `PLANNED_RESERVATION`: no completed coverage established.

Same-day presumptive credit waits until the scheduled end in business-local time.
Historical date-only queries use their date boundary; querying today uses the
current business-local time. Original assignments and audit/void histories remain
unchanged. Voiding all contradictory evidence may restore **presumptive** credit,
never independently confirmed credit. Attendance facts expose the same outcome;
`actual_worker_ids` never includes merely presumptive workers.

The approved substantive threshold is at least 50% of the scheduled elapsed shift.
The existing schema has no reliable actual-duration field. Full worked/covered
assertions qualify under the approved fallback. LATE/OPENED_STORE_LATE alone do
not establish duration and remain unresolved. No interval is fabricated, and no
partial-duration entry UI or time-clock system was added. A future interval adapter
belongs in the shared resolver and must enforce the 50% threshold.

No separate ledger, employee identity, call-out system, or migration is required.
Alembic remains at the single head `20260930_0029`. Existing shift lineage and
attendance events suffice for this bounded behavior. Contradictory lineage facts
are surfaced rather than automatically repaired.

## Regression expectation changes

- Existing special-store test fixtures now declare a real Vancouver home store;
  previously ROTATION alone implicitly represented Vancouver.
- Last historical dates now refer to Sunday week starts.
- Selection explanations refer to least-recent qualifying weeks and reservations.
- The discipline-neutrality test takes its comparison baseline after recording
  lateness: attendance changes qualification, while point edits remain neutral.
- A Lead-repair scenario now selects Alex directly because Carla already has an
  earlier draft reservation; it no longer assumes historical count outranks that
  reservation. Separate tests still exercise constrained Lead repairs.
- Regeneration clears legacy queue metadata pointers before replacing disposable
  draft shifts, preventing a foreign-key failure. Those counters/pointers are not
  the fairness source of truth and do not establish actual work.

## Local validation

Tests use uniquely named disposable PostgreSQL databases on localhost. The default
application DATABASE_URL is deliberately unreachable during tests; Square tokens
are empty and snapshot storage is mocked. No production services or data are used.

Browser acceptance uses `/tmp/longview_browser_preview.py` on loopback with
synthetic employees/stores, a disposable database, and mocked email delivery.
Verified readiness success/unmet messages, generated Vancouver-to-Longview board
assignment, preserved locked PRIMARY assignment with an understandable warning,
attendance call-out plus replacement recording, linked Employee Access, employee
login, and current/next published My Schedule. Browser automation stalled at an
existing JavaScript regeneration confirmation; the tab was recovered. Regeneration
itself is covered repeatedly in automated tests.

Test counts and final commit are reported in the implementation handoff.

Final validation on September 30, 2026:

- Focused Longview module: **20 passed, 0 failed, 0 skipped**.
- Dedicated Scheduling regression run: **367 passed, 0 failed, 0 skipped**;
  the two final shared-outcome/time-boundary tests were subsequently included in
  both the focused run and final full suite.
- Final full-suite Scheduling/Longview subset: **369 passed, 0 failed, 0 skipped**.
- Complete final repository suite: **1,086 passed, 0 failed, 1 optional live R2
  skip; 7 subtests passed**, in 402.17 seconds. Three dependency deprecation warnings.
- `pip check` and `git diff --check` pass; Alembic head remains `20260930_0029`.
- Synthetic browser preview stopped and its disposable databases removed.
- No push, deployment, production migration/data change, real email, or Render access.
