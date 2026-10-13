# Scheduling pre-beta local acceptance

October 13, 2026. Isolated branch: `codex/scheduling-prebeta`, based on
`83b247976ef99e9923496211ef1134c203244889`.

## Implemented behavior

The main schedule board and review queue show the requested Green / Yellow / Red
publication status, with next operating week unpublished taking precedence. There
are no new escalation stages, incident records, acknowledgments or repeated alerts.
Generation leaves every new week unpublished. Leads can review, edit, approve and
publish without automation administration; approval binds to the draft version.

Publication revalidates authoritative employee eligibility, date eligibility,
store restrictions, approved time off, overlap, consecutive days, hours, assignment
times and coverage. An audited availability override survives an unchanged revision
only when its assignment values still match. Hard violations cannot be waived by
changing warning permissions.

Rolling generation maintains complete future weeks, default X/Y 9/8, catches up
missing weeks, preserves existing periods and records results and sanitized failure
outcomes. Shift Swap supports direct replacement and two-way exchange. Store Swap
uses atomic joint validation, approved published revisions and correlated audit
history. Call Out preserves original assignments and expected replacement coverage.
Historical fairness uses elapsed coverage, preserves corrections and avoids counting
copied revisions or future reservations as additional completed work. Ordinary
replacement coverage does not confer company-wide Lead duty.

Blind Inventory resolves the linked employee's effective published working store,
including coverage commitments. Server checks and employee locks reject stale store
submissions and serialize races with schedule changes. The session retains its
account identity. Emergency Select Store is hidden until a Lead, Manager or Admin
authorizes a named employee/store with a reason and an expiry of at most twelve
hours. Scheduling changes invalidate older grants. Existing session selection is
reused; published schedules, permanent store assignments and inventory counts are
not changed by an emergency selection.

Approved employee preference requests overlay existing preferences by their dates.
Temporary requests expire back to permanent or baseline preferences. Submission is
ineffective until Admin approval; decision and revocation history remains auditable.

## Automated verification

The focused workflow suite passed 29 tests before the final additional audited-
availability regression, which also passed. It covers publication invalidation,
versioned approval, nine-week generation and interruption recovery, status colors,
Call Out attribution, Store Swap reversal, invalid second-side rollback, direct
and two-way Shift Swap, preference dates/history, stale logged-in requests,
concurrent swaps and count submission racing a swap. HTTP checks exercise Lead
review/approval/publication, CSRF, employee visibility and permission denial.

A disposable PostgreSQL migration rehearsal upgrades and downgrades an empty
pre-beta extension while preserving existing X. Once new evidence is retained,
downgrade is rejected and schema 0032 remains intact. The complete migration suite
also verifies a fresh database and earlier populated upgrades.

Final full-suite result: **1,296 passed, 1 skipped, 7 subtests passed** in
594.13 seconds. The skip is the optional private R2 integration test. Seven
additional invalid X/Y cases passed separately after full-suite collection.
The full run reported dependency deprecations and an unawaited coroutine in the
navigation-capability test helper; the helper was corrected and its focused test
rerun. Runtime assignment checks have separate HTTP acceptance coverage.

Raw results: [full suite](evidence/pytest-full.txt), [X/Y validation](evidence/pytest-horizon-config.txt), and [navigation capabilities](evidence/pytest-route-guards.txt).

Both changed JavaScript files pass `node --check`; `git diff --check` passes.
Tests use disposable local PostgreSQL databases and mock providers. No real Square
writes or production database access are part of this acceptance run.

## Browser verification

A local preview on loopback used synthetic Alex/Blair identities and a synthetic
count. Browser actions verified generation, Lead approval and publication, same-day
Store Swap, a retained published revision, employee schedule visibility, and a
reasoned emergency store authorization. Normal inventory changed to South after
the swap; explicit emergency selection opened North and its original draft count.
The main board and Lead review queue display Red when the next operating week is
unpublished. Admin automation settings are absent from the Lead review queue.

Screenshots are local synthetic acceptance evidence:

- [Publication continuity](evidence/scheduling-continuity-local.jpg)
- [Authorized temporary inventory store](evidence/emergency-store-local.jpg)

## Release limits

No production deployment, scheduling record edits, configuration edits, recurring
job activation or provider mutations were performed. The canonical checkout is
unchanged. Production identity links, live provider behavior and infrastructure
configuration require separate deployment acceptance. Production coverage must be
reviewed for one required employee per operating store/day; the local migration
preserves existing operational configuration rather than rewriting it.

Schema 0032 requires matching web and job artifacts. After new evidence or a
nine-week policy exists, use compatible recovery code or a reviewed forward fix;
the migration deliberately refuses destructive downgrade. The implementation plan
lists the separately authorized release prerequisites.
