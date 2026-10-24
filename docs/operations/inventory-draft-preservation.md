# Inventory draft preservation — local review candidate

No deployment or production migration is included. This candidate is based on production revision `21c3c53e0ea094a93e1d42c65e53501bbd733406`.

## Problem and resulting behavior

A count can outlive browser authentication or its working-store context. Previously, failed probes could stop autosave; same-tab recovery could be overwritten by a blank refreshed page, disappear on tab closure, or expire after 24 hours. A context fingerprint changed by a deployment could block an employee who still had access to the same store.

Blind counts/recounts, non-sellable stock takes, change-box counts, and daily chore sheets now protect each edit in encrypted browser storage, then save to the server. Entry is disabled until authenticated recovery and browser storage are available. Refresh and a new tab on the same browser profile can restore protected work automatically when its server revision matches. Different revisions or multiple pending tabs require review; they never silently replace newer server values. Both controllers also reject a changed revision between the HTML response and the recovery-key response.

Authentication or actual store/account changes hide quantities and derived row information while retaining the protected draft. A harmless version change for the same authenticated principal and authorized store updates the request context in place. The normal capability, active-account, employment, schedule, coverage-interval and transactional store checks still apply. Coverage never extends because a draft exists.

The browser persists exact in-flight draft/save operations and final-submit intent before requests. Draft retries use the same operation identity. Committed submissions have stable authorized receipts. Refresh after a lost submission response opens the original record, not a new stock take. Duplicate final commands return the existing receipt without applying business changes again. A receipt for somebody else's different work does not acknowledge or erase this browser's additional entries; authorized read-only recovery remains available from the receipt page.

Statuses distinguish device protection, server acknowledgment, pending authentication/access, and unconfirmed submission. Successful submission opens a durable receipt with the original store, record and submission time. Legacy autosave no longer forces navigation to login with unfinished work.

## Authorization and browser storage

Recovery keys are derived using HMAC-SHA256 with a domain containing the authenticated principal, original store, workflow and record ID. The endpoint independently resolves current schedule/capability authorization and the record's store. Server-derived identity must match the page's original identity. All key/state/receipt responses are `no-store`.

Browser records use AES-GCM with a fresh nonce and authenticated scope; neither plaintext quantities nor keys are persisted. Writes serialize so that delayed encryption cannot overwrite a newer edit. Each page has its own record; adopting a previous record replaces it only after a durable write, and deletion compares its exact ciphertext. Submission cleanup removes only the acknowledged page/adopted record. Other tabs' uncertain work is retained. Corrupt or undecryptable records fail closed and are not automatically deleted.

Keys are released only after authenticated original-store authorization, including for read-only review of the same account's local unconfirmed entries after a count closed. This returns no historical server quantities, expected stock or variances. Receipt review uses labels only and the account's own encrypted device values. Another account gets a different key even if it can work at the same store. Account/store changes hide an opened review again. Already disclosed information cannot be made unknown to a person; these controls govern new access, restoration and submission.

Existing same-tab buffers migrate only after an authorized load and confirmed encrypted persistence. There is no silent 24-hour deletion. Unauthorized restoration cannot move a draft to a different store or grant inventory permission.

If browser storage fills while authentication expires, the most recent unpersisted edit is retained privately in the live controller and hidden from the page until the original account/store is revalidated. The page explicitly requires keeping the tab open. No browser application can guarantee recovery after the only unsynced device copy is physically destroyed, browser data is cleared, or a process is forcibly terminated before persistence. The UI does not label such work saved.

## Backend and preservation

No schema change or historical rewrite is needed. Existing stock, stock takes, count observations and audit records are retained. New authorized legacy form saves append operation/digest/version evidence to the existing audit entry. Record locks serialize replay checks and mutations; versions use the actual PostgreSQL-trigger timestamp normalized to UTC. Older clients retain their prior response shapes and CSRF requirements. The upgraded controllers request JSON acknowledgments to avoid following submission redirects into creation of another draft.

Blind-count observation deduplication and Square inventory correction idempotency remain in their existing services. No Square catalog, vendor, price, cost or identity writes are introduced. Receipt recovery never initiates a Square write.

## Deployment prerequisites and limits

Before any separately authorized deployment:

1. Provision and retain a strong, stable `INVENTORY_DRAFT_ENCRYPTION_KEY` (at least 32 characters). This dedicated secret takes precedence over the existing integration/application secrets. Strong existing secrets can provide compatibility, but the public default is rejected with HTTP 503 and entry remains disabled. Do not log the secret or put it in source control.
2. Keep the key stable across logins, releases, cookie/token changes and restarts. Key rotation needs a plan for unresolved device drafts and retention of the prior key; automatic key-ring rotation is not implemented in this candidate.
3. Verify HTTPS/secure browser crypto and device storage, the actual deployed source identity, and an employee's temporary Longview coverage. Test refresh, reconnect, reauthentication in another tab, denial after reassignment, duplicate retries, and a lost final response before opening rollout.
4. Preserve old browser data and obtain a server draft acknowledgment before refreshing an already-open old client where possible. New code cannot upgrade an already-open old JavaScript controller without a page load. The authorized migration preserves its same-tab buffer on that load.

Scope is the four workflows listed above. Purchase-order receiving has its own controller and is not changed by this candidate. This work does not claim a completed durability audit of every other operational form.

## Verification

The investigation's original production evidence remains outside Git. Regression tests use disposable synthetic local PostgreSQL databases and mocked Square providers; browser acceptance uses a separate synthetic local server.

- Controller tests cover temporary-store version refresh, expiration/reauthentication, true reassignment/account change, offline exact-command retries, interrupted final submission across refresh, duplicate clicks, concurrency, revision reconciliation, quota failure, and prevention of blank-buffer overwrite.
- Crypto tests use real WebCrypto, including tampering, wrong keys/accounts, serialized writes, cross-tab cleanup, and a large inventory payload.
- Receipt tests verify authorized review and preservation of an unconfirmed second tab.
- HTTP/database tests verify key/receipt scope, no-store responses, weak-secret denial, immutable observation deduplication, stable legacy-form replay and original-record receipts, plus the synthetic Longview coverage/expiry/reassignment scenario.

Run:

```sh
node --test tests/blind_count_controller_test.cjs tests/inventory_draft_crypto_test.cjs \
  tests/inventory_form_drafts_test.cjs tests/inventory_receipt_test.cjs
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://localhost/postgres python -m pytest -q \
  tests/test_inventory_durable_drafts.py tests/test_blind_count_reliability.py \
  tests/test_blind_count_controller.py tests/test_coverage_operational_access.py \
  tests/test_employee_operational_context.py tests/test_cascading_coverage_acceptance.py \
  tests/test_longview_count_incident.py tests/test_recount_logic_service.py \
  tests/test_square_write_boundary.py tests/test_v2_daily_store_logs.py
```

Private logs, screenshots and acceptance receipts are kept in the enclosing workspace's `output/inventory-durable-drafts-20261024/` directory.


### Recorded acceptance — October 24, 2026

- 96 Python regression checks passed across inventory reliability, coverage/access changes, cascading coverage, the synthetic Longview incident, recount logic, Square policy and daily-store logs.
- 47 JavaScript controller, real-WebCrypto, generic-form and receipt checks passed on the final controller source.
- A final targeted run passed nine HTTP/controller checks after the receipt review, rendered-version guard, dedicated-key and no-store additions.
- A real local browser verified expired-session locking and same-account reauthentication, count save/refresh/submission, non-sellable stock-take save/refresh, and the persistent submission receipt. Only synthetic records were used.
- No production data, deployed source, provider settings or schema were changed. Three existing FastAPI/Starlette deprecation warnings remain in test output.
