# Immutable consignment sales and cost attribution checkpoint

## Readiness classification

**SQUARE-DERIVED COGS REPORTING NOT READY FOR OWNER SIGN-OFF pending representative production-clone and
Square sandbox evidence. The corrected V1-backed order-payment and receipt-replenishment subset is ready
for principal-scoped owner preview after migration.**

Finalized reports are reproducible from immutable local sale, return, cost, vendor-attribution,
report-link, and inventory-snapshot records. Production email is intentionally absent. V1 remains
canonical and `order_payments_v2` remains default-disabled.

## Square sources

Synchronization reads complete `Order` objects from `POST /v2/orders/search`, filtered to
`COMPLETED` and incrementally sorted/filtered by `updated_at`.

- Sale identity: `Order.id + OrderLineItem.uid`.
- Payment reference: `Order.tenders[].payment_id`, when present.
- Product identity: `OrderLineItem.catalog_object_id`; catalog identity is copied only at import.
- Economic amounts: line `gross_sales_money`, `total_discount_money`, `total_tax_money`, and
  `total_money`.
- Return identity: return-order ID + `OrderReturn.uid + OrderReturnLineItem.uid`.
- Original-sale match: `OrderReturn.source_order_id + OrderReturnLineItem.source_line_item_uid`.
- Refunds without itemized returns are persisted as `SOURCE_INCOMPLETE` and cannot finalize.

Square documents that Search Orders returns itemized sales and returns and that offline POS orders
can be transmitted up to 72 hours late. Finalization therefore requires a successful sync after the
report end plus the 72-hour lag window.

References: [Search Orders](https://developer.squareup.com/reference/square/orders-api/search-orders),
[Order](https://developer.squareup.com/reference/square/objects/Order), and
[OrderReturn](https://developer.squareup.com/reference/square/objects/OrderReturn).

## Immutable model and attribution

- `consignment_sale_facts`: one immutable economic snapshot per Square order line.
- `consignment_return_facts`: itemized COGS reversal or explicitly unresolved refund.
- `vendor_variation_assignments`: effective-dated vendor and consignment classification.
- `vendor_variation_costs`: effective-dated historical consignment cost.
- `consignment_sales_sync_state`: incremental successful-through watermark and failure state.
- `consignment_report_lines`: frozen vendor/store/variation/cost aggregates.
- `consignment_report_fact_links`: exact sale/return provenance and signed COGS contribution.
- `consignment_inventory_snapshots`: separate informational inventory facts at report time.

At import, assignment and cost are selected at the UTC transaction timestamp and copied into the
fact. Missing, ambiguous, non-consignment, excluded, and source-incomplete outcomes remain explicit.
Reruns can enrich unresolved facts after effective history is supplied but never rewrite already
attributed economic snapshots. Owner transaction overrides require a reason and are blocked after
finalization. If a linked draft fact changes, that preview becomes non-finalizable and must be
regenerated.

Effective-period overlap is rejected by the service and duplicate starts are constrained in the
database. Owner actions are serialized through normal application transactions; a database-native
range exclusion constraint is a possible later hardening step for concurrent external writers.

## Return and refund rules

Matched returns copy vendor, product, SKU, and cost from the original sale fact. Full and partial
returns reverse `returned quantity × original unit cost`. A return is never attributed from today’s
mapping when its original sale exists. Unmatched returns and refunds without itemized quantity remain
blocked until explicitly linked or otherwise resolved.

## Report formulas and lifecycle

- Net units = immutable attributed sale units − immutable attributed returned units.
- Current-period COGS = sale fact COGS snapshots − return fact COGS reversals.
- Prior unreplenished COGS = ledger balance immediately before the period start.
- Replenishment applied in period = eligible ledger entries within the period.
- Ending unreplenished COGS = ledger balance before the report period end + current-period COGS.
- Inventory value = report-time signed quantity × report-time mapped consignment cost. Inventory is
  stored separately and never used to infer historical sales.

Finalization requires no blocker codes, successful Square coverage through the exclusive report end,
the 72-hour offline lag window, no finalized overlap, and exact reconciliation between report total
and signed fact links. It freezes the report and creates one uniquely constrained `COGS_GENERATED`
ledger entry. Voiding creates one `VOID_REVERSAL`; it never edits the original ledger entry.

## Blockers and warnings

Finalization blockers include missing vendor, missing cost, ambiguous assignment, incomplete source,
unmatched return, unitemized refund, incomplete synchronization, offline-order lag, overlapping
finalized ranges, stale draft facts, negative period COGS, and fact-link reconciliation failure.
Negative or ambiguous current inventory remains visible as an inventory warning and does not alter
period COGS.

## Routes

- `GET /v2/consignment/attribution`
- `POST /v2/consignment/attribution/sync`
- `POST /v2/consignment/attribution/assignments`
- `POST /v2/consignment/attribution/costs`
- `POST /v2/consignment/attribution/sales/{fact_id}`
- `POST /v2/consignment/attribution/returns/{fact_id}`
- `POST /v2/consignment/{vendor_id}/reports`
- `GET /v2/consignment/{vendor_id}/reports/{report_id}`
- `POST /v2/consignment/{vendor_id}/reports/{report_id}/finalize`
- `POST /v2/consignment/{vendor_id}/reports/{report_id}/void`
- `POST /v2/consignment/{vendor_id}/reports/{report_id}/test-email`

Every mutation requires principal feature exposure, owner/admin authorization, CSRF, object/vendor
validation, and audit evidence. Store principals retain guarded 404 behavior.

## Migration, tests, and performance

- Migration: `20260728_0011`, down revision `20260728_0010`, one head.
- Adds eight V2-owned tables, eight report snapshot columns, and a captured-email body snapshot without
  changing V1 tables.
- Full suite with disposable PostgreSQL enabled: 319 passed, 1 skipped, 2 pre-existing FastAPI
  deprecation warnings.
- PostgreSQL migration/schema comparison, Python/Jinja compilation, and diff hygiene pass.
- Synchronization uses Square pagination at 500 orders per request, source-key indexes, idempotent
  lookups, and one transaction/savepoint per requested run. A 1,200-sale synthetic insertion segment
  completed in 5.079 seconds; production-scale performance remains unproved without the clone.

## Completed local owner-preview verification

1. Upgraded disposable PostgreSQL from `0009` through `0010` and `0011`; downgrade/re-upgrade and schema
   comparison passed.
2. Confirmed the feature remains globally disabled; no principal exposure was added.
3. Create effective-dated assignment and cost history for a test variation.
4. Synchronize a closed date range and inspect every unresolved outcome.
5. Verify a matched partial return copied the original sale cost.
6. Change the current vendor, cost, product name, and SKU; confirm imported facts remain unchanged.
7. Generate a report after the 72-hour lag window and reconcile every fact link and inventory snapshot.
8. Finalize twice and confirm only one `COGS_GENERATED` entry exists.
9. Capture the test email and confirm no network delivery occurred.
10. Void the report and confirm the original plus `VOID_REVERSAL` reconstruct a zero net COGS impact.
11. Confirmed store and unexposed principal denial contracts and CSRF dependencies for all mutations.

The detailed evidence and remaining external blockers are in
[the verification checkpoint](./consignment-verification-checkpoint.md).

## Verification checkpoint

- No production email provider or credentials.
- Synchronization is owner-triggered; no webhook or scheduler is introduced.
- Unitemized Square refunds require manual resolution and cannot be fabricated into item COGS.
- Disposable PostgreSQL migration, schema comparison, synthetic multi-page backfill, reconciliation,
  finalization, void, and captured-email checks pass. See
  [the verification checkpoint](./consignment-verification-checkpoint.md).
- A disposable representative production-data clone and Square sandbox/controlled non-production account
  were unavailable and remain canary blockers.

Recommended next checkpoint: repeat the recorded rehearsal on the sanitized production clone and Square
sandbox, obtain owner accounting sign-off, and only then consider separately approved principal canary
exposure. Production email delivery remains out of scope.
