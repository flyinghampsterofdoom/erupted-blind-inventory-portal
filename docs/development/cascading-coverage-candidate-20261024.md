# Cascading coverage — final candidate review

The approved corrections are implemented locally. This candidate includes the
previous call-out/coverage redesign plus the two corrections below. No production
deployment, migration, schedule change, employee edit, or external notification
has been performed.

## Corrections and root causes

1. The simplified form previously required an attendance status, so using it to
   relieve Cameron incorrectly recorded an absence. It now offers **Covered /
   Reassigned**, defaults to it for unmarked shifts, and serializes it as a null
   attendance event. Existing coverage APIs already support independent coverage
   records. A genuine existing call-out remains selected and is preserved even
   when a neutral coverage update is submitted explicitly. Cancellation removes
   the coverage assignment without erasing the independent absence.
2. Published warning evaluation compared historical source assignments and then
   repeated overlap checks against those same sources in policy evaluation.
   Published overlap warnings now compare the shared effective intervals and
   omit the duplicate source-based overlap result. Draft warning behavior and
   unrelated policy warnings retain their existing calculations. Original cards
   and publication records remain intact.

No new approval flow, confirmation dialog, mandatory note, permission system,
dependency, environment setting, or scheduled job is introduced.

## Exact acceptance scenario

Disposable local PostgreSQL databases contain synthetic Micah, Cameron and
Mikey employee identities with authenticated Store accounts, plus an authorized
Lead. The test uses real login cookies, CSRF, middleware, routes, SQL and backend
authorization. Only the operational clock is fixed at noon within the tested
shift; authorization is not mocked. Four parameterized cases exercise both the
coverage-only API and the actual JavaScript form serializer, each with both
cancellation orders.

The initial shifts are 8:45 AM–10:15 PM, with zero unpaid break: 13.5 hours each.

| Stage | Micah | Cameron | Mikey | Effective total |
| --- | --- | --- | --- | --- |
| Initial | Longview, 13.5h | Andresen, 13.5h | Off, 0h | 27h |
| Micah calls out; Cameron covers | Absent, 0h | Longview + Andresen, 27h; warning allowed | Off, 0h | 27h |
| Mikey covers Cameron | Absent, 0h | Longview, 13.5h | Andresen, 13.5h | 27h |
| Cancel Mikey first | Absent, 0h | Both stores, 27h; overlap returns | Off, 0h | 27h |
| Then cancel Cameron | Absent, 0h | Andresen, 13.5h | Off, 0h | 13.5h |
| Alternatively cancel Cameron first | Absent, 0h | Off, 0h | Andresen, 13.5h | 13.5h |
| Then cancel Mikey | Absent, 0h | Andresen, 13.5h | Off, 0h | 13.5h |

The completed chain has one effective worker per store, both purple coverage
cards, retained original cards, no Cameron absence, and no phantom overlap.
Micah's independent call-out remains active through all reversals. The final
13.5h after both cancellations is intentional: Longview remains uncovered while
Micah is absent.

Actual backend checks include allowed inventory reads and persisted chore
writes at the effective stores, rejection of cross-store operations, rejection
of stale store contexts, and denial of administrative users/sessions pages.
The same login sessions recognize every transition. Employee roles and home
stores do not change. Coverage does not assert actual work. Source assignment
columns remain unchanged, and both decisions and cancellations retain actor,
timestamp, original/replacement employee, source/store, interval and audit data.

Focused regressions also verify that neutral updates preserve existing genuine
absences, and that partial relief eliminates historical overlap while retaining
real overlap for the original employee's remaining interval.

## Validation evidence

| Suite | Result |
| --- | --- |
| Coverage backend/HTTP, effective projections, migration, exceptions, clear, employee context, Lead scope, sessions, company schedule and cascade | 197 passed |
| Full scheduling foundation, canonical/schema migrations, schema contract, Square write/cost boundaries and ordering routes | 374 passed |
| Lead duty, digital signage, employee access and access-controls regressions | 84 passed, 1 skipped |
| Clean locked-dependency environment: exact cascade, HTTP, projection and operational access | 31 passed |
| JavaScript scheduling/editor/query/session suites | 17 passed |

The clean run repeats 30 cases and adds the focused partial-overlap case.
All four cascade variants pass 110 assertions each. The sole skip is the
pre-existing private hosted R2 integration, which requires explicit external
credentials; no coverage acceptance case is skipped. Three existing framework
deprecation warnings remain. The full repository suite was not rerun in this
correction turn; the earlier redesign sweep is documented separately.

The shared historical test environment has unrelated system-package conflicts.
A fresh Python 3.10.13 virtual environment installed both checked-in hashed
dependency locks, passed `pip check`, and passed the 31-case clean rerun. Python
compilation, JavaScript syntax and `git diff --check` pass.

Evidence directory:
`/Users/justinrawlinson/Developer/Erupted/output/cascading-coverage-candidate-20261024/`.
The final counts and immutable revision are recorded in the packaged
`candidate-review.md` and `manifest.json`. JUnit XML, command logs, four detailed
acceptance JSON records and a browser screenshot accompany the candidate.

The browser screenshot verifies the neutral default inside the existing form.
The authenticated acceptance suite separately verifies its real serialized
payload against the backend and both cancellation orders.

## Deployment review and migration

The source candidate is based on `5c7112913a5c8037887fb0739b23d1fe9b5c61fd`.
The actual production revision and database head must be rechecked at deployment
time; this local work did not inspect or alter production. Use the committed
revision and source digest in the manifest, rather than a moving branch name.

The whole redesign requires migration `20261021_0048` → `20261024_0049`: additive
LEFT_EARLY enum support and nullable coverage start/end/break fields with check
constraints. Existing null intervals preserve full-shift meaning. The two
cascade corrections themselves require no additional migration. Fresh upgrade,
populated migration preservation, and guarded downgrade tests use disposable
local databases.

After separate deployment authorization, follow the existing release scripts:
verify the current deployment/schema and backup, build the exact candidate with
the hashed Python 3.10.13 dependency lock, run the existing migration entrypoint
to the candidate head, deploy matching source, and verify runtime identity,
schema readiness and normal operational health. Do not seed acceptance data in
production. There are no Square policy, forecast artifact, cron or environment
changes in this candidate.

Rollback must preserve coverage and attendance history. The old 0048 application
cannot simply be redeployed against 0049 because of its schema gate. The
downgrade intentionally refuses when partial coverage or LEFT_EARLY data would
be lost; voided records also remain historical evidence. Prefer a reviewed
0049-compatible forward fix if an issue appears after new records exist. Do not
drop history or restore a stale database to force rollback. Any full-release
rollback must have a separately reviewed compatible source/schema plan.

The packaged Git bundle, source archive, review patch, release identity and
checksums are local review artifacts. Packaging is not deployment authorization.
