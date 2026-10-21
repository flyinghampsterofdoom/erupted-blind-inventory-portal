# V1 receiving audit

Status: confirmed from current routes, services, models, templates, and tests unless labeled otherwise. V1 remains canonical.

## Physical delivery to final inventory

| Step | Confirmed V1 behavior | Persistence / side effect | Gap or risk |
|---|---|---|---|
| Find order | Management user opens an `IN_TRANSIT` purchase order | Reads PO, lines, allocations | No shipment, appointment, carton, or receipt identity |
| Identify product | Manual line selection or barcode scan | Scan matches PO SKU, variation ID, line/mapping GTIN; unknown scan creates a synthetic line | Wrong-vendor and mis-shipped goods are not distinct dispositions |
| Enter quantity | Quantity overwrites `store_received_qty`; scan increments by mapping pack size | Allocation row is the active receiving record | No append-only receipt event; concurrent edits can overwrite |
| Allocate stores | Existing allocations identify destination stores | Priority order is HWY99, Longview, Andresen, SR503; overage favors HWY99 | Priority is embedded policy; cross-store intent is not explained or versioned |
| Record partials | Any numeric received quantity may be saved | PO remains `IN_TRANSIT` until Square targets succeed | Short, backordered, damaged, and pending are only inferred from numbers |
| Push inventory | Receive action sends positive quantities to Square as `NONE -> IN_STOCK` adjustments | One `square_sync_events` attempt record per PO line/store target | Local and Square commits are not atomic |
| Finish | All attempted positive targets successful changes PO to `SENT_TO_STORES` | Later attempts skip targets with a recorded success | State means Square write success, not physical reconciliation or accounting close |

## Edge-case behavior

- **Partial receipts:** confirmed numeric support, but no receipt header, receipt date, shipment, or line disposition is written.
- **Overages:** accepted numerically; barcode allocation sends excess to the first priority store. No approval threshold exists.
- **Shortages/backorders:** representable only as received less than expected. No reason, backorder quantity, or vendor follow-up state exists.
- **Damaged goods:** no active field or workflow.
- **Unknown products:** scan creates an “Unexpected Barcode” PO line. This mutates the order rather than recording a quarantined receipt exception.
- **Wrong-vendor products:** no explicit model or resolution workflow.
- **Reopening:** no receipt reopen operation. An `IN_TRANSIT` order can be edited repeatedly; `SENT_TO_STORES` has no supported reversal.
- **Duplicate submission:** successful targets are skipped on later receive attempts using prior success events. This helps only when a success was recorded locally.
- **Cancel scan defect:** a scan can add a full pack, while canceling the scan subtracts one unit. Pack sizes above one can leave an incorrect quantity.
- **Manual reconciliation:** failed targets can be retried, but an outcome-unknown remote request has no confirm-before-retry workflow.
- **Audit history:** Square attempts are logged; ordinary quantity edits, scan/cancel actions, and receiving decisions have no complete append-only audit.

## Atomicity and replay boundary

The critical transaction crosses two systems:

```text
local received quantity -> Square inventory request -> local sync-event result -> PO status
```

Square may accept a request before a timeout or local database failure. In that case the remote inventory is changed but no durable local success necessarily exists. Retrying can duplicate inventory. Conversely, local quantities can be saved before a Square failure, leaving the PO and remote inventory deliberately divergent. The deterministic PO receiving key narrows ordinary replay risk, but there is no remote reconciliation query tied to the command before retry.

## Receipt-table finding

`purchase_order_receipts` and `purchase_order_receipt_lines` exist, but the active V1 workflow writes allocation quantities instead. Production row use is unresolved and must be measured before reuse or retirement. V2 should use explicit immutable receipt submissions and line dispositions, not assume these dormant-looking tables are empty.

## V2 implication

Receiving should be local-only first: receipt header, append-only receipt lines, exception dispositions, store allocation, optimistic concurrency, and reconciliation state. Square inventory must remain a later command boundary with explicit approval, deterministic business idempotency, outcome-unknown handling, and audit evidence.
