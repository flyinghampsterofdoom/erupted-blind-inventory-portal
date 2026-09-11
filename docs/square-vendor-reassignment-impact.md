# Square vendor reassignment impact

## Authority and invariants

Square is authoritative only for the current vendor assignment used for future
ordering. Erupted remains authoritative for unit cost, pack size, minimum order
quantity, par configuration, accounting, and historical records. The sync is a
Square read followed by local configuration changes; it must never send vendor,
catalog, product, price, cost, or identity mutations to Square.

For each processed SKU, the Square-assigned local vendor becomes the one active
default mapping. Other mappings are retained but are no longer default. Historical
purchase orders and financial records are never reassigned.

## Deterministic configuration precedence

1. An existing destination `VendorSkuConfig.unit_cost` wins when non-null.
2. Otherwise, the prior default mapping's non-null local cost is carried forward.
3. A newly created destination mapping inherits the prior default mapping's pack
   size and minimum order quantity. An existing destination mapping keeps its own
   pack size and minimum order quantity.
4. Existing destination ParLevel rows with manual values, a manual source, or a
   manual lock win and are not overwritten.
5. Otherwise, prior-default ParLevel state is cloned to a missing destination row
   or carried into an existing dynamic destination row. This includes manual and
   suggested levels, confidence state and score, lock/source state, streaks, and
   the principal that last maintained the configuration.
6. If stale duplicate prior defaults exist, the source is deterministic: active
   defaults first, then the most recently updated row, then the highest row ID.

The management Vendor Sync audit event includes the reassignment details (SKU,
old vendor ID, and new vendor ID); the audit record supplies actor and timestamp.
No Square price or cost is included.

## Dependency classification

| Dependency | Classification | Effect |
|---|---|---|
| `VendorSkuConfig.vendor_id` / `is_default_vendor` | Future-state/current mapping | Current catalog, lifecycle, recommendation, and normal PO generation queries select the active default mapping, so they move to the Square-assigned vendor. |
| `VendorSkuConfig.unit_cost` | Future-state local accounting | Never sourced from Square. Existing destination cost wins; otherwise known prior local cost carries forward; unknown remains null. |
| Pack size / minimum order quantity | Future-state ordering configuration | Existing destination values win; new mappings inherit prior-default values. |
| `ParLevel.vendor_id` | Future-state ordering configuration | Prior effective per-store/global configuration is cloned or carried forward unless explicit destination configuration already exists. Old-vendor rows remain intact. |
| Current PO generation | Future-state/current mapping | Normal generation uses only active defaults and therefore creates future orders for the new vendor. Full-stock generation may still show retained active non-default mappings only when that explicit mode is requested. |
| In-transit calculation | Mixed current/historical | Existing POs retain their original vendor. For a current default SKU, open quantities are aggregated across historical PO vendors so an old-vendor shipment still suppresses duplicate future ordering. Non-default full-stock rows retain vendor-scoped inbound math. |
| Stock coverage and V2 ordering dashboard | Future-state/current mapping | Both use the current active default. Cross-vendor historical inbound for the SKU is attributed to that current default only for recommendation math. |
| Inventory valuation / COGS current lookup | Future-state/current mapping | Default-first local-cost lookup resolves to the reassigned mapping, whose local cost follows the precedence above. No historical snapshots are rewritten. |
| Sales-by-vendor and targeted demand reports | Current mapping behavior | Vendor-filtered current mapping reports follow the selected mapping/default ordering. Square sales facts are read-only. |
| Historical purchase orders, lines, allocations, and receiving | Historical snapshot behavior | Unaffected. The sync does not update these tables; vendor, saved line cost, allocation, and receipt identity remain unchanged. |
| OrderPayment and payment history | Historical snapshot behavior | Unaffected. These records derive scope from their saved PO/payment relationships, not from today's default mapping. |
| Funding reports and finalized COGS snapshots | Historical snapshot behavior | Unaffected. Existing PO, report-line, fact-link, exclusion, adjustment, and payment records are not updated. |
| Consignment replenishment and settlement history | Historical snapshot behavior | Unaffected. Existing vendor/PO-linked replenishment, receipt, allocation, ledger, report, and settlement records are not updated. |
| Square write policy | Unaffected | The centralized request policy remains mandatory. Catalog/vendor reads remain reads; only approved inventory-quantity workflows may mutate Square. |

## Open and in-transit purchase orders

An open PO placed with Vendor A remains a Vendor A PO, including line cost and
store allocations. After Square changes the default to Vendor B, normal future
ordering selects Vendor B. Recommendation math subtracts the still-open Vendor A
quantity for the same store and SKU from Vendor B's need, preventing a duplicate
order while the historical PO remains untouched.

This attribution is calculation-only. It does not move or relabel the PO, its
lines, its allocations, receiving history, or any financial record.
