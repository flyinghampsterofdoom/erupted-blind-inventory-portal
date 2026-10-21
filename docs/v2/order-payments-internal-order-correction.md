# V2 internal-order payment integration correction

Date: 2026-07-28

## Readiness classification

**READY FOR PRINCIPAL-SCOPED OWNER PREVIEW AFTER MIGRATION DEPLOYMENT.**

This classification covers payment-method setup, vendor defaults, the V1 order-payment list and detail,
ordinary paid/unpaid tracking, Terms due dates, consignment order identification, and V1 receipt-based
replenishment. It does not classify Square-derived COGS reports as production-accurate.

## Verified source boundary

V1 order data comes from `purchase_orders`, `purchase_order_lines`,
`purchase_order_store_allocations`, `vendors`, and `stores`. The V1 ordering service functions used as the
behavioral contract are `submit_purchase_order`, `save_purchase_order_received_quantities`,
`scan_purchase_order_barcode`, `cancel_purchase_order_barcode_scan`, `_sync_line_received_totals`, and
`receive_purchase_order` in `app/services/purchase_order_admin_service.py`.

The exact receipt source is each `purchase_order_store_allocations` row and its
`store_received_qty`. The reconciled line aggregate is `purchase_order_lines.received_qty_total`.
Although `purchase_order_receipts` and `purchase_order_receipt_lines` exist, no active V1 service writes
them; they are not used as financial source records.

V2 reads those records but never writes them. A new V2 receipt freezes the source allocation ID, PO line
ID, store ID, prior and current received quantities, positive delta, captured PO-line cost, received value,
observation time, actor, report allocation, and ledger lineage.

## Corrected behavior and reconciliation

- Every placed V1 order is lazily initialized exactly once through the unique V1-PO relationship.
- Ordinary orders initialize `UNPAID`; no V1 invoice status, paid date, or paid amount is inferred or
  changed.
- The vendor's active default method is snapshotted when initialization occurs. Terms also freeze the term
  duration and compute due date from the V1 order date.
- List and detail views use the V1 order, saved V1 lines, captured historical line costs, and store scope.
- Consignment defaults create a distinct replenishment identity and never expose the invoice paid selector.
- Ordered-but-unreceived quantity does not settle COGS.
- Each positive V1 receipt delta is valued at the saved V1 PO-line cost and allocated against the oldest
  open finalized report first.
- A partial $20.00 receipt against $15.00 open COGS reconciled to $15.00 applied plus $5.00 replenishment
  credit, with $20.00 received, $40.00 ordered, and `PARTIALLY_APPLIED` status.
- A repeat synchronization created no second receipt or ledger entry. A later reduction of a previously
  settled V1 received quantity produced an integrity warning requiring a typed correction.

Square Orders remain used solely for immutable customer-sale and itemized-return facts used by COGS.
Square inventory may supply current on-hand snapshots. No Square order search supplies the vendor order
list, line quantities, receipt state, payable state, or replenishment value.

## Verification evidence

- Full suite with disposable PostgreSQL 16.12: **331 passed, 1 skipped**.
- Focused provenance, order-payment, receipt-lineage, immutable-fact, and authorization suite: **36 passed**.
- Fresh migration plus supported downgrade/re-upgrade and schema comparison: **passed**.
- Migrated head: `20260728_0011`; migrated schema and SQLAlchemy model comparison: exact match.
- Jinja compilation for corrected list and detail templates: passed.
- Feature contract: global feature disabled; a named owner pair grants access; store principals retain 404;
  every mutation retains feature, owner/capability, and CSRF dependencies.
- External COGS mutation contract: `V2_CONSIGNMENT_COGS_ACTIONS_ENABLED` defaults false; UI and server-side
  route guards independently enforce the internal-only preview boundary.

The configured local environment currently has neither global nor principal exposure. The configured
database uses an internal Render hostname that is not resolvable from this workstation, so its current
revision and active owner principal were not guessed and no production mutation was attempted.

## Deployment step and remaining COGS blockers

Deploy the application and migrations first. Verify the existing active owner principal in that
environment, add only `<owner-id>:order_payments_v2` to `V2_PRINCIPAL_FEATURES`, leave
`V2_ENABLED_FEATURES` empty, and smoke-test one existing ordinary V1 order and one partial consignment
receipt. Rollback is removal of that one principal-feature pair; V1 records remain untouched.
Keep `V2_CONSIGNMENT_COGS_ACTIONS_ENABLED=false` so Square synchronization, attribution mutations,
COGS-linked adjustments, report generation/finalization/void, and email capture remain visibly disabled
and server-blocked during the internal preview.

The only unresolved functional evidence belongs to Square-derived COGS sales/report accuracy: controlled
Square sale/return/refund reconciliation, representative historical backfill performance and recovery,
current-inventory retrieval, and owner sign-off on a real-data final report. Those items do not block the
internal-order owner preview described above.
