# Scheduling base-pattern cleanup

Implementation prepared for review; not deployed. No live schedules or employee
profiles were edited or regenerated.

## Semantics

An A/B pattern with no selected days means no base-day preference. Profile writes
normalize mask `0` to SQL `NULL` through the shared service used by both the form
and API. Legacy stored zeros remain valid and are interpreted as unconfigured by
base-pattern reads, scoring, annotation, labels, and readiness checks. Nonempty
masks (all 127 possible combinations) retain their selected days and existing soft
preference behavior. An unconfigured base pattern is not an availability lockout.

No migration is required: the columns already accept NULL and zero. Existing data
is not rewritten in bulk. Existing stored diagnostics remain available until a
normal generation or relevant mutation updates them.

## Presentation and diagnostic lifecycle

Both server-rendered and JavaScript-rendered shift cards omit all `Base exception`
text. There are no replacement explanation badges. Existing warning, manual-lock,
Lead, and Double Coverage indicators remain. The serializer and generation audit
still expose base-pattern diagnostic metadata for investigation.

Manual shift edits (including assignment and move), lock/unlock operations,
Double Coverage employee overrides, and completed transfers clear the affected
shift's old base-pattern expectation and reason. They do not invent a replacement
explanation. Subsequent generation can derive fresh diagnostics. Existing manual
lock state, reason, actor, and timestamp continue using their established fields.

No hard constraints, weekly-target completion, lead requirements, fairness rules,
publication rules, or Square integrations were changed.

## Review considerations

Normalizing zero changes its soft base score from -1 to 0, matching NULL. Therefore
future regeneration can select different legal assignments. A controlled replay
of the anonymized Oct 11 fixture isolates that effect: workers 2 and 9 exchange
Sunday stores 2 and 9; the other 26 positions, target totals, covered requirements,
and three-position repair remain intact. Regression coverage checks the same
final plan for legacy-zero and persisted-NULL inputs and checks repeat generation.

Readiness already reports an unconfigured base pattern as a nonblocking notice;
legacy zero now receives that same existing notice. No new warning type was added.

The test suite also had a date-dependent automation-page fixture that called an
August 30 draft upcoming while using the real clock. That test now fixes its
planning date to August 23. Production time and publication behavior are unchanged.

The JavaScript rendering regression uses Node as a test-only dependency. No
application dependency was added.

## Validation

Focused regressions cover NULL, legacy zero, all nonempty masks, form save/clear,
base scores, readiness, stored diagnostic clearing, both card renderers, manual
edits/moves/reassignments/locks, transfers, Double Coverage overrides, and Taylor's
required one-shift completion (including legal extra coverage and blocked cases).

Integration runs use disposable local PostgreSQL databases, an unreachable default
application database URL, an empty Square token, and the mock snapshot provider.
The broader run excludes the two scheduling test modules, which are run separately.

Final results:

- Scheduling foundation and repair suites: **338 passed**.
- Remaining repository tests: **682 passed, 1 skipped; 7 subtests passed**.
- JavaScript syntax check and `git diff --check`: passed.
- The only reported warnings were existing FastAPI startup-event deprecations.

Commands (with the local test environment described above):

```sh
python -m pytest -q -rs tests/test_v2_scheduling_foundation.py tests/test_v2_scheduling_repair.py
python -m pytest -q tests --ignore=tests/test_v2_scheduling_foundation.py --ignore=tests/test_v2_scheduling_repair.py
node --check app/static/v2/scheduling.js
git diff --check
```
