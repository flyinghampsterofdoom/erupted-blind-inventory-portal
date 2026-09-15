# Consignment reports: configured vendor products

## Report contract

`CONFIGURED_VENDOR_PRODUCTS_V1` answers: for products belonging to this vendor, what sold in this period?

The selected account identifies the vendor. Active configured mappings across active vendors establish each variation's single default owner using the same `single_default_mapping` rule as the standalone Vendor Inventory Report. Alternate purchasing relationships and historical PO/Funding/account associations cannot add a product. A conflicting default or unresolved configured identity affecting this vendor is reported explicitly; unrelated ambiguity is ignored.

Explicit catalog variation identity takes precedence. Identity-less facts may use globally unambiguous normalized SKU resolution. An explicit different variation is never admitted by matching SKU. Catalog identity is read from the persisted catalog, not inferred from historical sales attribution or product-name similarity.

Sales and returns come from persisted Square facts inside the requested inclusive business-date range. Product filters select configured products; store filters select activity and inventory for those products. Zero-activity products appear by selected active store, with zero sales and UNKNOWN inventory when no current observation exists. Empty ownership/filter results are valid empty reports. Earlier sales do not consume capacity. Positive, zero, and signed negative observations are valid. Returns need no historical allocation. Missing/nonfinite quantities affecting matched activity and unknown store identity under a store filter are explicit scoped errors.

## Money and inventory

The current locally maintained vendor/product cost is snapshotted per line. It is never copied into Square, saved PO costs, or configuration. Net activity times that cost gives the configured-cost financial amount. A genuinely unknown payable amount remains NULL in report, line, and source-link storage and displays UNKNOWN. Quantities remain visible. A configured product with no net activity can have a known zero obligation even if its unit cost is unknown. Financial finalization requires a known amount.

Observed inventory is secondary. Each selected product/store needs a persisted count whose saved and time-derived freshness are both FRESH under the Ordering inventory contract (24 hours). Missing, stale, critical, or nonfinite counts are UNKNOWN. Negative counts remain negative. Any unknown component makes the aggregate inventory total unknown; available per-store observations remain visible. The oldest included observation timestamps the snapshot. Inventory does not determine sales or block financial finalization.

The payment-position workflow still floors an amount payable at zero; it does not erase the signed calculated result or turn negative net activity into an exception.

## Historical evidence and explicit new calculations

Consignment GETs never normalize reports. Neither old nor new Consignment drafts are automatically rewritten. Legacy FIFO lines, pending exceptions, Ignore/Include decisions, finalized snapshots, fact links, payments, and source data remain unchanged.

Both creation entry points create a fresh calculation. If an earlier report overlaps, the owner uses the standard creation form to acknowledge the overlap. The legacy entry point redirects there with an explanation. Old report pages link to that workflow. Combined Consignment creation may preserve finalized members, but creates fresh calculations instead of reusing old drafts. Duplicate finalized fact posting remains prohibited. The existing Credit Card refresh path and guard remain unchanged.

Legacy Consignment drafts cannot be posted as though they used the new semantics. They remain available as evidence; the user can create a new version without deleting them. Only new-semantics drafts with complete source coverage, unchanged source-sync revision and relevant configured ownership/identity/cost signature, known required financial amount, and no duplicate finalized source posting can finalize.

## Schema and release

Migration `20260915_0025` follows `20260911_0024`. It makes eight report-owned financial/inventory columns nullable in `funding_reports`, `funding_report_lines`, and `funding_report_fact_links`. No source table, PO, vendor configuration, historical report value, or exception is updated. Existing numeric defaults remain available to legacy calculations, while explicitly unknown values persist as NULL.

A downgrade uses NOT NULL validation and refuses to discard unknown values. It never converts NULL to zero. Deployments must apply the migration before running this code. No production migration is authorized by implementation/testing alone.

## Tests whose former expectations are superseded

- Financial assignment and funded-PO membership tests become configured-owner membership tests; unrelated PO/account links no longer confer eligibility.
- The 75 detected / 65 allocated / 10 exceptions expectation becomes 75 counted without capacity exceptions and successful financial finalization.
- Missing-layer, FIFO-layer consumption, prior-period capacity, and original-allocation return expectations become direct period-activity tests.
- PO-cost Consignment expectations become local configured-cost snapshots. Tests for explicit PO correction continue against the Credit Card workflow where PO cost remains authoritative.
- Automatic Consignment draft exception removal becomes preservation of existing exceptions and an explicit fresh calculation.
- Zero-activity presentation includes the selected active stores. An absent store snapshot makes aggregate inventory UNKNOWN rather than silently contributing zero.
- No configured products produces an empty report rather than a missing-funded-order failure.
- The schema-head assertions in the Square cost boundary and Digital Signage schema contract tests advance to `20260915_0025`; the original vendor-cost migration and source write restrictions remain tested.

These changes do not waive source coverage, snapshot/history, payment, duplicate-posting, or Credit Card regression tests.

### Exact superseded test cases

| Previous test | Replacement expectation |
| --- | --- |
| `test_consignment_75_square_units_with_sufficient_funding_reports_75` | Configured ownership counts 75 independent of funding. |
| `test_consignment_account_mapped_sale_without_funded_layer_is_not_silently_dropped` | Owned activity counts without a funded layer or an exception. |
| `test_consignment_draft_recalculation_removes_obsolete_nonmember_exception` | Preserve saved exceptions; create a fresh report. |
| `test_consignment_fifo_partially_consumes_multiple_funded_po_layers` | Sum period observations without layer allocation. |
| `test_consignment_membership_ignores_default_purchasing_vendor_and_cost` | Current configured default ownership and local configured cost are authoritative. |
| `test_consignment_prior_period_sales_consume_capacity_without_double_payment` | Prior sales cannot consume current-period eligibility; duplicate finalized posting remains blocked. |
| `test_consignment_production_shape_reconciles_75_to_65_plus_10_exceptions` | Count all 75 without capacity exceptions. |
| `test_consignment_purchasing_mapping_alone_does_not_create_membership` | Active configured ownership establishes membership without PO funding. |
| `test_consignment_return_recredits_original_funded_layer` | Period returns count without original FIFO allocation. |
| `test_consignment_sale_before_receipt_evidence_is_still_allocated` | Count matched period sales without receipt allocation. |
| `test_consignment_unassigned_po_is_not_an_opening_inventory_layer` | Display observed inventory independently of PO assignment. |
| `test_duplicate_sku_across_assigned_orders_does_not_duplicate_sales` | Explicit variation identity and globally unique fallback prevent duplicate or incorrect membership. |
| `test_financial_reassignment_changes_future_eligibility_only` | Financial reassignment does not redefine configured ownership; finalized evidence stays fixed. |
| `test_legacy_consignment_create_entry_point_uses_funded_report_membership` | Both creation entry points use configured membership. |
| `test_legacy_consignment_draft_without_funded_fifo_semantics_cannot_be_finalized` | All old-semantics drafts require a fresh version without deleting evidence. |
| `test_no_assigned_orders_or_no_usable_purchase_order_skus_fails_closed` | An empty configured universe is a valid empty report; no PO is required. |
| `test_original_purchase_order_vendor_may_differ_from_financial_account` | Historical PO/account associations cannot override the configured owner. |
