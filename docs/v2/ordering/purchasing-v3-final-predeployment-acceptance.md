# Purchasing V3 final predeployment acceptance

October 17, 2026 · Local synthetic data / disposable PostgreSQL / mocked Square only.

Follow-up: [Accounting correction and regression investigation](purchasing-v3-accounting-correction-review.md) supersedes the accounting blocker assessment below. The original test results are retained as historical acceptance evidence; consult the follow-up for current readiness and the remaining Monday payment verification gate.

**Overall: FAIL — require corrections before production authorization.** Purchasing, PDF and inventory reconciliation passed locally. Canonical COGS attribution does not survive a configured legacy financial hook after supplier substitution. The full regression suite also retains the previously documented scheduling failure. Production data/configuration compatibility and live performance remain unverified. No deployment, production migration, live Square mutation or production financial change occurred.

## Workflow acceptance

| Stage | Result | Evidence and qualification |
| --- | --- | --- |
| A — Recommendations | PASS locally | Existing engine consumes current Square observations, keeps incoming separate and ignores historical receipts as current stock. Four-store differing needs verified as **59 / 128 / 197 / 0**, total 384, including a genuinely zero-need store. Incomplete/missing coverage remains unknown. Refresh retains owner quantities, supplier and allocations. |
| B — Allocations | PASS locally | Four editable allocations persist; total is their sum. 500-SKU route tests cover group units/cost, supplier moves, sorting/search/filter/pagination and stale-version HTTP 409. Browser burst of 24 edits in the 506-product workspace saved **65 / 80 / 95 / 81 = 321** and survived reload. Supplier units increased 20 and cost $150.00. Manual owner quantities survive supplier substitution; automatic quantities retain existing supplier pack rules. |
| C — PO creation | PASS locally | Actual routes generate a session, edit allocations, change supplier, refresh, retain intended account, create an idempotent PO and place it through the existing route. Persisted lines are 100 and 80; each equals its allocation sum. Supplier 2 and intended account 10 retained; immutable revisions/conversion lineage preserved. No inventory or actual financial entries created by preparation. |
| D — Vendor PDF | PASS after narrow correction | Actual vendor PDFs now include SKUs, products, consolidated quantity, unit cost, line total and estimated total. Two-line PDF reconciles to **$600.00**. A **250-line / 14-page** PDF from a 500-SKU session reconciles every SKU/name/quantity/unit cost/line total and **$507,812.50** total against persisted PO rows. Rendered first/last pages inspected. No internal store allocations, intended account or COGS fields exposed. Existing template/pack/removed-line tests pass. |
| E — First receipt | PASS locally | Actual shipment route counts 25 / 20 / 25 / 20 = 90 of the first SKU's 100. That SKU has 10 outstanding; the entire two-SKU PO has **90** outstanding because the second SKU's 80 remain unreceived. Unsent draft survives refresh and tab close/reopen; mock post count stays 21. Submit generates only new positive received quantities. |
| F — Later receipts | PASS locally | First SKU completes as **90 + 5 + 5 = 100**. Third shipment also delivers the second SKU's 80: event totals **90 / 5 / 85**, PO-wide event-time outstanding **90 / 85 / 0**. Three immutable events; Fully Received; reopening shows history without receiving controls. Explicit shortage closure is idempotent and blocks further counts. |
| G — Inventory reconciliation | PASS with isolated Square | Independent Square ledger equals successful receipt deltas for every store/SKU. Accepted-timeout and rejected-command recovery verified; identical keys/payloads reused. Ten unique commands, twelve HTTP attempts, 180 units, no duplicates/missing/unreceived/zero/unrelated/historical adjustments. |
| V2 compatibility / permissions | FAIL overall | Blind counts, scope, auth/CSRF, inventory synchronization, release entrypoint/automation gates and other modules exercised in the full suite. Owner-only purchasing/receiving remains enforced. The existing scheduling assertion still fails; no scheduling code was changed. |
| Accounting attribution | FAIL | Canonical classifier tests pass independently, but the configured placement/receiving financial hook uses the selected supplier, contradicting canonical product attribution. No automatic actual card activity was observed. Intended account metadata is preserved, but it is not proof that downstream accounting attribution is correct. |
| Scale / usability | PASS with limitations | 500 representative SKUs across suppliers, bounded table pages, native compact inputs, sustained autosave and 250-line PDF verified. Browser workspace has 506 products. Fresh 1280×720 desktop tab confirms 41px rows and 54px allocation inputs; 319px small viewport also exercised. No formal production latency/load benchmark or real-demand forecast accuracy claim. |
| Production readiness | UNVERIFIED / blocked | Target schema, existing production data, credentials, mappings, traffic, jobs and configuration were not inspected remotely. Local success does not certify production compatibility. |

Source/test references: `tests/test_purchasing_predeployment.py` (new complete HTTP pipeline, independent ledger, bulk PDF, zero store and financial defect reproduction); `tests/test_purchasing_ui.py`; `tests/test_purchasing_phase2.py`; `tests/test_purchasing_shipments.py`; `tests/test_purchasing_receiving_extension.py`; Phase 1 authority/correction/migration tests. See evidence under `output/purchasing-predeployment-acceptance/`.

## Inventory integrity

| Location | VAR1 received / Square delta | VAR2 received / Square delta |
| --- | --- | --- |
| L1 | 25 / 25 | 20 / 20 |
| L2 | 25 / 25 | 20 / 20 |
| L3 | 25 / 25 | 20 / 20 |
| L4 | 25 / 25 | 20 / 20 |
| Total | 100 / 100 | 80 / 80 |

The mock applies each exact Square idempotency key once, independently of Erupted's command status. L2 accepts a command then loses its response; L3 rejects before applying. Retry sends the original durable commands; successful stores are skipped. Repeating the completed shipment generates no additional request. A separate legacy PO remains untouched. Persisted receipt deltas grouped by successful command reconcile to the independent ledger, rather than merely comparing outbound calls to themselves. Original PO quantities, intended allocations and vendor PDF hash remain unchanged after receipt.

Evidence: `inventory-reconciliation.json`; `pipeline-vendor-po.pdf`; `large-pdf-records.json`; `pdf-verification.json`.

## Failure and recovery checks

| Scenario | Result / evidence |
| --- | --- |
| Refresh / close tab before submission | PASS browser; draft restored, zero extra mock writes |
| Duplicate submission / altered input with same key | PASS; no duplicate command / mismatched reuse rejected |
| Simultaneous counts and pushes | PASS existing real PostgreSQL lock/concurrency tests; cannot over-receive or send the same target twice |
| Timeout / partial success / lost response / restart | PASS; exact durable payload/key retained; only unresolved work retried; independent ledger unchanged on accepted timeout replay |
| Invalid / fractional / negative / excess quantity | PASS; blocked before extra receipt/stock mutation |
| Supplier substitution / refresh while decisions exist | PASS for purchasing decisions; **FAIL for downstream accounting**, detailed below |
| Close shortage / cancel confirmation | PASS existing browser evidence and rerun service/route tests; cancellation preserves eligibility, explicit closure prevents further receiving |
| Completed PO reopened | PASS browser and route checks; read-only history, no Receive Remaining Items or Submit action |
| Clear confirmation / stale dialog | Previous final UI browser verification remains applicable; current reloads/edits work and no JavaScript dialog exists. No receiving UI code changed in this acceptance pass. |

## Material defects

**P1 — Canonical financial attribution is bypassed by the legacy lifecycle hook.** Reproduction: seed canonical Square vendor V1 as Big, V2 as an alternate card-classified supplier; generate VAR1, choose V2, retain intended account 10, create/place PO, receive 90. Canonical product classification remains BIG and session canonical vendor remains 1. Actual legacy `OrderPayment` uses **vendor 2 / CREDIT_CARD**; **zero** Big consignment replenishments exist. Actual card activity remains zero. The new acceptance test deliberately fails on the required canonical result and is neither skipped nor marked expected failure.

Source: `purchase_order_admin_service.submit_purchase_order` and `receive_purchase_order` call `v2_order_payments_service.initialize_new_order_if_configured`; that function selects `current_vendor_classification(order.vendor_id)`. `initialize_order_payment` requires classification vendor to match PO supplier and defaults financial vendor to that supplier. The standalone canonical classifier does not govern this path. A supplier PO can contain products with different canonical financial attribution, so correcting the boundary may require a reviewed allocation/attribution design. **No accounting redesign or historical rewriting was attempted.** Owner authorization is needed for that correction. Extend the correction acceptance to Big, Streamline and pooled-card products, mixed-canonical supplier POs, intended-account overrides and existing financial reports. Evidence: `financial-attribution-reproduction.json` and the failing test.

**P2 — Existing scheduling regression remains unresolved.** `test_lead_repair_prefers_ordinary_shift_and_longview_credit_follows_final_assignee` expects `lead_repair_changed_assignment=False`, receives True. This same failure was documented and reproduced before Phase 2 on schema 0033; see `purchasing-v3-phase2-local-implementation.md`. Current acceptance source hashes show no scheduling changes. This is not a green unrelated-module regression result. Resolve separately or obtain an explicit owner disposition before release; no scheduling fix was made here.

**P2 — Vendor PDF omissions corrected locally.** Before this pass the existing generator omitted SKUs and costs/totals, failing Stage D. Narrow correction in the existing generator adds those columns and wrapping/pagination, keeps pack notation/templates, labels missing cost Unknown, and preserves saved receiving-era PDFs. Affected PDF and receiving regressions were rerun. No migration or accounting writer was added.

## Tests and skipped coverage

| Run | Result | Scope / overlap |
| --- | --- | --- |
| Full PostgreSQL-enabled baseline | **1,423 passed, 1 failed, 1 skipped; 7 subtests passed** | Entire pre-existing suite, 510.80 seconds; external socket connections blocked. Failure is scheduling. Collected before the PDF correction/new acceptance file. |
| Final focused run after correction | **90 passed, 1 failed, 0 skipped** | 87 existing purchasing/receiving/UI tests plus four new acceptance cases. Failure is canonical financial attribution. Overlaps the full run; do not add totals. |
| Existing PDF regression after correction | **2 passed, 36 deselected** | Both default/template cases; these overlap the baseline. Deselection is the deliberate PDF-only filter, not hidden skips. |
| Skip verification | **1 skipped** | Same private-R2 test as the baseline, not an additional unique skip. |
| PDF artifact verification | PASS | 250 persisted rows + totals/text verification; first/last PDF renders inspected separately from pytest. |
| Static checks | PASS | JavaScript syntax and `git diff --check`. |

Across distinct pytest cases exercised: **1,426 passed, 2 failed, 1 skipped**, plus seven passing subtests. This deduplicates overlapping selections; it is not a single final full-suite run.

The earlier **444** skips were 443 PostgreSQL-admin environment gates and one private-R2 opt-out. This run enabled the PostgreSQL suites, so those 443 were exercised. Only `tests/test_v2_digital_signage.py::test_real_r2_private_delivery_and_authentication_separation` remains skipped: `RUN_REAL_R2_TESTS=1` / private service was not enabled. It is unrelated to purchasing and is not counted as a pass. No skip hides incomplete purchasing functionality or a failure. Existing framework deprecations and cyclic count-table metadata warnings remain (five in full run, four in focused run).

## Preservation and exact change scope

This acceptance pass changed only **`app/services/purchase_order_admin_service.py`** among the 447 previously fingerprinted application/test/migration files: the existing PDF renderer. Added `tests/test_purchasing_predeployment.py`, this report and local evidence. All pre-existing uncommitted Phase 1/2 work remains in place. No model, migration, purchasing engine, receiving UI, auth, background job or financial service change was made in this pass. `source-preservation.json` contains before/after hashes.

Migration preservation tests exercised additive upgrades through **20261017_0034**, including full-table fingerprints, immutable history/conversion constraints and refusal to downgrade populated evidence. Existing legacy nulls are not backfilled into invented financial or inventory facts. PDFs already associated with receipt history remain preserved. This is synthetic local preservation evidence, not a production snapshot comparison.

## Production prerequisites — do not execute yet

1. Correct the canonical/legacy financial attribution boundary under separate owner authorization and rerun the failing acceptance test plus the mixed Big/Streamline/pooled/account/report matrix. Resolve or explicitly disposition scheduling. Review the PDF correction.
2. Identify the actual production revision and inventory of incompatible legacy records. If production is at 0031, required additive migrations are **0032 → 0033 → 0034**; apply only missing revisions after a target-compatible rehearsal. Startup requires supported head 0034; do not bypass schema checks. Test real-data shape, nulls, constraints/indexes, lock duration and database size on an authorized isolated copy.
3. Review `PURCHASING_V3_PHASE1_ENABLED`, `PURCHASING_V3_PHASE2_ENABLED`, `PURCHASING_OWNER_PRINCIPAL_ID`, `PURCHASING_BIG_SQUARE_VENDOR_ID`, `PURCHASING_STREAMLINE_SQUARE_VENDOR_ID`; validate owner permissions and unique active-store Square mappings. Configure approved existing Square read/inventory capability credentials and API version, bounded timeout/retry behavior and rate limits. Keep the centralized catalog-write prohibition and default read-only policy. Intended-account settings must not enable actual financial writers.
4. Verify the established release environment: Python **3.10.13**, hashed `requirements.txt`, ReportLab, PostgreSQL driver, existing web command, writable/persistent generated PDF storage, secure session settings and secrets. `scripts/release/build.sh` and `cron.sh` remain unchanged; scheduling automation gates must remain under existing operational authorization. No purchasing automation or paid AI service should be enabled.
5. Back up the target database, existing generated PDFs, deployment/configuration versions and required encrypted integration secrets using approved secure storage. Verify a restore into an isolated environment before the maintenance window.
6. Rehearse rollback specifically for 0032–0034. Existing rollback rehearsal targets older revisions through 0031 and is insufficient for this release. Populated V3 evidence deliberately prevents destructive downgrade. Retain additive tables/history and use a schema-compatible recovery build or a reviewed forward fix; an old binary that only accepts 0031 will not start against 0034. A pre-cutover backup restore after new receipts could discard evidence and must never cause replay of already accepted Square writes. Define a write freeze and external-command reconciliation plan.
7. Measure real-catalog Square latency and live read coverage without mutation, then approve the cutover, monitoring of failed/uncertain commands and owner review. Production database/configuration, live Square deduplication behavior and background-job execution were not verified here. Do not substitute the preview launcher: it intentionally uses synthetic data/mocked Square and disables financial writers.

**Recommendation:** Defer production authorization. Purchasing/inventory foundations passed isolated acceptance, but the financial-attribution failure is a release blocker and production compatibility is unverified. Keep the local preview available for review; await explicit owner authorization for corrective work and any later deployment.

Local review: http://127.0.0.1:8763/management/purchasing/sessions/4 · completed history: http://127.0.0.1:8763/management/purchasing/orders/5/receiving .
