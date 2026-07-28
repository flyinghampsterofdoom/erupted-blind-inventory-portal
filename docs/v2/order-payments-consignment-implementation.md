# V2 Order Payments and Consignment settlement implementation

> This records the `20260728_0010` foundation checkpoint. The immutable-facts implementation and current
> readiness classification supersede its earlier accounting blockers; see
> [the current verification checkpoint](./consignment-verification-checkpoint.md).

## Readiness

**Historical `0010` foundation record; not the current canary classification.**

V1 remains canonical. The module is default-disabled and exposed only through the principal-scoped
`order_payments_v2` feature key. Implementation or migration does not authorize production exposure.

## Implemented scope

- Reusable Wire, Credit Card, Debit Card, Terms, and Consignment methods.
- Storage is limited to a display name, institution/company, nickname, and optional last four digits.
- Vendor default method, validated report email, and internal notes.
- Repeatable lazy initialization of placed purchase orders.
- Captured payment-method/category/terms snapshots and immutable payment transition events.
- Invoice paid/unpaid workflow with portal-local effective date and separate UTC audit timestamp.
- Read-only saved purchase-order line/cost detail.
- Consignment orders classified as replenishment instead of invoices.
- Rolling consignment ledger, replenishment record, report allocation, excess-credit carry-forward,
  exceptional cash settlement, inventory view, and summary.
- Actual received quantities at saved PO line cost drive replenishment value. Ordered-but-unreceived
  value remains pending.

## Routes

| Method | Route | Purpose |
|---|---|---|
| GET | `/v2/order-payments` | Placed-order payment and replenishment list |
| POST | `/v2/order-payments/{payment_id}` | Explicit invoice payment update |
| GET | `/v2/order-payments/{order_id}` | Read-only saved order detail |
| GET/POST | `/v2/payment-methods` | Method list and create |
| POST | `/v2/payment-methods/{method_id}/active` | Activate/deactivate |
| GET/POST | `/v2/vendors/{vendor_id}/payment-settings` | Vendor defaults |
| GET | `/v2/consignment` | Consignment vendor summaries |
| GET | `/v2/consignment/{vendor_id}` | Inventory, replenishments, and ledger |
| POST | `/v2/consignment/{vendor_id}/cash-settlements` | Exceptional owner-confirmed settlement |

Every mutation requires feature exposure, owner/admin authorization, and CSRF validation. A store
principal receives guarded 404 behavior even if a capability is accidentally granted.

## Exact balance definitions

- `COGS generated to date` = sum of `COGS_GENERATED` ledger entries created only from finalized,
  non-voided reports.
- `Replenishment applied` = sum of `REPLENISHMENT_APPLIED` ledger entries backed by allocation rows.
- `Unreplenished COGS` =
  `max(finalized COGS - replenishment applied - cash settlements - approved credits, 0)`.
- `Available replenishment credit` =
  `max(credit created - credit used, 0)`.
- `Current consignment inventory value` = signed current quantity from
  `ordering_current_inventory` multiplied by the uniquely mapped active consignment unit cost.
- `Pending replenishment` = `max(ordered captured cost - received captured cost, 0)`.

Received value is allocated against the oldest finalized report balance first. Each allocation links
the replenishment and report. Excess received value creates replenishment credit; it never makes
unreplenished COGS negative and is never represented as cash or payment.

## Internal-order source boundary

The purchase-order side does not call Square Orders. It reads the existing internal ordering records:

- `purchase_orders`: vendor, placed/order date, V1 lifecycle status, and internal identity.
- `purchase_order_lines`: saved product, variation, SKU, ordered quantity, captured `unit_cost`,
  aggregate `received_qty_total`, and missing quantity.
- `purchase_order_store_allocations`: store scope and the canonical active V1 per-store
  `store_received_qty` values.
- `vendors` and `stores`: display identity for the V1 order and destination scope.

The active V1 writers are `submit_purchase_order`, `save_purchase_order_received_quantities`,
`scan_purchase_order_barcode`, `cancel_purchase_order_barcode_scan`, `_sync_line_received_totals`, and
`receive_purchase_order` in `purchase_order_admin_service.py`. The first receipt lineage source is the
exact `purchase_order_store_allocations.id`; `purchase_order_lines.received_qty_total` must reconcile to
the sum of those store rows before any settlement is recorded.

`purchase_order_receipts` and `purchase_order_receipt_lines` are not used: repository inspection confirms
that the current V1 receiving services never write them. Treating those unused design tables as canonical
would lose the actual production workflow.

For every positive per-store received-quantity delta, V2 freezes the V1 order line, allocation, store,
prior/current/delta quantity, captured unit cost, value, receipt observation, applied report/ledger rows,
and any excess-credit ledger row. A decrease after settlement is never netted silently; it blocks further
allocation pending an owner-reviewed typed correction.

Square remains valid only on the sales side:

- Ordering V2 persists point-in-time per-store Square inventory and refresh evidence.
- Immutable `consignment_sale_facts` and `consignment_return_facts` retain itemized customer sales,
  returns/refunds, dates, location, and effective-dated cost attribution for COGS.
- Square inventory may supply current on-hand quantities for the separate inventory snapshot.
- No Square customer-order object supplies a vendor PO, vendor, ordered/received quantity, order cost,
  payment status, or replenishment identity.

## Migration and rollback

- Revision: `20260728_0010`
- Down revision: `20260725_0009`
- Adds the V2 payment/settlement tables without rewriting V1 purchase orders. The corrected migration also
  adds immutable receipt, receipt-line, and receipt-allocation provenance tables.
- Foreign keys use restrict semantics for historically referenced payment methods.
- Missing order-payment rows are created lazily and only for placed orders; nothing is inferred paid.
- Downgrade removes only the twelve new V2 tables and indexes.

## Verification

The corrected full suite with disposable PostgreSQL enabled is 331 passed and 1 skipped. It exercises a
fresh migration, supported downgrade/re-upgrade paths, schema comparison, V1 initialization idempotency,
ordinary invoice transitions, terms dates, per-store partial receipts, oldest-first allocation, excess
credit, immutable lineage, owner authorization, and store denial.

Owner-preview deployment sequence:

1. Upgrade a disposable clone from `20260725_0009` to `20260728_0010`.
2. Run `TEST_POSTGRES_ADMIN_URL=... python -m pytest tests/test_schema_migration_postgres.py`.
3. Run schema comparison against the disposable migrated reference.
4. Verify the existing active owner principal, then add only
   `V2_PRINCIPAL_FEATURES=<owner-id>:order_payments_v2`; leave `V2_ENABLED_FEATURES` unchanged.
   Keep `V2_CONSIGNMENT_COGS_ACTIONS_ENABLED=false` until the external COGS evidence is approved.
5. Create one method of each category and validate masking/deactivation.
6. Assign invoice, Terms, and Consignment defaults to test vendors.
7. Open Order Payments and confirm safe initialization and historical line costs.
8. Receive a partial consignment PO and verify only received captured cost reaches the ledger.
9. Receive value above unreplenished COGS and reconcile allocation plus excess credit.
10. Confirm store principals and unexposed owner principals receive 404.

No production email environment variables were added. Internal-order payment and receipt-replenishment
preview is not blocked by Square sandbox evidence. Representative Square verification remains required
only for consignment sale/return attribution, current inventory retrieval, and final COGS report accuracy.
