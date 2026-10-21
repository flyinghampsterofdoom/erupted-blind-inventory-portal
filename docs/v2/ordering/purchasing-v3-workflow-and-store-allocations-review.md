# Purchasing V3 workflow and editable store allocations

> Workflow clarification: The later owner directive specifies direct-to-store receiving with no warehouse or transfer confirmation. The central-receiving gap assessment below is historical and superseded by `purchasing-v3-direct-store-receiving-local-review.md`.

2026-10-17 — local implementation and verification; no deployment or production writes.

## Conclusion

Compact editable store allocations are implemented. Total is read-only and immediately sums those allocations. Existing saved decisions are preserved. V3 connects to the existing consolidated PO, vendor PDF, internal store split and destination-based receiving workflows. The requested **central receiving → separate store distribution/transfer confirmation** workflow is only partially implemented; no missing receiving or distribution functionality was added.

Preview: http://127.0.0.1:8763/management/purchasing/sessions/4 . Supplier Cascade remains editable; supplier Evergreen links to local draft PO 3 used for verification. Generate Order remains available at purchasing home. All data is synthetic; Square reads are mocked and external mutations/financial writers are blocked by the local launcher.

## Workflow verification from source, models, routes and tests

| Step | Implementation / V3 connection | End-to-end evidence and limits |
| --- | --- | --- |
| 1. Store needs | **Fully implemented for the approved current store scope.** `purchasing_session_service.generate`, existing forecast/math services and `purchasing_inventory_service.load_incoming_supply`; `/management/purchasing`. | 500-SKU fixture verifies all four needs; current Square evidence, velocity and eligible outstanding incoming are separate. Missing observations remain unknown. No inventory reconstruction from historical documents. New store columns do not automatically expand the engine's approved scope. |
| 2. Consolidated supplier PO | **Fully implemented, connected.** `convert_group`, `PurchaseOrder`, `PurchaseOrderLine`, `PurchaseOrderStoreAllocation`; POST `/management/purchasing/sessions/{id}/groups/{supplier}/po`. | Browser edit immediately before creation produced PO 3 with 302 units and store plan 60/75/90/77. Stale-version creation is refused. Creating a draft has no Square or actual-balance effect. |
| 3. Vendor PDF | **Fully implemented, connected.** Existing editor `/management/ordering-tool/orders/{id}`, download `/pdf`, `ensure_current_purchase_order_pdf` / `_generate_purchase_order_pdf`. | Actual PDF 3 extracted and rendered: one supplier order, Mint Cartridge quantity `302 (61 pk @ 5)`, no four-store columns or separate store orders. PDF generator unchanged. Pack annotation uses existing ceiling behavior; manual totals are not silently rounded to a whole pack. |
| 4. Shipment to one central receiving location | **Partially implemented.** PDF contains the existing business address, but PO/receipt models have no explicit central custody or receiving-location workflow. | A single consolidated vendor order exists. The PDF's business address alone does not establish central receiving inventory or distribution readiness. Physical shipment itself cannot be proven by local tests. |
| 5. Record actual receipt, partials and discrepancies | **Implemented as destination-based receiving, connected; partial relative to the requested central workflow.** Existing PO editor Receiving; POST `/received-quantities`; `save_purchase_order_received_quantities`, corrections, closures and receipt models. | Existing tests verify repeated partial deliveries, duplicate keys, over/under receipt, outstanding quantities, corrections, closure, audit immutability and permissions. Added V3 integration creates a PO with allocations 2/3/4/8 and saves one unit for store 1: received 1, outstanding 16, saved plan unchanged, no Square write on save. There is no independent unallocated central receipt balance. |
| 6. Store-by-store distribution breakdown | **Partially implemented, connected.** Internal PO editor store split and Receiving ordered/received/outstanding columns use saved allocations. | PO 3 internal records preserve 60/75/90/77. Saved intended plan provides a breakdown; edits and destination receipt corrections exist. No distinct central received vs held vs dispatched vs physically transferred quantities or transfer-confirmation records exist. No dedicated distribution document/workflow was found in these paths. |
| 7. Authorized inventory adjustments | **Existing receiving posting implemented; separate distribution stage missing.** Owner-only `/receive` and `/receive-retry-failed`, `receive_purchase_order`, durable `SquareSyncEvent`. | Save Receiving leaves Square unchanged. Explicit Push to Square posts each saved destination delta using durable idempotency keys; failure/uncertain-outcome recovery is tested. This can increase destination store stock before a distinct physical transfer confirmation, because that stage does not exist. It must not be described as an enforced central-distribution boundary. All test sends were mocked; no live mutation was made. |

The full requested physical process cannot yet be marked end-to-end PASS. Existing partial receiving and its explicit Square posting work within their current destination-based semantics.

Primary source references: `app/services/purchasing_session_service.py` (`_allocation`, `generate`, `update_line`, `convert_group`); `app/services/purchase_order_admin_service.py` (`get_purchase_order_detail`, `save_purchase_order_edits`, `_generate_purchase_order_pdf`, `save_purchase_order_received_quantities`, `correct_purchase_order_receiving`, `close_purchase_order_remainder`, `receiving_history`, `_prepare_receiving_operations`, `receive_purchase_order`); `app/routers/management.py` (order detail/PDF/receiving routes); `app/templates/management_ordering_order_detail.html`; `app/models.py` (`PurchaseOrder`, `PurchaseOrderLine`, `PurchaseOrderStoreAllocation`, `PurchaseOrderReceipt`, `PurchaseOrderReceiptLine`, `PurchaseOrderReceiptAllocation`, `SquareSyncEvent`).

## Targeted UI correction

Store cells now contain right-aligned 54 px numerical inputs within the existing 65 px columns. Row height remains **41 px**, with **16 visible rows at 1440 × 1080**, matching the previous screenshot. Removed the redundant main-table Total Need / editable Order Qty pair and replaced it with one read-only Total. No new editing sections, per-row buttons or mobile redesign. Sticky headers, paging, supplier grouping and search/filter/sort remain.

Each edit stages the complete allocation map and its numerical sum through the existing atomic draft PATCH and audit machinery. The total updates before the 450 ms autosave debounce. Blank, negative, fractional and over-limit values block saving and PO creation; invalid entries remain recoverable locally. Tab retains normal cell order. Supplier substitution sends the current complete plan so supplier pack settings cannot silently redistribute an owner-selected plan. Confirmed server saves update supplier units and estimated cost. Navigation and Create PO flush pending edits, and version checks reject conflicts. Existing failure recovery is retained.

Original raw store needs remain in expandable calculation evidence. Initial editable values are the **existing engine-derived saved allocations**, including existing pack rounding and any prior owner override. Raw need and pack-adjusted allocations can differ; converting raw need into new allocations on page load would silently rewrite established decisions, so that was deliberately avoided. A new draft is initialized through the existing engine allocation function. The recommendation engine and refresh policy were not changed.

No migration or reconciliation is needed for consistent saved drafts. A bounded read-only check of the isolated preview found zero allocation-total mismatches. Production was not inspected. If an inconsistent legacy draft is encountered, row edits are blocked and the saved total mismatch is explicitly disclosed in details; no automatic repair occurs, and existing conversion validation rejects it. Inactive saved destinations remain visible as `(saved)` columns so quantities cannot disappear from the total. Newly active stores without saved allocation evidence remain disabled/unknown. Earlier unconfirmed total-only browser edits remain retained for explicit review rather than being silently interpreted as a new store plan.

## Test evidence

- **79 passed**, no skips, 4 warnings: relevant PostgreSQL purchasing UI, Phase 2, receiving extension and existing admin receiving tests. Log: `output/purchasing-store-allocations/relevant-regression.log`.
- **973 passed, 444 skipped, 7 subtests passed**, 5 warnings: general regression. Log: `general-regression.log`. 443 skips require the explicit PostgreSQL-admin integration environment; one requires private real-R2 access. Skips are not passes.
- **7 passed**, no skips, 3 warnings: final UI suite including the subsequently added V3-to-partial-receiving integration test. Log: `final-ui-tests.log`. Test selections overlap; do not add counts as unique tests. Full all-PostgreSQL scheduling suite was not rerun.
- JavaScript syntax and `git diff --check` pass. Browser verified immediate sum, multiple stores, zero/negative validation, totals over 1,000, keyboard Tab, saved return navigation, supplier move, refresh preservation and an allocation edit immediately followed by Create PO.
- Local PO 3: consolidated 302; allocation map 60/75/90/77; intended account retained. Vendor PDF content and rendering checked. No receipt, Square event, funding actual activity or funding ledger entry was created in the interactive preview. The receiving integration uses separate disposable fixtures and mocked Square posting.
- 508 representative synthetic catalog SKUs / 506 recommendation rows; only 50 rows loaded per open supplier page. This confirms bounded rendering, not a production load benchmark.

New tests: `test_inline_store_plan_sum_validation_refresh_and_supplier_preservation` verifies zero, negative/fractional/boolean/blank rejection, original evidence, supplier preservation, refresh and latest-version conversion. `test_v3_allocations_connect_to_existing_partial_destination_receiving` verifies the actual V3 connection and existing destination receiving semantics. Existing tests in `test_purchasing_receiving_extension.py` cover partials, idempotency, corrections, retry, permissions and historical preservation.

## Gaps requiring separate authorization

1. A central receipt/custody stage that records actual vendor shipment quantities without declaring destination stores received them.
2. An explicit distribution/transfer workflow, including partial dispatch, actual destination confirmation and custody reconciliation, distinct from intended allocations and vendor receipts.
3. An enforced rule tying store inventory posting to the appropriate confirmed physical stage. Current explicit receiving posting is owner gated and durable but does not enforce a separate transfer milestone.
4. A receiving destination/address configuration and any dedicated distribution printout, if required beyond the existing internal store grid. The present PDF prints the established business address.

These are reported for owner decision; none was implemented. The existing receiving flow remains available with its established semantics. Production readiness for the complete requested central workflow is deferred pending resolution.

## Changed files and artifacts

Only UI changes: `app/static/v2/purchasing.js`, `app/static/v2/purchasing.css`, and `app/templates/purchasing/session.html`. Extended `tests/test_purchasing_ui.py`; added this report. No model, migration, calculation, accounting, receiving, PDF-generation or Square-policy change.

Screenshots: `output/purchasing-store-allocations/compact-editable-store-table.jpg` and `original-recommendations-and-owner-plan.jpg`. Supporting local PDF, extracted text, logs and `verification.json` are in the same directory. Temporary PDF rendering is not part of the implementation.

Recommendation: review and accept the targeted allocation UI locally; require a separately authorized design for the missing central-receiving/distribution boundary before accepting the complete physical workflow. No deployment or production-data changes were made.
