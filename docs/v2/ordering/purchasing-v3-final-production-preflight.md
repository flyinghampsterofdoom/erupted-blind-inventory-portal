# Purchasing V3 final production preflight

2026-10-18 — **Original preflight evidence; PDF preservation addendum now governs readiness.**

This is preparation evidence and a proposed runbook, not deployment authorization. No production deployment, migration, record/configuration change, purchase, payment, feature activation or Square mutation was performed. Production inspection used Render HTTP GET and bounded PostgreSQL READ ONLY transactions. All destructive rehearsals used isolated local synthetic databases.

**Current status:** the [final hosted storage verification](purchasing-v3-final-hosted-storage-verification.md) closes both hosted checks and records remaining configuration gates. See [PDF preservation correction](purchasing-v3-pdf-preservation-correction.md) for the bounded local code correction, shared R2 requirements, independently verified file backups, and remaining deployment prerequisites. The original readiness conclusion and R2 exception below predate this correction; they are not the current deployment gate. Owner principal 6 has since been verified and confirmed. No deployment is authorized by this document.

## Integrated release

The local release includes the approved recommendation engine, editable store allocations, supplier-group PO conversion and PDFs, direct store receiving with subsequent partial shipments, immutable receipt evidence, Monday payments, current consignment inventory valuation, Needs Attention, Payment Methods, and balance-proportional card allocation. Existing production source is integrated from `529df6f566b780e2e8ab5d470c160fb481d06715`. The final Git commit and tree are recorded in `output/purchasing-final-preflight/release-revision.json`; this document is part of that release. Nothing was pushed.

All 44 deployed migration files remain byte-identical. All 75 compared production scheduling, authentication/security/access, inventory working-store and release/cron paths remain byte-identical. Both deployed services were rechecked at production commit `529df6f…`, schema `20261016_0037`, PostgreSQL 18.3. Existing Render auto-deploy is off. The web and cron currently follow different branch names; deployment must pin both to the same reviewed release SHA.

| Approved area | Evidence / result |
|---|---|
| Purchasing / allocations / supplier POs / PDFs | PASS — existing Phase 2, 500-SKU UI, workflow and accounting regression tests |
| Direct store receiving / partial shipments / audit | PASS — receiving extension, three-shipment, concurrent/retry and original-document regression tests |
| Monday Payments / actual balance authority | PASS — atomic retries/corrections/checkpoints and configurable-account tests |
| Consignment Inventory Value | PASS — current Square stock × authoritative COGS; vendor credits/payments do not affect it |
| Needs Attention | PASS — 36-item fixture, costs, individual/bulk persistence, permissions, stale/concurrent decisions and nonfabrication |
| Payment Methods | PASS — Admin configuration, role/name changes, new masked account, prospective vendor defaults, historical preservation |
| Card recommendations | PASS — one pooled weekly card COGS amount divided by positive actual issuer balances; cent residual assigned to Card 2 |
| Production V2 / Square boundary | PASS within relevant regressions and source comparison; no integration policy bypass |

## Payment and configuration requirements

Confirmed Card 1 is **Chase Visa, existing funding account 2**, linked to active card method 1. Initial configuration is `PURCHASING_CARD1_ACCOUNT_ID=2`; no source-code identity constant or new account is needed. Leave `PURCHASING_CARD2_ACCOUNT_ID` absent. No second account or balance was invented. An unconfigured Card 2 displays **Needs Attention — awaiting configuration**, leaves recommendations unavailable and prevents Save Payments. The Admin can add/select its real record through `/v2/payment-methods` later; only the owner may enter actual issuer-balance checkpoints via the existing funding-account page.

For positive actual issuer weights W1=max(B1,0), W2=max(B2,0), Card 1 recommendation is round-to-cent-half-up(P × W1/(W1+W2)); Card 2=P−Card 1. If either balance is unknown or both positive weights are zero, the screen requests owner input. Vendor default assignments do not influence these weights. Big Wholesale/Streamline use their separate weekly sales COGS. Actual amounts are independently editable; one atomic/idempotent Save Payments remains. Role changes invalidate old server save tokens and require review before a browser draft can apply to changed accounts. Saved weekly identities/calculations and existing financed lots are preserved.

Activation configuration, applied only after authorization:

| Setting | Reviewed value / treatment |
|---|---|
| `PURCHASING_OWNER_PRINCIPAL_ID` | Owner must confirm identity; production principal **6** is an active ADMIN candidate, not an assumed owner. **Set the confirmed ID before web cutover**, because receiving writers require the verified owner even while the new interface is hidden. |
| `PURCHASING_BIG_SQUARE_VENDOR_ID` | `42STABRGTKEP4PSC` — Big account 1 / local vendor 40 |
| `PURCHASING_STREAMLINE_SQUARE_VENDOR_ID` | `JYKK2W5FJUPOMDPQ` — Streamline account 3 / local vendor 32 |
| `PURCHASING_CARD1_ACCOUNT_ID` | `2` — owner confirmed |
| `PURCHASING_CARD2_ACCOUNT_ID` | Unset until the owner provides the actual identity |
| `V2_ENABLED_FEATURES` | Preserve `v2_primary,staff_scheduling_v2`; append `order_payments_v2` for Payment Methods/funding-account navigation. Never replace the existing features. |
| `PURCHASING_V3_PHASE1_ENABLED` | Activate the sole actual-card authority at coordinated cutover; retain thereafter. |
| `PURCHASING_V3_PHASE2_ENABLED` | Activate approved owner purchasing/receiving interface after schema and web/cron checks. |
| `PURCHASING_MONDAY_PAYMENTS_ENABLED` | May activate the warning/read interface with Card 2 missing; recording remains blocked until four valid distinct accounts exist. |
| `SQUARE_READ_ONLY` | Retain `true`; preserve existing tokens, four location mappings and centralized request policy. This flag does not replace established inventory-writer protections. |
| `PYTHON_VERSION` / schema validation | Retain `3.10.13`; schema validation ON, no skip/stamp workaround. |
| Existing V2 COGS action flags | Record existing values (both true on web). Pause writes during cutover; restore only the reviewed guarded paths with actual authority active. No historical charge backfill. |
| Existing cron execution flag | Record true; gate/suspend only during the maintenance window, then restore existing hourly scheduling operation after verification. No purchasing automation or AI activation. |

Four active Square stores remain connected: Andresen local 2/LZH57Q25MGWS1; HWY 99 local 4/LV1SDYTZB2AYR; Longview local 6/LS4T7D19Y1WYR; SR503 local 9/L5Y41WDGQGWBC. Neither current credentials nor mappings were changed.

## Forward migration and rehearsal

Single forward graph (revision suffix numbering is not migration direction):

`20261016_0037 → 20261017_0032 → 20261017_0033 → 20261017_0034 → 20261017_0035 → 20261018_0038`

These are the existing approved additive Purchasing migrations: evidence/card checkpoints; receiving events; purchasing sessions; Monday payment batches; owner decisions. No migration was added for this preflight or Payment Methods. No destructive financial rewrite/backfill is part of upgrade. Legacy receipt event fields remain NULL; historical documents cannot be replayed as new receiving.

A private Unix-socket-only **PostgreSQL 18.6** cluster was created, seeded at production 0037, dumped in PostgreSQL custom format, restored into a second disposable database, and upgraded to 0038. All **178 pre-existing tables' row fingerprints matched** before backup, after restore and after upgrade; new nullable receipt columns were excluded when comparing old receipt row contents. Nonempty fixtures included historical partial payment/allocation, finalized funding report, legacy assignment ledger, audit, PO/line, partial receipt/line and schedule/approval evidence. Owner decisions remained empty: no migration invented financial choices. ASGI startup/login passed; the enabled cron entrypoint passed its strict schema gate with a local no-op scheduler; recovery startup with Phase 1 ON and Phase 2/Monday OFF passed. Both disposable databases were dropped and the temporary cluster stopped.

Fresh read-only production metadata matched the PostgreSQL 18 production-head reference semantically. Normalization is limited to the four previously documented absent V1 checks, NOT NULL representations checked through identical column nullability, extension versions (names compared), and physical column/enum ordering. No schema difference requiring new implementation was found. This was a synthetic production-compatible backup/restore, **not an actual production backup**. An actual verified production backup is step 1 of the authorized deployment window.

## Unresolved information is not a global rollout gate

The interface can deploy with the **36 ambiguous financing SKUs** and missing costs **S491722 / H504649** unresolved. Needs Attention supports explicit owner decisions and verified current costs, without changing Square identity or historical accounting. No production decisions were saved.

Unresolved identity/cost/financing remains unknown with visible warnings. Known valuation portions are identified as subtotals, never represented as a complete account value. Conflicting received financing remains incomplete; a current SKU choice cannot retroactively classify old weekly sales. Affected PO conversions require evidence review, a valid supplier/cost and allocations; unrelated complete supplier groups remain independently usable. A missing Card 2 blocks Monday recording only, not unrelated purchasing. Incomplete consignment valuation does not disable the entire ordering interface. No blanket prerequisite to resolve all 36 was introduced. Earlier synthetic preview decisions are not production assignments.

## Final test disposition

- **835 passed, 1 skipped, 7 subtests passed; no new failures** in the final relevant purchasing, inventory, receiving, payment, Square, V2, migration, release-entrypoint and integration run (182.54 seconds). See `regression.log/xml`.
- Separate employee-access/scheduling candidate check: **26 passed, the same 4 baseline failures**, zero skips. All four `test_own_schedule_scope_roles_and_denies` variants expect three `9:00 AM` occurrences but production-aligned HTML has two. The four reproduce in an independent untouched deployed-source export. Scheduling implementation/tests were not changed to suppress them.
- The one skip is unchanged opt-in `test_real_r2_private_delivery_and_authentication_separation`: no isolated private R2 test bucket/credentials exist. Production storage credentials are not authorization to upload test objects there. This is **UNVERIFIED**, not a pass. Mocked/private delivery tests run; no Purchasing change depends on a new R2 writer. An isolated bucket test or explicit scoped acceptance exception remains an owner release decision.
- An initial scheduling-only invocation omitted its PostgreSQL test environment and skipped 30 cases; it was rerun with the required local environment. The final 26+4 result above replaces that incomplete invocation. An initial PG18 cluster used SQL_ASCII; the rehearsal was rerun successfully with UTF8. Neither issue required application changes.

Source and test evidence is under `output/purchasing-final-preflight/`. Runs overlap; do not add their counts as independent coverage. This is a relevant-suite run, not a claim of an entirely green full repository suite.

## Exact proposed deployment sequence — execute only after owner authorization

1. **Freeze and back up.** Push only the reviewed release SHA after authorization; keep Render auto-deploy OFF. Record current web/cron commit, configuration and database head. Confirm the owner principal before cutover. Quiesce all application/database business writers, put web into a visible maintenance window and suspend cron/gate `SCHEDULE_AUTOMATION_EXECUTION_ENABLED=false`; wait for in-flight jobs, receipts, payments and Square requests to finish. Take a Render backup/PITR recovery marker and PostgreSQL 18 custom dump. Using secret-injected connection environment, run `pg_dump --format=custom --file="$BACKUP_DIR/database-0037.pgcustom"`; verify with `pg_restore --list`, SHA-256 and a restore into a separate PostgreSQL 18 recovery database. Compare version and selected historical financial/PO/receiving/scheduling counts/fingerprints. Back up existing generated/vendor PDF files at the configured location (including `generated/purchase_orders` where used), associated storage objects and configuration version identifiers, without printing credentials. Stop if backup/restore or document recovery cannot be verified. Keep writers paused throughout the migration/cutover.
2. **Migrate once from the candidate artifact.** In a dedicated one-off environment pinned to the release SHA, assert current revision is exactly `20261016_0037` and the 44 deployed migration hashes match. Set reviewed lock/statement timeouts for the window. Run `python -m app.schema_contract upgrade --revision 20261018_0038` with the production URL supplied securely through `DATABASE_URL`. Do not use `stamp`, `create_all`, downgrade or automatic backfill. Verify one head at 0038, required indexes/constraints/triggers, zero new checkpoint/activity/payment/owner-decision rows, and unchanged old-row fingerprints. Never run this command as a routine web/cron build step.
3. **Deploy web, exact SHA.** Use the existing `sh scripts/release/build.sh`, pre-deploy schema check `python -c "from app.schema_contract import assert_supported_schema; assert_supported_schema()"`, and `uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Keep V3 entry features off during initial startup; supply the confirmed owner ID and verified identities before reopening existing receiving. Do not bypass schema validation. Confirm build/runtime SHA and readiness. Existing worker stays quiesced until the new worker is healthy.
4. **Deploy cron, same SHA.** Preserve the existing build and `PYTHONPATH=. python scripts/run_schedule_automation.py` command and hourly `0 * * * *` schedule. Keep suspended/execution false during deployment. Confirm the cron artifact has the same code/head/schema contract as web. A disabled invocation must exit before importing database/automation; do not manually trigger enabled production automation as a smoke test.
5. **Activate reviewed features.** Apply the configuration table above, preserving unrelated environment variables, existing accounts and scheduling features. Activate Phase 1 before releasing any financial writer, then Phase 2 and Monday. Set Card 1=2, leave Card 2 unset and actual unknown issuer balances unknown. Never derive checkpoints from PO estimates, invoice totals, old funding reports or vendor pools. Owner entry of actual balances, Card 2 creation and production exception decisions require their own operational authorization/input. Do not activate purchasing automation. Resume the existing scheduling cron only after maintenance/schema checks pass and its previous settings are restored.
6. **Read-only production smoke.** Use existing authenticated sessions and GET/read-only SQL only. Verify schema/current SHAs, V2 navigation/auth/permissions, existing schedule views, purchasing landing, Payment Methods Card 1=Chase Visa #2 / Card 2 Needs Attention, Monday's four rows with recording blocked, Needs Attention's 36 unresolved IDs/cost warnings, existing PO detail, and existing stored PDF download without regenerating it. Use bounded Square catalog/count reads for the four stores only if needed; no refresh/reset mutation shortcuts. Compare historical payment/report/receipt/PO counts/fingerprints with the checkpoint. Do **not** Generate Order (creates a draft), edit/configure accounts, save decisions/payments, create/submit POs, clear/count/submit receiving, close shortages, regenerate documents, or adjust stock in this smoke phase. Stop on any unexpected history change, missing permission, fabricated amount or incompatible scheduling view.
7. **Controlled operational verification, separately authorized.** Present the read-only results first. Obtain specific owner authorization identifying any actual account/balance correction, exception decisions, PO, payment amounts or receipt/Square quantity adjustment to be exercised. Until then use the already verified isolated tests for mutation behavior. The owner can configure Card 2 later; missing Card 2 and unresolved SKUs do not prevent safe interface rollout. Preserve idempotency keys, before/after evidence and request identities during authorized operations; never blindly retry a lost acknowledgement with a new key.
8. **Recovery/rollback.** Follow the decision procedure below. Record outcome and stop for owner review if financial or inventory evidence is uncertain.

## Rollback and recovery procedure

- **Before activation or any new legitimate business writes:** keep maintenance and cron suspension. Correct failed build/configuration if reversible. If migration/candidate must be abandoned, retain the upgraded database for investigation and restore the verified 0037 backup into a **new** recovery database; validate fingerprints/head before repointing services to it and redeploying the former exact commit `529df6f…`. This database switch/restore requires the owner's incident authorization. Never overwrite the original database. Verify no legitimate writes occurred after the backup; otherwise use the post-write path.
- **After any Purchasing/payment/receiving activity:** retain schema 0038 and all appended evidence. Quarantine affected operations visibly, suspend cron if required, and use the tested **same schema-compatible candidate artifact** with Phase 2 and Monday entry points disabled. Keep Phase 1 actual-card authority ON; retain checkpoints, role mappings, historical batches and receiving idempotency evidence. Do not return to the old production binary against 0038, flip schema checks off, or fall back to assignment-triggered actual charges. Resume unaffected V2 operations only after verifying their guards/owner configuration. A full binary recovery after a deeper runtime defect requires a schema-compatible repair and owner review; feature quarantine is the prepared immediate containment, not a claim of a separately backported old V2 binary.
- Never downgrade populated evidence or restore an old backup across later legitimate payments/receipts/scheduling writes. Never replay a receiving event to compensate for uncertainty. Reconcile uncertain Square request status and payment request identities with the owner before correction. Keep original financial evidence; use existing explicit audited correction mechanisms only with authorization.

## Remaining owner decisions

1. Authorize the reviewed SHA and maintenance window/runbook, including recovery responsibilities.
2. Confirm which production principal is the receiving/payment owner (candidate 6); required before normal web cutover, not a guessed setting.
3. Card 2 identity and actual issuer balances can be supplied later through approved controls. Do not invent them or delay interface deployment merely to manufacture a second card.
4. Acknowledge the reproduced scheduling baseline-test defect and the isolated private R2 verification limitation, or provide isolated R2 credentials. These are reported exceptions, not new Purchasing failures.
5. Resolve the 36 SKU associations and missing costs through Needs Attention when the owner has actual evidence; they are not a blanket deployment blocker.

**READY FOR OWNER DEPLOYMENT AUTHORIZATION** — no new feature work was needed during this preflight. Deployment, configuration activation and production mutations remain unauthorized until the owner explicitly approves the proposed next step.
