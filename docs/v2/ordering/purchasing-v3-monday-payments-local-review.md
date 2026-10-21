# Purchasing V3 Monday Payments Local Acceptance Review

> Historical implementation review: the consignment report-based Outstanding display described below has been superseded by the [Consignment Inventory Value correction](purchasing-v3-consignment-inventory-value-review.md). Weekly payment recording and its historical evidence remain preserved.

Monday Payments is implemented for local owner review. Four account recommendations and four editable actual amounts share one atomic Save Payments action. Existing funding reports, payments, allocations, audit entries and the actual-card authority remain the financial records. No production database, configuration, deployment or external financial service was changed.

Open http://127.0.0.1:8763/management/purchasing/payments?monday=2026-10-19. Inventory → Order Payments replaces the placeholder for the configured owner. The primary V2 navigation also includes the destination under Inventory / Ordering. Both navigation variants and server access require the feature and owner configuration.

The preview uses a separate synthetic database, blocked external network access and a simulated October 19 payment date. Database audit timestamps retain the computer's actual October 17 time. The two preview history rows are test batches, not historical business payments. Expand the latest row and choose **Correct this week's payments** to edit the recorded example. First-time weeks prefill actual amounts from available recommendations; subsequent changes require explicit correction.

## Calculations and accounting boundaries

- October 19 selects October 11–17 in America/Los_Angeles. A completed-period sales-coverage contract is required across all active store locations. Sales outside the period or active locations are excluded.
- Gross quantity × authoritative local/captured cost establishes COGS. Discounts, taxes and return records do not reduce this approved weekly basis. Missing cost is unavailable, never manufactured zero. Ordinary sales lacking captured cost reuse the existing configured-product cost resolver and freeze the selected cost in the batch snapshot.
- Explicit sale overrides and saved received-PO financing are respected. Supplier substitutions do not reassign financing. Conflicting received-lot classifications produce unavailable recommendations; no FIFO, latest-lot selection or new inventory depletion model resolves ambiguity.
- Big and Streamline retain their canonical consignment identities. Ordinary card COGS remains the approved pool, divided between the explicitly configured Card 1 and Card 2 in proportion to positive actual issuer balances. Negative balances have zero weight. Unknown balances or an undividable positive pool are unavailable. The saved balances, split, costs and source IDs are frozen.
- Existing overlapping reports or already finalized source facts require review. No duplicate COGS recommendation is created. Owner-entered actual payments remain possible when recommendations are unavailable, provided all four accounts are configured; unavailable COGS is not turned into a report obligation.
- Recommendations and PO/receiving activity never post actual payments. Save Payments records owner-reported activity through existing payment writers; it sends no money. Zero amounts retain an audited batch entry without a fictitious zero-dollar money record.
- Outstanding is explicitly labeled: unpaid existing reports for consignment; the sole actual issuer balance for cards. Unallocated credits remain visible in existing funding-account activity. No parallel card-balance calculation was introduced.

Synthetic browser recommendations reconcile to Big $4,500, Streamline $2,200 and a $5,900 card pool. Actual issuer balances of $31,000/$28,000 split that pool $3,100/$2,800. The first recorded actuals were $4,400/$0/$3,100/$2,800. Explicit correction changed them to $4,350/$0/$3,000/$2,800. The original and correction remain separate history rows; actual card balances are $28,000/$25,200. Consignment report shortages are $150/$2,200.

## Atomicity and recovery evidence

A PostgreSQL week lock serializes batches; account locks use stable ID order shared with the existing authority. Sale-source locks and finalized source links prevent competing legacy finalization from claiming the same sale twice. Recommendations are checked again under the transaction locks. All four reports/entries/payments and their audit effects commit together, or roll back together.

The database enforces one request identity, one revision per week, one successor per correction and four distinct account/role entries. A deferred constraint validates complete entries against the existing money records. Committed batch headers and entries reject updates/deletes. Corrections append existing payment reversals and replacements; they do not overwrite originals. Weekly reports cannot be deleted or voided through legacy report controls, and weekly payments must use the explicit batch correction. Legacy report/payment workflows retain their existing controls for other records.

A stable browser request identity and actual amounts persist before submission. Inputs are locked during a save. Failed or unverifiable acknowledgements retain the draft. Retry returns the committed batch before recomputing changed balances. Another request for the same week conflicts rather than recording another batch. Refresh preserves entered actuals; recorded recommendation snapshots stay frozen while current balances remain current.

Browser verification exercised editing, zero actuals, refresh, reload, failure before commit, lost acknowledgement after commit, safe retry, correction cancellation, correction persistence and explicit correction saving. PostgreSQL reconciliation shows **2 batches, 8 entries, 4 existing reports, 9 existing payment rows**: three initial payments, three reversals and three replacements. Net actual-card effects are exactly −$3,000/−$2,800. See `output/purchasing-monday-payments/browser-database-reconciliation.json`.

## Verification results

- **25 new Monday-payment PostgreSQL tests passed**; result in `output/purchasing-monday-payments/monday-final.log`.
- Accounting/funding/consignment/financing/migration regression: **249 passed**, `final-accounting-regression.log`.
- Final payment/report compatibility after the legacy weekly-report boundary: **159 passed**, `report-compatibility-final.log`.
- V2 shell, primary navigation and Monday UI regression: **86 passed**, `final-ui-regression.log`.
- **6 final schema/migration tests passed**; result in `migration-final.log`. It includes fresh upgrades, strict schema gates and historical preservation.
- Full suite before final focused corrections: **1,463 passed, 3 failed, 1 skipped; 7 subtests passed**, `full-regression.log`. Two failures were stale expected migration ancestry/table-count assertions introduced by adding schema 0035; both were corrected and pass focused reruns. The remaining failure is the previously documented scheduling fixture involving an expired October 5 reservation. Scheduling code was not changed. This is not an all-green full-suite claim.
- The sole full-suite skip is the opt-in private R2 delivery integration. It is unrelated to Monday payments; no purchasing, receiving or balance tests were intentionally skipped in that run.
- JavaScript syntax validation passed. Browser and service tests verify existing funding-account pages/reports and exact balance effects. Existing purchasing, PO/PDF, receiving and financing regressions are covered by the full run and the financing-focused run.

The additive 0035 migration creates only batch coordination/audit tables and their constraints/triggers. It performs no financial backfill or posting. A populated migration rehearsal fingerprints existing financial, source, PO and audit rows across 0034 → 0035, including issuer checkpoints. Older-history preservation also passes the existing 0031 → head rehearsal. Downgrade refuses to discard weekly history.

## Changed implementation files

- `.env.example`, `app/config.py`: default-off feature and explicit account-ID configuration.
- `app/models.py`, `app/schema_contract.py`, `migrations/versions/20261017_0035_monday_payments.py`: additive batch records and schema head.
- `app/services/purchasing_payments_service.py`: weekly orchestration, evidence checks, snapshots, atomic save, idempotency and explicit corrections.
- `app/services/v2_funding_reports_service.py`: validated weekly context for existing payment/reversal writers, source locking during legacy finalization and preservation guards for weekly reports.
- `app/routers/purchasing.py`: owner/CSRF-protected page, workspace and save endpoints.
- `app/v2/navigation.py`, `app/v2/workspaces.py`: owner/feature-gated native navigation.
- `app/templates/purchasing/payments.html`, `app/static/v2/monday-payments.js`, `app/static/v2/purchasing.css`: compact table, persistent actual drafts, save states and collapsed history using V2 components/tokens.
- `app/templates/v2/order_payments/funding_account_detail.html`, `funding_report_detail.html`: identify weekly dollar allocations and direct their changes through audited weekly corrections. Existing nonweekly reports retain their presentation and controls.
- `tests/test_purchasing_monday_payments.py`: PostgreSQL, HTTP, permissions, concurrency, rollback, source/account compatibility, corrections and preservation tests.
- `tests/test_canonical_migration.py`, `test_schema_migration_postgres.py`, `test_square_cost_workflow_boundaries.py`, `test_v2_digital_signage.py`: new head, ancestry and additive table-count expectations.

These are the Monday-payment changes; the checkout also contains earlier authorized Purchasing V3 work.

## Remaining limitations and deployment prerequisites

1. Obtain explicit owner acceptance and separate deployment authorization. Nothing was deployed.
2. Confirm the real Card 1/Card 2 funding-account IDs, canonical Big/Streamline Square vendor IDs and owner ID. These were not inferred or written to production. Defaults remain disabled; the local harness uses synthetic IDs.
3. Rehearse additive migration 0035 and establish a compatible rollback plan retaining audit history. Startup's schema gate now requires 0035 even when the feature is disabled. A populated weekly schema must not be destructively downgraded.
4. Enable the owner-scoped Phase 1, Phase 2 and Monday feature only after configuration review. Existing funding-account read access still uses its established `order_payments_v2` exposure. Review legacy overlapping reports and source readiness before the first operational week.
5. Reliable recommendations require fresh complete sales coverage, verified canonical identities, known local costs and established actual issuer balances. Ambiguous mixed financing remains unavailable until an explicit supported owner resolution supplies evidence; this implementation does not invent a lot allocation.
6. A correction cannot reverse actual activity already covered by a later issuer checkpoint. The entire correction rolls back and requests existing Adjust Balance reconciliation; it cannot replay old payments past a checkpoint.
7. Browser draft persistence is local to the current browser/device. Cross-device storage is not provided. Server idempotency, weekly uniqueness and audit history remain authoritative across devices. History displays the latest 30 revisions; older records remain preserved in the database/existing accounts.
8. The independent scheduling regression and opt-in R2 integration remain as documented. No live Square or production financial integration was exercised.

Recommendation: review the local four-account workflow and its screenshots. Do not deploy until the explicit production prerequisites and owner authorization are satisfied.
