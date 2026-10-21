# Purchasing V3 production preparation — 2026-10-18

**BLOCKED — the approved Purchasing checkout is not compatible with the current production release/schema.** No implementation, deployment, production migration, feature activation, financial record, PO or Square quantity was changed. This report prepares a conditional deployment; it does not authorize execution.

Evidence is under `output/purchasing-production-readiness/`. Production PostgreSQL inspections explicitly executed `SET TRANSACTION READ ONLY` (verified `on`), an 8-second statement timeout and 1-second lock timeout. SQL queries were bounded; detail limits are visible in the inspection scripts. Credentials were fetched into memory, never written to these artifacts. Render calls were GET only. Square calls used the existing central request policy and read gateway; POST search/retrieve endpoints do not mutate inventory. Application startup was never run against production.

> Attribution clarification, 2026-10-18: the [fresh SKU investigation](purchasing-v3-attribution-investigation.md) confirms 167 live dual Square associations, resolves 131 using existing Big financing, and leaves 36 unresolved. The separate 120 TERMS-related warnings are not Big/Streamline ownership conflicts; saved non-consignment invoice treatment excludes them from those accounts. This supersedes the attribution interpretation below, not the independent schema/card deployment blockers.

> Local integration update, 2026-10-18: the schema ancestry blocker described below has been reconciled and rehearsed locally. See the [final integration review](purchasing-v3-final-integration-review.md) for current test results and remaining owner/deployment gates. The original production observations below are retained as historical evidence.

## Readiness matrix

| Area | Result | Evidence / outstanding requirement |
|---|---|---|
| Big Wholesale mapping | Account identity verified; SKU attribution incomplete | Active consignment account **1**, local vendor **40**, canonical Square vendor **42STABRGTKEP4PSC**. 172 configured SKU/cost rows; live catalog identifies 172 variations. Saved financing must take precedence over purchasing suppliers. |
| Streamline mapping | Account identity verified; SKU attribution incomplete | Active consignment account **3**, local vendor **32**, canonical Square vendor **JYKK2W5FJUPOMDPQ**. 191 configured SKU/cost rows; live catalog identifies 190 variations. Inactive local vendor **39**, `Stream`, is a different identity and must not be selected. |
| Credit Card 1 mapping | Owner confirmation required | Only one active card funding account exists: **2**, `Chase Visa Inventory`, linked to active CREDIT_CARD payment method **1**, `Chase Visa`. No production V3 role mapping is configured; do not infer whether this is Card 1 or Card 2. |
| Credit Card 2 mapping | Missing; owner action required | No second card funding account or second CREDIT_CARD payment method exists. The Monday workflow requires four distinct accounts; account 2 cannot fill both roles. Owner must identify the second card and authorize its existing-workflow setup separately. |
| Square inventory | Connections and bounded inventory coverage verified | All four production store IDs match active Square locations. 196 distinct configured consignment variations × 4 locations = **784/784 explicit counts**, no omitted pairs in this read. This is coverage evidence, not proof of financing attribution or every catalog SKU. |
| SKU costs / suppliers | Exceptions identified | 1,478 active supplier/SKU configuration rows, all with variation IDs; two missing unit costs listed below. No conflicting non-null costs within a vendor/variation group. All 363 Big/Streamline configured rows have costs. Supplier association is not financing authority. |
| Consignment formula | Verified locally/source; live final valuations incomplete | Current Square on-hand × authoritative cost, summed once per reliably attributed variation. Unknown attribution must remain incomplete. No payment/credit/unpaid-report inputs. |
| Database compatibility | **FAIL** | Live database revision **20261016_0037**; local supported head **20261017_0035**, forked from **20260930_0031**. The local migration graph does not contain the production scheduling 0032–0037 revisions. No valid direct upgrade is prepared. |
| Existing V2 compatibility | **Not established for deployment** | Local operational regressions pass, but this checkout omits later deployed scheduling/authentication/model changes. Production records remain intact; direct replacement is unsafe. |
| Required migrations | Documented; integration blocked | Four additive purchasing migrations listed below must be integrated with the actual production migration ancestry before deployment. Do not stamp over the production revision or disable schema validation. |
| Feature/environment configuration | Inspected; incomplete | No PURCHASING_* variables exist on production web or cron. V3 flags therefore default off. Owner principal, canonical vendor IDs and both card role IDs need reviewed configuration. Existing V2 financial-action flags are true and require a controlled writer cutover. |
| Integration tests | Results documented, R2 unverified | Private R2 test requires an isolated writable test environment; only production-bucket credentials were found. Scheduling failure independently reproduced and isolated. See verification below. |
| Deployment procedure | Prepared conditionally | Execution gates and ordered procedure below; not executable until compatibility and mapping gates close. |
| Rollback procedure | Prepared conditionally | Requires a rehearsed schema-compatible fallback. A blind rollback to either old binary is unsafe after the schema head changes. |

## Production observations

Web service `srv-d6ccg9ktgctc738u1c1g` and hourly cron `crn-da6pd2qd0e5s73dbudr0` both have live commit **529df6f566b780e2e8ab5d470c160fb481d06715**. Web tracks `codex/release-scheduling-0033`; cron tracks `codex/release-dated-allocations-0037`. Both have automatic deployment off. Both use `sh scripts/release/build.sh`, pinned Python 3.10.13. Web runs Uvicorn and its predeploy command only asserts the supported schema; it does **not** migrate. Cron runs `PYTHONPATH=. python scripts/run_schedule_automation.py`, schedule `0 * * * *`, and is active. Updating only the web service would leave an incompatible cron binary after a schema change.

Web V2 global features: `v2_primary,staff_scheduling_v2`. Principal-scoped features include principal 6's `ordering_intelligence_v2` and `order_payments_v2`; read-only principal lookup confirms principal 6 is active ADMIN, but that does not establish owner identity for V3. Both `V2_CONSIGNMENT_COGS_ACTIONS_ENABLED` and `V2_CREDIT_CARD_COGS_ACTIONS_ENABLED` are true. `SQUARE_READ_ONLY=true`, `SNAPSHOT_PROVIDER=square`; Square token/application and private R2 configuration are present. Cron has `ENVIRONMENT=production`, `SCHEMA_REVISION_CHECK_ENABLED=true`. No secret values are included in the redacted environment artifacts.

| Store | Local store ID | Live Square location |
|---|---:|---|
| Andresen | 2 | LZH57Q25MGWS1 |
| HWY 99 | 4 | LV1SDYTZB2AYR |
| SR 503 | 9 | L5Y41WDGQGWBC |
| Longview | 6 | LS4T7D19Y1WYR |

Read-only counts: 56 purchase orders, 4,508 PO lines, 18 saved order-payment assignments, 3 funding accounts, 28 funding payment records and 78 funding reports. `purchase_order_receipts` currently contains zero rows despite qualifying received PO-line quantities. These are observations, not defects repaired or an authorization to backfill. Weekly recommendation attribution that requires dated receipt evidence may therefore be unavailable for legacy sales; owner-entered actual payments do not justify fabricating that evidence.

### Attribution and cost exceptions

Big/Streamline catalog vendor associations overlap on **167 variations**. There are 363 configured supplier rows but only **196 distinct variations**. Summing both vendors' supplier lists blindly would double-count inventory. Qualifying saved received financing exists for 131 Big and 11 Streamline variations, but these are historical financing cohorts, not proof of current quantities or current unit splits.

The exact valuation classification applied to saved, qualifying received assignments finds **120 variations with unknown financing evidence**: 108 with unknown-only categories and 12 with other-plus-unknown categories. These are not 120 proven mixed consignment/card splits. They are unresolved evidence, including unsupported legacy payment categories. The service warns both consignment accounts when a saved financing category is unknown. Separately, 13 qualifying historical orders / 399 distinct received variations have no saved `OrderPayment`; current catalog association fallback cannot resolve ambiguous two-vendor identities. Do not silently translate TERMS, choose the latest supplier, allocate proportionally or apply FIFO.

The complete bounded exception list (120, below its 1,000 limit), SKU IDs and categories is in `production-mappings.json` → `financing_ambiguity`; catalog identities, configured SKUs/costs and explicit current counts are in `square-readonly.json`. Example affected SKUs: `F539462`, `341310T`, `649964672252`, `410219Q`, `5094646`. Configured Streamline SKU **G635369** is absent from the active catalog response and needs identity review; its configured inventory count was nevertheless retrieved. No live final Big/Streamline valuation is asserted.

Missing configured unit costs:

- **S491722**, IMRBatteries, config 1309, variation `UVJY6W6FIAQJWRVTW2K5SYDN`.
- **H504649**, VaporBeast, config 1308, variation `VF7GE5L6NBU7WXHJOARC6CE2`.

One qualifying historical received line also has no unit cost: SKU **7**, PO **77**, line **7083**; configured-cost completeness does not prove every saved received-line cost is valid. Missing latest authoritative received cost must not fall back to an invented/older cost.

Owner decisions: identify the two distinct card roles/accounts; confirm the V3 owner principal; supply authoritative resolution for ambiguous consignment SKU attribution and the above data exceptions. Canonical Big/Streamline account identities are verified, but production environment mapping remains unset. No assignments were guessed or changed.

## Approved calculations and protections

`app/services/purchasing_payments_service.py:266` calculates:

```
ROUND_TO_CENTS(SUM(unique reliably attributed variation:
    SUM(explicit current Square IN_STOCK quantities across active stores)
    × authoritative SKU unit COGS))
```

Saved qualifying received `OrderPayment` financing identifies the financing cohort; PO quantities do not reconstruct stock. Uniform financing counts a variation once. Mixed/unknown financing remains incomplete. Without received financing, live canonical catalog identity plus existing explicit cash exception applies; local purchasing supplier options alone do not assign financing. Assigned cost comes from the latest authoritative received PO line; otherwise the existing configured cost resolver is restricted to the financing vendor. No Square cost is imported.

Weekly recommendations (`recommendation`, line 101) remain prior Sunday–Saturday sales COGS. Current inventory valuation runs independently in workspace reads, outside Save Payments and frozen recommendation history. Vendor credits, payment allocations, payments/reversals and unpaid funding reports are not valuation inputs. Cards retain `card_balance_service` actual activity/checkpoints. Existing legacy issuer balances must not be inferred from PO estimates or automatically migrated as real charges; approved actual balances/checkpoints are an activation prerequisite.

Prior independent synthetic four-store valuation: Big **$1,600**, Streamline **$370**, independently matched SKU sums. See the approved consignment valuation review and its `independent-valuation.json`. This is test evidence, not production valuation.

## Migration / release inventory

Purchasing migrations currently form a separate chain after 20260930_0031:

| Migration | Change |
|---|---|
| 20261017_0032 | Evidence, product authority, actual-card activity and balance checkpoints; no historical backfill/posting. |
| 20261017_0033 | Nullable receiving-event fields and request uniqueness on existing receipts; immutable store allocation evidence/triggers. Legacy null values preserved. |
| 20261017_0034 | Purchasing sessions, revision/line/supplier groups and immutable conversion lineage. |
| 20261017_0035 | Monday batches/entries, idempotency/week/account constraints and deferred completeness/immutability triggers. No financial posting during upgrade. |

Production instead advanced through scheduling revisions dated October 13–16, ending 0037. The full revision IDs differ; similar numeric suffixes are not the issue. **The incompatible ancestry and exact-head startup checks are the issue.** A release must preserve every production migration/model/authorization/scheduling change and integrate the approved purchasing delta into that ancestry. Do not run this branch's upgrade, relabel production with stamp, blindly cherry-pick all old files, or change scheduling rules. Integration/rehearsal is remaining compatibility work, not a new feature; no such change was made in this preparation task.

`changed-tracked-files.txt` and `new-release-files.txt` inventory the current uncommitted release delta, including new services/assets/templates/tests/migrations. The current local HEAD alone does not contain the approved implementation. Prepare and review an immutable release commit/artifact after integration; do not deploy the current HEAD or include output fixtures/preview launchers/secrets. Re-run inventory at release freeze. Older general schema-baseline documentation lists historical heads and cannot substitute for the live revision query.

## Verification

- Focused rerun: **44 passed, 1 failed, 1 skipped** (`verification.log`). The failure is the known scheduling fixture; the skip is private R2. Purchasing valuation, Monday transaction/concurrency and additive preservation tests passed.
- Scheduling failure reproduced at the original assertion (`lead_repair_changed_assignment` True versus expected False). Service and fixture SHA-256 values match untouched baseline 83b2479. Earlier baseline/current clock comparisons both failed at the late clock and passed at October 1. A fresh diagnostic October 1 clock injection again **passed** (`scheduling-frozen-clock.log`). No scheduling source or fixture changed. This isolates the failure from Purchasing; it does not certify the later production scheduling release against an unintegrated purchasing branch.
- Private R2 integration remains **UNVERIFIED**. Production credentials exist, but `test_real_r2_private_delivery_and_authentication_separation` calls `store_or_reuse_image`, uploading a deterministic object into `digital-signage/images/<hash>` in the configured bucket, without a dedicated test prefix/cleanup. Only the production bucket environment was located; using it would modify production storage. No production R2 upload was attempted. To close: supply an isolated private R2 test bucket/credentials and run the unchanged test with local disposable PostgreSQL and `RUN_REAL_R2_TESTS=1`. A skip is not a pass.
- Full relevant operational regression rerun: **531 passed, zero skips**, 109.18 seconds, five existing warnings (`regression.log`). Coverage includes purchasing, PO/PDF, receiving/distribution, financial assignment isolation, card authority, Monday payments, consignment valuation, funding reports, Square read gateway and V2 navigation. This does not prove compatibility with production's divergent schema; an integrated-release rehearsal remains mandatory.
- No production writes occurred. Local preservation rehearsal compares historical fingerprints across migrations. Production inspection used enforced read-only transactions; its row counts are observations, not a complete backup or proof against unrelated concurrent operational writes.

## Conditional deployment procedure — execute only after owner authorization

1. **Close release gates.** Integrate onto the live production release, retain all production scheduling/access changes, establish one reviewed supported schema head. Resolve four-account mapping/owner identity, unknown attribution and cost handling; obtain actual card balances/checkpoints through existing owner workflows. Close private R2 verification or obtain an explicit scoped acceptance exception. Re-run relevant V2/purchasing tests and a disposable migration/rollback rehearsal from the actual production schema. No scheduling feature repair is part of this task.
2. **Freeze and verify the artifact.** Record exact release and fallback commits, tree/diff hashes, dependencies, reviewed migrations and redacted web/cron configurations. Ensure new untracked implementation files are included. Keep auto-deploy off. Test that old routes, permissions, PDFs, receiving and weekly payment idempotency work against the integrated schema. Review enabled legacy COGS writers so every actual activity enters the sole balance authority exactly once.
3. **Backup and recovery gate.** Before any approved migration, obtain/verify a managed PostgreSQL backup or encrypted custom-format pg_dump using a client compatible with production PostgreSQL 18 (local installed client is 17; do not assume it can dump 18). Record restore point/time, retention and restore credentials; restore to a disposable isolated environment and verify revision/counts/constraints/representative financial history. Back up PO PDF/document artifacts and any durable files separately, and confirm their actual storage location/retention; the Render web metadata lists no persistent disk. Never copy production credentials/data into test fixtures or seed production. Coordinate a write freeze so backup and migration boundaries are meaningful.
4. **Coordinate all database consumers.** In the authorized window, pause the active hourly scheduling cron and relevant web/background write entry points using the approved operational procedure. Preserve existing cron execution settings; do not accidentally activate automation. Recheck live deployed commits/revision/config for drift. Keep all V3 flags off and owner access closed.
5. **Apply reviewed integrated migrations once.** Use the exact approved integrated migration command/head and a dedicated migration runner. The current web predeploy command only checks schema, so it cannot perform this step. Preserve financial history; no backfill/actual-card charges or assignment side effects. Validate schema, triggers/indexes and historical fingerprints/counts afterward. Stop on any unexpected drift; no stamp or schema-check bypass.
6. **Deploy schema-compatible consumers.** Deploy the reviewed web and cron artifacts while cron remains paused, with their exact-head contracts aligned. Preserve secrets, integration configuration, authentication and existing V2 flags. Restore ordinary V2 service only after read checks pass; restore cron at its existing approved schedule/settings only after compatibility verification.
7. **Activate narrowly under explicit owner authorization.** Configure reviewed canonical IDs, owner principal and two distinct card account IDs. Activate Phase 1, Phase 2 and Monday flags in the approved order after the financial writer cutover/checkpoints are verified. No activation was performed here. Existing broad financial flags must not leave a path that bypasses the new authority; verify rather than assume disabling a UI button protects backend writers.
8. **Non-mutating production smoke checks.** Verify schema/health and login/permission behavior through existing authorized sessions; inspect existing PO details and existing PDF delivery, existing receiving history, existing payment history, four-account mapping and displayed costs/inventory values. Use only paths whose GET code has been checked for side effects; recommendation/catalog refresh GETs can persist caches, so do not assume GET means read-only. Compare a bounded SKU sample to policy-controlled Square read counts and local costs; incomplete evidence must warn. Confirm unauthorized access is denied without submitting forms. **Do not generate/create a PO, clear/close/submit receiving, save a payment/correction, refresh persistent evidence, run a cron, or adjust Square inventory.** Any write smoke test needs separate explicit authorization. Verify money and Square mutation counts have not changed due to release activity.

## Rollback procedure

Prepare and rehearse a fallback build that accepts the **integrated** schema and preserves the current production V2 functionality before migrating. Both the current live binary (0037 only) and this local binary (0035 only) enforce exact revision checks; neither is an automatic fallback for a new merged head.

On a failed deployment, stop new V3 writes and pause cron/other incompatible consumers; preserve logs, idempotency requests and in-flight submission evidence. Disable V3 exposure only through the authorized rollback procedure and deploy the rehearsed schema-compatible fallback. Retain additive tables/history and reconcile in-flight payment/receiving requests before retries. Do not downgrade populated history: the purchasing downgrades deliberately refuse evidence loss. Do not replay already successful inventory/payment requests.

If a compatible fallback cannot restore service, use the verified backup/restore procedure under an approved outage/data-recovery decision. A restore after production writes can lose legitimate financial/receiving/scheduling activity; require reconciliation and owner approval rather than automatically restoring. Restoring PostgreSQL cannot undo Square mutations or real payments; this deployment's smoke tests therefore permit neither. Resume existing cron only when its binary and schema agree.

**Recommended next action:** authorize a narrowly scoped production-compatibility integration and disposable rehearsal, identify the second card and role mapping, and resolve the documented attribution exceptions. Return for deployment authorization only after these gates are closed. No deployment is authorized by this report.
