# Blind Count Reliability / Automatic Correction / Lead Review

This implements the owner's approved sequence: three independent matching nonzero variances → automatic inventory correction → mandatory Lead review. No deployment or live Square write was performed during validation.

## Count entry and evidence

Employees enter Front Stock and Back Stock; both must be explicitly present, including deliberate zero. The browser calculates Total immediately and the server calculates the saved total. Incomplete rows are valid drafts and cannot be submitted as physical observations. Invalid fields do not prevent valid fields from being saved, but must be corrected before submission/logout.

The employee query selects only current-round product identity, section label and Front/Back draft values. It does not select expected inventory, prior counts, variance, streaks or correction targets. The page is `no-store`. Submission responds with a generic submitted acknowledgment. Recounts use new sessions; concurrent Generate requests serialize by store and resume an existing draft. Submitted rounds cannot be unlocked, re-counted through replay, or purged through the management delete action.

Each immutable observation retains session/store/variation identity, SKU/name/variation, Front/Back/Total, nullable expected/variance, provider/location/fetch-time provenance, authenticated last-changing account and submitter, last-entry-save time and submission time. A database trigger rejects observation updates/deletes. Shared account attribution remains account-level; the system does not infer an individual from an entered name. Meaningful draft changes record before/after values, actor, revision and operation ID; repeated unchanged saves do not change the recorded counter/time.

## UNKNOWN and correction

Missing/malformed Square quantities remain UNKNOWN; an explicit quantity of zero remains zero. UNKNOWN observations retain the physical work, break the qualifying streak, remain on the recount queue, and are never re-compared with a later expected snapshot. A fresh physical round is required. Legacy queue metrics do not qualify as independent evidence.

The latest three immutable observations must belong to three distinct sessions and have the same nonzero variance. Qualification requires fresh observations after the last successful correction. A pending operation prevents another operation for that store/product. The service commits the observations and exact correction request before Square is contacted. Retry reuses the same operation/idempotency key, physical quantity, location and occurrence time. Concurrent submit/correction replay creates one observation and one external call in the integration test.

Only a confirmed successful Square request marks success and creates the OPEN Lead review. The ordinary recount requirement is removed without deleting the evidence or closing the review. A newer observed discrepancy is retained if an older pending correction later succeeds. Production execution requires Square-sourced evidence and `SNAPSHOT_PROVIDER=square`; mock evidence cannot write live inventory. Calls use the existing policy-enforced inventory client. The legacy fresh-push action is unavailable for new immutable rounds.

The initial correction attempt runs automatically after the third qualifying round's transaction commits. Failed/pending operations remain visible in Count Review. Replaying the submitted request or selecting **Retry prepared inventory correction** retries the saved operation; no new physical observation or approval is created. There is no new background retry scheduler in this change.

## Lead review

`/management/audit-queue` shows all correction/review states, including older OPEN reviews. ADMIN, legacy MANAGER and LEAD with management access can inspect the three qualifying observations, counters, times, provenance, Square payload/response and attempts. Success does not resolve a discrepancy.

Review requires an explanation and notes and records reviewer/time. Same-store corrections with the exact same product-family name are suggested as possible relationships; a reviewer can associate them. This is evidence grouping, not a determination of mis-ring, theft or misconduct. Existing reviewed evidence remains accessible. Product-family matching is intentionally limited to exact item names, not fuzzy relationship inference.

## Autosave, logout and sessions

The controller debounces edits, serializes saves and only marks the current generation Saved when acknowledged. Lost save responses replay an idempotent operation. Clears persist as NULL. Revision conflicts retain pending values and require reconciliation instead of overwriting another tab's work. The final-submit retry path handles a lost response without another draft write or another observation.

Intentional logout disables entry, flushes current values and waits for an acknowledgment; failure pauses logout. `/session-status` neither extends server inactivity expiry nor renews the session cookie. Background polling uses that endpoint. Pending edits are saved before a known expiry where possible. Expired authentication cannot save. Reauthentication requires the same account in the controller and server access/CSRF are checked again, including changed store scope.

Recoverable current-round values are scoped by account/store/session in `sessionStorage`, expire after 24 hours, and require explicit restoration after reload. Cross-tab conflict recovery is deliberate. Browser closure, power loss, disabled storage or complete network failure can still lose edits that never reached the server; this is not an offline persistence guarantee.

## Migration and deployment implications

Migration **20260922_0025**, parent **20260911_0024**:

- Adds nullable Front/Back fields and allows incomplete draft totals.
- Allows UNKNOWN expected inventory and recount variance; zero-length qualifying streaks are valid.
- Adds draft revision and closed-observation flags.
- Adds `count_observations`, `count_corrections`, `count_correction_attempts`, `count_reviews` and the observation-history index/immutability trigger.
- Preserves historical saved count totals without creating Front/Back splits. An untouched legacy draft keeps its old total; subsequent genuine entry changes retain the prior total in the change audit.
- Closes previously submitted/reopened historical sessions. Existing open drafts have UNKNOWN expected inventory until submission. Existing recount membership remains, while old mutable streak/attempt metrics reset to zero.

Use a controlled application migration/restart so old and new count writers do not overlap. Employees with legacy drafts must enter both stock areas before submission. Downgrade refuses to discard observations, Front/Back data, incomplete totals, UNKNOWN inventory, or zero/UNKNOWN recount state. An unused compatible legacy-only database can round-trip; once new evidence exists, use a forward repair instead of a lossy downgrade.

## Validation

39 focused tests passed across the PostgreSQL count/migration integration, browser controller and permanent Square boundary suites. The controller wrapper runs ten deterministic JavaScript behavior scenarios with DOM/network/timer doubles. HTTP tests render the real template and exercise authentication, CSRF, store reassignment and expired/new sessions. PostgreSQL tests include populated legacy upgrade, preserved totals, no invented splits, downgrade refusal, immutable evidence, sequential/concurrent replay, UNKNOWN streak breaks and independent review.

Tests used disposable PostgreSQL 16 databases on a temporary UTF-8 cluster and mocked Square transport. They do not establish deployed behavior or constitute live Square acceptance testing.
