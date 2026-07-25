# V1 vendor payment and COGS audit

Status: confirmed current behavior unless labeled unresolved. No accounting behavior is added by this document.

## Payment support matrix

| Capability | Current support | Current owner / representation | Finding |
|---|---|---|---|
| Paid / unpaid | Yes | `purchase_orders.payment_status` | Binary mutable flag |
| Payment date | Yes | `paid_date` on PO | Required when PAID |
| Paid amount | Yes | `payment_amount` on PO | Required when PAID |
| Difference explanation | Yes | `payment_difference_note` | Required when paid amount differs from current active-line calculated total |
| Card or wire | No | None | Payment method cannot be recorded |
| Funding account | No | None | No bank/card/account identity |
| Partial payment | No | None | One amount overwrites prior value; no allocation state |
| Multiple payments per PO | No | None | No payment-event table |
| One payment across POs | No | None | No payment-to-order allocation entity |
| Due/scheduled/disputed/reconciled | No | None | Only PAID and UNPAID |
| Historical correction | Destructive overwrite only | PO fields | Switching to UNPAID clears payment data; no reversal event |
| Audit history | No complete ledger | Updated PO row only | Actor/reason/history are not preserved as accounting events |

Current payments therefore belong only to a purchase order record. They do not belong to a vendor ledger, accounting period, funding account, or COGS record. A future model likely needs a payment plus many allocation rows, but that is proposed and requires owner/accounting approval.

## COGS behavior

The current COGS report searches completed Square orders for the reporting window, takes sold variation quantities, and multiplies them by the **current** preferred vendor mapping unit cost. It groups output using current catalog/category context.

Confirmed consequences:

- It is a live estimate, not a persisted accounting ledger.
- It is not linked to a PO, receipt, vendor invoice, vendor payment, funding account, or accounting period close.
- Changing preferred vendor, unit cost, SKU mapping, or category can change a historical report.
- Missing or duplicate mappings can omit or ambiguously attribute cost.
- Returns, waste/non-sellable inventory, transfers, freight, tax, discounts, and inventory valuation method are not fully allocated here.
- “Last week” is a selected date window, not a closed immutable period.

## Paid/unpaid order totals

The PO amount is recalculated from active lines using current line quantities and costs. Payment fields can therefore be compared with a mutable order total. The difference-note rule records an explanation at edit time but does not preserve the exact compared invoice snapshot independently.

## Reconciliation assessment

No current workflow reconciles bank/card transactions, vendor statements, invoices, receipts, payments, and COGS. There is no defensible current answer for partial payments, combined payments, credits, disputes, or historical correction without overwriting data.

## Proposed boundary

V2 should keep these distinct:

1. approved PO commercial snapshot;
2. vendor invoice and adjustments;
3. payment event with method and funding account reference;
4. payment allocations across invoices/POs;
5. receipt and inventory valuation facts;
6. COGS calculation/allocation and accounting-period status.

Whether COGS is recognized at sale, receipt, invoice, payment, or period close is unresolved accounting policy. Implementation must not encode a choice until owner/accounting approval.
