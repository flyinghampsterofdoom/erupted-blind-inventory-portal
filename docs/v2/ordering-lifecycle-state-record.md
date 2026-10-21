# Ordering lifecycle and state record

This record describes actual V1 transitions. It does not impose a new lifecycle.

## Standard purchase-order states

Declared enum values:

- `DRAFT`
- `IN_TRANSIT`
- `RECEIVED_SPLIT_PENDING`
- `SENT_TO_STORES`
- `COMPLETED`
- `CANCELLED`

Implemented route transitions:

```text
DRAFT --submit--> IN_TRANSIT --all Square receive targets succeed--> SENT_TO_STORES
   \                    \
    \--hard delete-------\--hard delete

IN_TRANSIT --partial Square failure--> IN_TRANSIT
IN_TRANSIT --failed-only retry succeeds--> SENT_TO_STORES
```

No active service transitions a PO to `RECEIVED_SPLIT_PENDING`, `COMPLETED`, or `CANCELLED`.

## State behavior matrix

| State | Meaning in actual code | Created by | Valid implemented next state | Editable | Main table | History | Payments | Reopen/delete/correct |
|---|---|---|---|---:|---:|---:|---:|---|
| DRAFT | Local vendor PO under preparation | Generation or Stock Coverage handoff | IN_TRANSIT | Yes | Yes | Same combined table | Detail UI hides invoice | Hard delete; line corrections |
| IN_TRANSIT | Submitted PO; PDF created; awaiting/recording receipt | Submit route | SENT_TO_STORES | Yes | Yes | Same combined table | Editable | Hard delete; receive and line corrections |
| RECEIVED_SPLIT_PENDING | Declared but unused | No active route | None implemented | No defined behavior | Would display | Same table | Service would allow invoice | No route |
| SENT_TO_STORES | All attempted positive received quantities successfully added to Square | Receive/retry | None implemented | No line/receive UI | Yes | Same table | Service allows, overview hides payment display | No reopen/delete |
| COMPLETED | Declared but unused | No active route | None | No defined behavior | Would display | Same table | Service allows | No route |
| CANCELLED | Declared but unused | No active route | None | No defined behavior | Would display | Same table | Service allows | No route |

`list_purchase_orders` does not distinguish current versus history. It returns the newest 100 standard orders of every status. The main route merges them with the newest emergency drafts and truncates the combined result to 100.

## Line and allocation state

- Lines use `removed` rather than a separate lifecycle.
- A normal save removes an untouched line when ordered and received quantities are both zero.
- Explicit removal zeros ordered, received, in-transit, allocation, and store-received quantities.
- A received overage/manual line with ordered zero is retained.
- Removed lines can be restored by adding the same SKU while the order is DRAFT or IN_TRANSIT.
- `in_transit_qty` is derived as `max(ordered - received, 0)`.
- Store allocation `variance_qty` is `allocated - expected`.
- No row version or transition event table protects concurrent edits.

## Receiving state

- Received quantities exist primarily in `purchase_order_store_allocations.store_received_qty`.
- `purchase_order_lines.received_qty_total` is recalculated as the sum of active allocation values.
- `purchase_order_receipts` and `purchase_order_receipt_lines` are not used by the current service.
- Barcode scanning chooses the first store below its allocated quantity according to a name-based priority. Once all stores are filled, it favors a specific priority store and marks the scan as overage.
- A barcode pack scan adds `pack_size` units.
- Cancellation always subtracts one unit, not the recorded scan increment.

## Payment state

Payment is a separate two-value field, not part of `PurchaseOrderStatus`:

- `UNPAID`
- `PAID`

PAID requires date and amount. If paid amount differs from current calculated line cost total, a difference note is required. Switching back to UNPAID clears date, amount, and note. There is no payment-event history, partial-payment model, invoice number, payee, method, attachment, accounting sync, or permission separate from `management.admin`.

## Emergency state

```text
DRAFT --all target writes succeed--> PUSHED
DRAFT --any target write fails-----> DRAFT
```

Emergency drafts remain editable after partial success. A subsequent push creates new idempotency keys for every target and can replay previously successful physical counts.

## Audit/transition history

`audit_log` records route-level action names, actor, IP, time, and selected metadata. It does not form a complete state-transition ledger and generally lacks before/after values. `square_sync_events` is the durable integration attempt record for receive and emergency writes.
