# Purchasing V3 accounting correction and regression investigation

October 17, 2026 · Local development and isolated PostgreSQL only.

**Determination: PASS WITH LIMITATIONS — Owner decision required.** The narrowly scoped financing correction and affected purchasing acceptance tests pass. Purchasing V3 did not introduce the scheduling failure: it is a date-sensitive baseline fixture. The consolidated Monday screen is missing and explicitly deferred by the owner to a separately authorized task; production configuration/data remain unverified. Deployment is not authorized.

## A Accounting correction

The legacy placement/receiving hook consulted the currently selected supplier before loading the PO's saved financing assignment. It also ignored the Purchasing V3 intended account. For an existing consignment lot, switching supplier could stop subsequent consignment receipt valuation even while its saved assignment remained consignment. For a new purchase, an explicitly selected financing account could be replaced by supplier defaults.

**Authority is the existing PO-specific `OrderPayment`, its audited events and associated replenishment/receipt evidence.** PO lines identify the purchase lot; `FundingAccount` links its card method or consignment vendor. Square canonical vendor identity describes the product, not permanent financing for every future purchase of its SKU. The previous acceptance test overreached by expecting Big solely from SKU identity despite selecting a card account for a new purchase. It now tests an explicit Big assignment, with separate tests proving a new card-funded lot does not inherit old consignment financing.

Changes reuse existing schema and services:

- `app/services/v2_order_payments_service.py`: load/lock existing PO financing first; retain its financial vendor/method and synchronize consignment receipts using that assignment. Only a new, unassigned purchase can initialize from its owner-selected account. Explicit card selection wins over supplier defaults; explicit Big/Streamline selection uses that account's effective consignment classification. Invalid accounts and orphan consignment evidence fail closed for owner review. Unassigned purchases without an explicit selection retain the established effective vendor-default path; missing configuration stays uninitialized rather than guessing between cards. No SKU-history inheritance or actual card charge is introduced. The existing `OrderPayment` row is an unpaid financing snapshot, not an actual payment.
- `app/services/purchasing_session_service.py`: preserve selected financing in existing draft decision JSON when products move suppliers. Conflicting destination accounts block PO creation until the owner explicitly confirms the group's intended account. Existing account-selection audits retain prior group/line choices. This is planning-decision preservation, not another financial authority.
- `tests/test_purchasing_accounting_correction.py`: 16 real PostgreSQL cases. `tests/test_purchasing_predeployment.py`: corrected explicit-Big acceptance case. The legacy order-payment SQLite fixture now includes the existing purchasing-group table read by the hook; all 26 legacy cases pass. No model, migration, engine, receiving UI, PDF generator, payment screen or scheduling implementation changed in this correction pass.

Existing explicit bank/wire assignments remain supported through the existing audited All POs financial assignment service. No bank account type or new purchasing selector was added. Later legitimate owner corrections outrank stale original intended-account metadata; changing supplier alone cannot perform a financial reassignment.

| Accounting acceptance | Result |
| --- | --- |
| Big consignment, unchanged supplier | PASS |
| Existing Big or Streamline lot, supplier changed repeatedly | PASS; saved vendor/method retained; later receipt cost advances from $24 to $39 |
| New purchase, explicit Big / Streamline / Card 1 / Card 2 | PASS; correct financial account, supplier remains independently correct |
| Ordinary new purchase, configured card default | PASS |
| New purchase of previously consigned SKU | PASS; new card assignment, old lot records unchanged |
| Explicit bank assignment / later owner override | PASS; preserved, audited, no automatic charge |
| Draft supplier moves and refresh | PASS; prior financing retained, conflicting account requires explicit confirmation |
| Funding-account purchase-line membership | PASS; each new lot appears only under its assigned account |
| Historical financing records | PASS locally; prior payment/events/replenishments/receipts/ledger rows compared by persisted ID and unchanged |
| Ambiguous/inactive assignment | PASS locally; explicit error, no guessed reassignment or backfill |

An isolated diagnostic substituting the old initializer reproduces the receipt defect: Big and Streamline stay at **$24**, rather than **$39**, after 8 + 5 units at $3 when supplier changes. The corrected initializer passes both cases. That diagnostic is not counted as a final failing implementation test.

## B Scheduling regression

Exact test: `tests/test_v2_scheduling_foundation.py::test_lead_repair_prefers_ordinary_shift_and_longview_credit_follows_final_assignee`. Assertion: `lead_repair_changed_assignment is False`; actual result is True.

| Same environment and dependencies | Current clock, October 17 | Diagnostic planning clock, October 1 |
| --- | --- | --- |
| Untouched baseline commit `83b247976ef99e9923496211ef1134c203244889`, schema 0031 | FAIL, same assertion | PASS |
| Purchasing V3, schema 0034 | FAIL, same assertion | PASS |

Root cause: the fixture's October 5 draft Longview reservation has expired by October 17. The test expects that reservation to affect the October 18 rotation, but generation derives its planning date from the real current clock. Expired unpublished drafts no longer supply future reservation credit. Carla is provisionally selected and Lead repair assigns Alex, producing True. At October 1 the reservation is future and the test's expected direct selection passes.

Scheduling service/test hashes match the untouched baseline. This classifies the failure as **pre-existing, date-dependent fixture behavior**, not a Purchasing V3 regression, migration effect or changed dependency. The diagnostic clock was injected only in separate test processes. No scheduling business rule, fixture, generated schedule, assignment, publication control or automation was changed. Any permanent fixture repair is a separate scheduling issue under the owner's authorization boundary.

## C Regression results

Full regression: **1,441 passed, 3 failed, 1 skipped; seven subtests passed**, 525.93 seconds. Two failures were the legacy SQLite fixture's missing purchasing-group table; the fixture correction passed **all 26 legacy cases** on rerun. The remaining failure is the independent scheduling fixture described above. Five existing warnings remain.

Distinct latest results after the fixture rerun: **1,443 passed, 1 failed, 1 skipped**, plus seven passing subtests. This is deduplicated across the full run and affected reruns, not a claim of one green final full-suite run. The sole skip is private real-R2 integration opt-in, unrelated to Purchasing V3; no purchasing test was skipped.

Final affected selection: **107 passed, 0 failed, 0 skipped**, four existing warnings. It includes all 16 accounting cases plus the prior purchasing/UI/receiving/PO service selection. The earlier 106-case selection and intermediate matrix runs overlap and are not added. An earlier full run was deliberately interrupted after the final draft-account safeguard was identified; it is superseded by the final full run and contributes no additional unique count.

Baseline and frozen-clock scheduling probes are diagnostic repeats of the same test, not additional distinct coverage. The old-hook reproduction intentionally reinstalls the defective initializer and is also excluded from final implementation counts. Full-run/affected-run logs and final JUnit results are in `output/purchasing-accounting-correction/`.

## D Purchasing and inventory integrity

**PASS locally.** Actual application routes and database persistence repeat recommendations → four-store allocation edits → supplier substitution → PO creation → vendor PDF → partial receiving → later shipments → reconciliation. Quantity overrides and allocations survive refresh; conversion remains idempotent; historical PO/PDF evidence remains unchanged.

The established scenario still reconciles **180 received units, ten unique inventory commands, twelve mocked HTTP attempts**. First SKU receives 90 + 5 + 5 = 100; second SKU receives 80 in the final shipment. Independent Square ledger equals successful persisted receipt deltas: each store receives 25 of VAR1 and 20 of VAR2. Event totals remain 90 / 5 / 85 and PO-wide outstanding 90 / 85 / 0. Accepted-timeout recovery reuses the exact durable key/payload; rejected work retries without replaying successful stores. No duplicate, missing, zero, unreceived, unrelated or historical adjustment appears. Actual financial activity from PO preparation/receipt remains zero.

The actual two-line vendor PDF still totals $600.00. The 250-line / 14-page PDF reconciles every persisted row and totals $507,812.50. No PDF implementation change was needed. Existing 500-SKU, concurrency, permissions, receiving draft, shortage closure and duplicate-submission tests pass in the affected selection.

## E Monday payments and production readiness

**The consolidated Monday screen is NOT IMPLEMENTED in the inspected code.** No combined four-account Sunday–Saturday / following-Monday “Save Payments” route or template was found in this checkout or the other local application checkouts. `purchasing-v3-phase1-local-implementation.md` identifies the four-account Monday screen as later-phase work. The owner clarified that its existence must not be assumed and explicitly excluded its implementation from this task. It was not built. Actual existing routes and reusable components are catalogued in [Monday payment workflow inventory](purchasing-v3-monday-payment-workflow-inventory.md).

The existing payment backend was exercised with real PostgreSQL and four finalized synthetic Sunday–Saturday reports. On the following Monday, edited actual payments of **$10 / $20 / $25 / $35** reach Big / Streamline / Card 1 / Card 2 respectively. Remaining report amounts are **$90 / $180 / $275 / $365**. Card checkpoints of $1,000 / $2,000 become **$975 / $1,965**; only the two actual repayments affect actual card activity. These tests certify account routing and edited payment persistence, not generation of Monday recommendations or a combined Save Payments UI. Existing legacy report calculations and payment workflows were preserved; no pooled weekly engine or FIFO financing attribution was added.

Before any owner-authorized release:

1. Keep the missing consolidated Monday workflow as a separately authorized implementation task. Do not claim complete four-account weekly acceptance from backend payment tests alone. Record the independent scheduling fixture issue and its disposition.
2. No new migration is required by this correction. The unreleased V3 chain still requires additive **0032 → 0033 → 0034** as applicable to the actual target revision. Confirm target schema/configuration and rehearse with an authorized isolated copy; production was not inspected or changed.
3. Review existing PO financing, supplier defaults, active consignment classifications, both card methods and account mappings. Investigate orphan/inconsistent consignment evidence and differences between saved assignments and original intended accounts. Such differences may reflect legitimate later owner corrections. `compatibility-preflight.sql` is read-only, syntax-checked locally on 0034, and was not executed in production. Already-moved legacy drafts without retained account metadata require owner review; no historical intent was reconstructed.
4. Review existing feature flags and owner/store scope, Square credentials/location mappings, secrets and generated-PDF persistence. No new flag or financial writer is introduced. Keep catalog writes prohibited and purchasing automation disabled; use existing actual-activity authority only for known charges/payments.
5. Back up and restore-test the database, PDFs and approved configuration/secrets before cutover. Retain additive schema and audit evidence on rollback. The older rollback rehearsal targets through 0031 and is insufficient for 0034; an older schema-restricted binary is not a valid recovery build. Freeze new financial/receiving writes during rollback and reconcile durable Square commands rather than replaying history.

**Recommendation:** accept the local financing correction and scheduling investigation for owner review, with the separately authorized Monday implementation and production preflight explicitly outstanding. Do not deploy or broaden accounting/scheduling scope without owner direction.
