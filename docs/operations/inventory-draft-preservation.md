# Inventory draft preservation and disaster recovery

Release qualification is required before deployment. The original candidate was based on production revision `21c3c53e0ea094a93e1d42c65e53501bbd733406`.

## Problem and resulting behavior

A count can outlive browser authentication or its working-store context. Previously, failed probes could stop autosave; same-tab recovery could be overwritten by a blank refreshed page, disappear on tab closure, or expire after 24 hours. A context fingerprint changed by a deployment could block an employee who still had access to the same store.

Blind counts/recounts, non-sellable stock takes, change-box counts, and daily chore sheets now protect each edit in encrypted browser storage, then save to the server. Entry is disabled until authenticated recovery and browser storage are available. Refresh and a new tab on the same browser profile can restore protected work automatically when its server revision matches. Different revisions or multiple pending tabs require review; they never silently replace newer server values. Both controllers also reject a changed revision between the HTML response and the recovery-key response.

Authentication or actual store/account changes hide quantities and derived row information while retaining the protected draft. A harmless version change for the same authenticated principal and authorized store updates the request context in place. The normal capability, active-account, employment, schedule, coverage-interval and transactional store checks still apply. Coverage never extends because a draft exists.

The browser persists exact in-flight draft/save operations and final-submit intent before requests. Draft retries use the same operation identity. Committed submissions have stable authorized receipts. Refresh after a lost submission response opens the original record, not a new stock take. Duplicate final commands return the existing receipt without applying business changes again. A receipt for somebody else's different work does not acknowledge or erase this browser's additional entries; authorized read-only recovery remains available from the receipt page.

Statuses distinguish device protection, server acknowledgment, pending authentication/access, and unconfirmed submission. Successful submission opens a durable receipt with the original store, record and submission time. Legacy autosave no longer forces navigation to login with unfinished work.

## Authorization and browser storage

Recovery keys are derived using HMAC-SHA256 with a v2 domain containing permanent account, store and stock-take UUIDs plus workflow. Numeric IDs are routing hints, never recovery identity. Protected save, state and submission requests validate the original UUIDs, including under the blind-count transaction lock. Restored/recreated records receive fresh random UUIDs and cannot accept older drafts. The endpoint independently resolves current schedule/capability authorization and the record's store. Server-derived identity must match the page's original identity. All key/state/receipt responses are `no-store`.

Browser records use AES-GCM with a fresh nonce and authenticated scope; neither plaintext quantities nor keys are persisted. Writes serialize so that delayed encryption cannot overwrite a newer edit. Each page has its own record; adopting a previous record replaces it only after a durable write, and deletion compares its exact ciphertext. Submission cleanup removes only the acknowledged page/adopted record. Other tabs' uncertain work is retained. Corrupt or undecryptable records fail closed and are not automatically deleted.

Keys are released only after authenticated original-store authorization, including for read-only review of the same account's local unconfirmed entries after a count closed. This returns no historical server quantities, expected stock or variances. Receipt review uses labels only and the account's own encrypted device values. Another account gets a different key even if it can work at the same store. Account/store changes hide an opened review again. Already disclosed information cannot be made unknown to a person; these controls govern new access, restoration and submission.

Existing same-tab buffers migrate only after an authorized load and confirmed encrypted persistence. There is no silent 24-hour deletion. Unauthorized restoration cannot move a draft to a different store or grant inventory permission.

If browser storage fills while authentication expires, the most recent unpersisted edit is retained privately in the live controller and hidden from the page until the original account/store is revalidated. The page explicitly requires keeping the tab open. No browser application can guarantee recovery after the only unsynced device copy is physically destroyed, browser data is cleared, or a process is forcibly terminated before persistence. The UI does not label such work saved.

## Backend and preservation

Migration 20261024_0050 adds unique UUID identities to counts, stock takes, cash counts, chore sheets, admin counts, accounts and stores. Database triggers prohibit identity updates. Existing business columns are unchanged. A compatibility downgrade retains these additive columns, defaults, constraints and triggers; re-upgrade validates and reuses them. Never drop or regenerate identities during recovery. No historical quantity or audit rewrite is needed. Existing stock, stock takes, count observations and audit records are retained. New authorized legacy form saves append operation/digest/version evidence to the existing audit entry. Record locks serialize replay checks and mutations; versions use the actual PostgreSQL-trigger timestamp normalized to UTC. Older clients retain their prior response shapes and CSRF requirements. The upgraded controllers request JSON acknowledgments to avoid following submission redirects into creation of another draft.

Blind-count observation deduplication and Square inventory correction idempotency remain in their existing services. No Square catalog, vendor, price, cost or identity writes are introduced. Receipt recovery never initiates a Square write.

## Keys, rotation and disaster recovery

Production uses `INVENTORY_DRAFT_ENCRYPTION_KEYS`, a JSON object mapping stable key IDs to base64-encoded 32-byte random master keys, and `INVENTORY_DRAFT_ACTIVE_KEY_ID`. Store them in the existing restricted Render secret environment and preserve a separately access-controlled recovery copy. Never place real keys in Git, command arguments, reports, browser storage or logs. The dedicated single-key setting is a compatibility path; there is no integration/application-secret fallback in v2.

Every instance must have the same ring and active ID. A deployment/restart does not create keys. Ciphertext records contain format version, key ID and immutable identity scope; labels, quantities and operation intents are encrypted with AES-GCM and fresh nonces. Only scoped derived keys are released after original-account/current-store authorization. Authenticated associated data binds immutable identity and key ID. Master keys never reach the browser.

To rotate: generate a new random 32-byte master in the secrets-management process; add it with a new ID while retaining all existing versions; distribute the complete ring to all instances; then switch the active ID. Verify authorized old-draft decryption and new writes on every instance before declaring rotation complete. Opening and saving an old draft reencrypts it with the active key before removing the adopted ciphertext. Offline browser copies cannot be exhaustively enumerated, so retain historical decrypting keys indefinitely unless an explicitly audited recovery/retirement decision accepts the loss. Simply replacing the only key is prohibited.

A missing/malformed ring disables entry. A missing historical key retains the ciphertext, quarantines the work and disables automatic recovery. Restore the exact historical key under the same ID, then reauthenticate and retry. Never replace a key's bytes under an existing ID.

Before activation, verify a production backup and isolated restoration. Back up again after identity migration and before activation so the recovery set includes permanent account/store identities. Retain the corresponding key ring independently of the database. A database backup alone cannot recover keys. A pre-identity backup requires restoring the trusted identity-bearing recovery set before employee recovery is available; fail closed rather than assigning new account/store identities to old ciphertext.

After restoring an older database with newer browser drafts, original UUIDs still present in the backup recover normally. Missing or recreated counts cannot adopt those drafts, even if numeric IDs and revisions match. The **Review protected work on this device** link opens `/store/draft-recovery`, which works without the original count row. Fresh original-account and currently authorized-store checks permit read-only review of the employee's own retained ciphertext and encrypted labels. Recovery never creates a count or offers an import/submit action. A Lead must establish provenance before any separate authorized, audited reconciliation. If the account/store identity itself is missing, restore its trusted identity-bearing backup; do not change identities to satisfy a draft.

Legacy buffers without permanent identities are never imported automatically. Their encrypted quarantine is retained with unverified provenance; employee review of that legacy payload is blocked pending administrator verification. Existing plaintext same-tab buffers are removed only after encrypted quarantine persistence succeeds. Old v1 encrypted records remain retained; their numeric-only identity cannot be trusted after restore.

## Release and rollback

1. Complete all regression, adversarial restoration, restart, rotation, authorization and substantial-count browser gates. Incorporate current main's CI and require its checks; never override branch protection for this release.
2. Preserve the production database, audit/quantity fingerprints, provider configuration, previous immutable artifact, secret ring and forecast advisory artifact. Rehearse migration and compatibility rollback on an isolated restored backup; prove existing business columns and historical audits match.
3. Provision stable keys only through existing secret configuration. Confirm all instances agree without logging values. Apply the additive migration before activation and verify a post-migration identity-bearing recovery backup.
4. Verify HTTPS, browser crypto/storage, actual deployed revision and temporary Longview coverage. Use isolated authorized synthetic fixtures; never edit an employee's completed stock take.
5. For rollback, preserve every v2 ciphertext and every retained key. The previous application ignores the additive UUID columns; migration compatibility downgrade retains them. Never restore an older database as routine application rollback. Re-upgrade reuses identities. The previous UI cannot serve v2 recovery; keep the verified new recovery service available or pause inventory counting until rollforward. Do not silently return users with pending protected drafts to an older counting UI. Validate this operational path before release.
6. Monitor auth/context denials, draft persistence failures, submission errors, duplicate retries/receipts and latency. Critical integrity, authorization or recovery failures stop activation.

The four covered workflows are blind counts/recounts, non-sellable stock takes, change-box counts and daily chore sheets. Purchase-order receiving has a separate controller. Protected device drafts do not move to another device automatically. No browser can recover an unacknowledged copy after its sole device storage is destroyed; storage failure must remain visible and must not be called saved.

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

### Identity correction qualification

`tests/test_inventory_identity_recovery.py` performs actual pg_dump/pg_restore with newer offline ciphertext, reused sequence IDs, permanent identity update rejection, authorized orphan review, retained/missing keys, independent application processes and identity-preserving compatibility rollback. Real WebCrypto tests use 1,000-product payloads and verify retained exact ciphertext on rejection. Longview coverage runs with both one and 1,000 products. Production release status is recorded separately; local test success is not a deployment receipt.
