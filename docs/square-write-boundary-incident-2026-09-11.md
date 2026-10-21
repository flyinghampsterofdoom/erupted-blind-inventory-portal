# Square data-authority incident and permanent boundary

## Root cause

`sync_vendor_sku_configs_from_square()` treated Square vendor cost as authoritative
for `VendorSkuConfig.unit_cost`. It also collapsed every absent or unusable cost to
`Decimal('0')`. Existing local costs were then overwritten whenever the derived
value differed. The behavior entered the repository in commit `c9f6b83` on
2026-02-24.

The same February commit installed both confirmed callers:

1. `POST /management/ordering-tool/vendors/sync` called the mapping sync after
   refreshing vendors.
2. `generate_purchase_orders()` called the mapping sync for the selected vendors
   before generating lines. Both standard and full-stock generation routes use
   this function.

An additional unsafe cost mutation existed in
`refresh_purchase_order_lines_from_catalog()`: the explicit draft catalog refresh
could replace an existing `PurchaseOrderLine.unit_cost` with a Square vendor cost.

## August 3+ forensics

### Confirmed

- Repository-wide call searches and Git history show that Consignment, Funding,
  reconciliation, FIFO, report calculation/regeneration/finalization, combined
  reports, funding source-readiness handling, and the shared Square sales refresh
  do not call `sync_vendor_sku_configs_from_square()` directly or indirectly.
- Funding/Consignment Square refresh calls `synchronize_square_facts()`, whose
  Square operation is the read-only `POST /v2/orders/search` and whose local writes
  are immutable sale/return facts and sync-state data.
- The only callers of the destructive mapping sync at current HEAD, and throughout
  history after its introduction, are vendor sync and PO generation.
- `ORDERING_VENDORS_SYNCED` is audited. Standard and full-stock PO generation are
  audited as `ORDERING_PURCHASE_ORDERS_GENERATED` and
  `ORDERING_PURCHASE_ORDERS_FULL_STOCK_GENERATED`.
- The local `.env` identifies the configured database as a production Render
  PostgreSQL host. A read-only forensic transaction was attempted, but the private
  Render hostname is not resolvable from this workspace. No database query ran and
  no production state changed.

### Strongly inferred

If the Juicehead mapping costs were correct after approximately 2026-08-03 and
later disappeared, one of these operations ran after the last manual edit:

- legacy Ordering Tool **Sync Vendors**, or
- standard/full-stock purchase-order generation including the Juicehead vendor.

The exact operation and timestamp can be resolved from production audit data if it
is retained. Consignment/Funding work occurring in the same period is temporally
correlated but not a caller of the destructive function.

### Unknown / not recoverable from this checkout

The exact August invocation cannot be selected between the two confirmed triggers
without production `audit_log` rows (and, secondarily, mapping/PO timestamps or
application logs). Commit timestamps establish when code existed, not when a user
executed it.

## Square mutation inventory

All mutation calls currently present target the same permitted Square endpoint.

| File | Function | Endpoint | Method | Data changed | Trigger | Reachable | Classification |
|---|---|---|---|---|---|---|---|
| `app/services/count_square_sync_service.py` | `_push_rows_to_square` | `/v2/inventory/changes/batch-create` | POST | Inventory quantity (`PHYSICAL_COUNT`) | approved physical count, recount, automatic recount closeout | Yes | PERMITTED |
| `app/services/admin_store_count_service.py` | `submit_count` | `/v2/inventory/changes/batch-create` | POST | Inventory quantity (`PHYSICAL_COUNT`) | admin store-count submission | Yes | PERMITTED |
| `app/services/purchase_order_admin_service.py` | `receive_purchase_order` | `/v2/inventory/changes/batch-create` | POST | Inventory quantity (`ADJUSTMENT`) | PO receiving/retry | Yes | PERMITTED |
| `app/services/ordering_emergency_service.py` | `push_emergency_draft` | `/v2/inventory/changes/batch-create` | POST | Inventory quantity (`PHYSICAL_COUNT`) | explicit emergency true-on-hand push | Yes | PERMITTED |

No Square catalog, product, price, cost, vendor, identity, category, tax, archive,
or delete mutation endpoint is implemented at current HEAD. Before this correction,
generic POST transports had no centralized endpoint policy and could have issued
such a request if called with a prohibited path. Every current raw Square HTTP
transport now invokes `enforce_square_request_policy()`. The policy allowlists
known read/search POST endpoints, requires an explicit inventory capability for
`batch-create`, validates that its changes are only `PHYSICAL_COUNT` or
`ADJUSTMENT`, and rejects every other non-GET request.

## Cost authority and recovery

Existing vendor mapping costs are never changed by Square synchronization. New
Square-discovered mappings store `NULL` until an owner establishes local cost.
New PO lines snapshot only the local mapping cost; they no longer fall back to a
Square vendor cost. Catalog refresh no longer changes saved PO-line cost.

Potential authoritative recovery sources, in descending usefulness, are:

1. a pre-destruction `PurchaseOrderLine.unit_cost` snapshot tied to the applicable
   Juicehead receipt/lot;
2. an explicit `FUNDING_PO_LINE_COST_CORRECTED` audit event (old/new value, actor,
   reason, timestamp);
3. finalized Consignment/Funding line or fact snapshots tied to the same PO lot;
4. a database backup/WAL image from after manual entry and before the trigger;
5. a generated PO PDF/export whose line cost can be tied unambiguously to the lot.

A current zero mapping alone cannot distinguish a legitimate manual zero from an
automatically manufactured zero, and a Square cost cannot establish the manual
cost. Recovery is deterministic only where one of the historical sources above
contains a cost tied unambiguously to the same Juicehead SKU/variation and effective
lot/date. No production value should be changed until that evidence is reviewed.

## Production forensic query procedure (do not run during deployment)

In a read-only transaction, identify the Juicehead vendor and mapping rows, then
inspect `audit_log` after the last known manual edit for the three Ordering actions
above. Correlate `audit_log.metadata->'vendor_ids'` and created order IDs with
`purchase_orders`,
`purchase_order_lines`, mapping `updated_at`, Funding cost-correction audits, and
finalized report snapshots. Export the evidence before proposing a correction.
Any eventual repair must be explicit, reviewed, audited, and separate from this
code checkpoint.
