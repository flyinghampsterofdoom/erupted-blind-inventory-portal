# Schedule Board calendar week navigation

Local review branch: `codex/scheduling-week-navigation`, based on `7c07ba7`.
No deployment or live database access was performed for this change.

The week label is a calendar button. Existing Previous week, Current week and Next
week links retain their original targets. Choosing any date opens its Sunday–Saturday
week through the existing board GET, preserving store filters and removing stale
revision/history parameters. Empty weeks retain the existing empty state and actions.
Navigation never calls creation, generation, approval or publication endpoints.

The native modal dialog provides month/year controls, whole-row hover/focus,
viewed-week outlining, date labels with status, a legend, Escape and focus return.
Arrow keys move by date/week, Home/End move to Sunday/Saturday, Page Up/Down change
month, and Enter/Space select a week. Dates use UTC date arithmetic so browser
timezone and daylight-saving transitions do not shift week boundaries.

## Publication model and colors

Publication is atomic for a global weekly SchedulePeriod. The schema allows one
active published period and one draft per week. Publishing replaces the prior
published revision, and cannot publish only the stores in a filtered view.
The calendar therefore describes all-store status, regardless of board filters.

| Current records | Calendar |
| --- | --- |
| Published period only | Subtle green, Published |
| Generated or manually populated draft only | Subtle yellow, Unpublished draft |
| Published period plus draft revision | Green/yellow stripe, Published with unpublished revision |
| No current schedule, or empty ungenerated draft | Neutral, Not generated; empty draft identified in date label |
| Archived, cleared or retrospective records only | Neutral; these do not imply current publication |

An archived period's `published_at` cannot turn a new draft green. A published
schedule with an unpublished revision cannot appear wholly published. Generated
zero-shift weeks still count as generated using `generated_at`.

## Query and freshness

`GET /v2/scheduling/api/calendar-weeks?start=YYYY-MM-DD&end=YYYY-MM-DD` uses existing
feature/board access dependencies and store-scope validation. Ranges are limited
to 63 days. One query projects period date/status/generation metadata and an indexed
shift-existence predicate. It never loads complete schedules, assignments, employee
records or PTO. The existing unique `(schedule_period_id, id)` shift index supports
the existence predicate.

The response uses `Cache-Control: no-store`. The dialog reuses month results for
up to 30 seconds during a visit and clears them on reopening, page restoration,
return to the visible tab and board updates. Existing generation/approval/publication
flows either reload the page or update the board. Stale month responses cannot
repaint a newer month. A failed lookup announces unavailable status while keeping
navigation usable; unknown weeks are not mislabeled as not generated.

## Verification

- Relevant regression run: **441 passed** (407.50 seconds).
- Final focused calendar run: **5 passed** (overlaps the regression run).
- Node test run: **5 passed**.
- Chrome browser acceptance: passed, zero JavaScript errors and zero scheduling
  mutation requests; includes navigation after a simulated status-query failure.
- `git diff --check` and JavaScript syntax check: passed.
- Warm local snapshot query: one query, five weeks, **1.30 ms**. This is a local
  measurement, not a production latency guarantee.


Committed tests: `tests/test_scheduling_calendar.py` and
`tests/js/scheduling_calendar.test.cjs`. The Python tests cover one-query metadata
projection, status combinations, empty/manual drafts, archived publication,
historical entries, invalid ranges, access guards, existing arrow targets, empty
navigation without generation, and Christmas closure preservation. JavaScript
checks cover leap years, month edges, year crossover, DST dates and stale revision
removal while retaining store filters.

Browser acceptance used Chrome at desktop 1440px and mobile 390px/320px widths.
It checked keyboard movement, Escape/focus return, year navigation, date selection,
whole-week status, adjacent arrows, empty-week loading and absence of mutation
requests. Screenshots were visually inspected.

The existing local production snapshot was restored to an isolated PostgreSQL 18
database. Its December 20–26, 2026 schedule remained unpublished/yellow and all four
December 25 Christmas closures remained visible. After browser navigation, 31
protected scheduling, employee, PTO, business-hours and permission tables matched
a separate restore exactly. The existing board refresh updated only derived
`schedule_warnings`; the new metadata endpoint does not refresh warnings.

Detailed logs, screenshots, query timing and preservation hashes are in the local
workspace's `output/scheduling-week-navigation-2026-10-20/` review directory.
The snapshot is existing evidence, not a fresh production audit. No schema migration,
engine, assignments, allocations, PTO, publication rules, locks, hours exceptions,
permissions or approval/regeneration policy were changed.
