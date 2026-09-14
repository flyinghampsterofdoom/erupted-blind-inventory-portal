# Period COGS reports for funding accounts

This owner-preview module provides manually dated COGS reports for Consignment and Credit Card funding accounts. It remains inside the principal-scoped `order_payments_v2` feature; both report action gates default to off. It creates no schedules, automatic periods, payments, finalizations, or emails.

The owner selects an account, exact sales start and end dates, optional stores, and an optional exact SKU or product filter. A Consignment report first loads qualifying internal purchase orders whose current `order_payments.vendor_id` is the selected account's financial vendor. The unique exact-normalized SKUs on saved, non-removed, positively ordered purchase-order lines are the complete eligibility boundary. Square sales and itemized returns are queried only for that set. The saved purchase-order line cost supplies effective cost; the latest assigned order cost not later than the transaction date is used when the same SKU occurs on multiple orders. Product names, the original purchase-order vendor, global mappings, and partial SKUs never determine Consignment inclusion.

Credit Card reports include only catalog variations on qualifying, non-removed, positively ordered PO lines explicitly paid by that card, within the existing vendor assignment. They count Square sales and itemized returns only in the requested period and filters. Each variation uses the latest mapped PO date, then PO line ID, to select its saved cost, independently of transaction or receipt chronology. Multiple mapped POs do not multiply sales. Each card observes its own mapped product set independently. Missing PO identity blocks calculation; missing selected cost blocks only when that product has period activity. Inventory history, funded capacity, other cards' POs, and unsold inventory rows are outside this calculation.

Overlapping non-voided reports produce an acknowledgement warning but do not remove previously reported sales. Proceeding saves the overlap acknowledgement and calculates the complete selected range.

Finalized reports preserve account and date scope, assigned purchase-order and line snapshots, eligible SKUs, source fact IDs, cost and inventory snapshots, calculated and adjusted values, actor, and timestamp. Later order reassignment changes only future draft eligibility and cannot rewrite saved finalized records. Adjustments, payments, replenishment, card activity, corrections, reversals, and voids remain append-only or preserve their original rows. Unfinalized drafts with no downstream financial references may be deleted; deletion cascades only draft-owned calculation rows and records a lightweight audit event. Finalized reports cannot be deleted and use void instead.

Credit-card reports no longer generate FIFO or funded-capacity exceptions. Legacy card drafts are rebuilt and their obsolete capacity decisions removed when opened, reused by Combined Reports, or finalized. This normalization is audited; finalized reports remain unchanged. Period returns deduct the selected PO cost without requiring historical sale allocation. **Discard Report** continues to delete only draft-owned rows.

Calculation trace: `calculate_combined_report` reuses or creates vendor reports through `calculate_report`. Previously, `_credit_card_fifo_scope` loaded cross-account PO layers and `_populate_credit_card_funding_report` consumed historical sales against remaining quantity, generating `FundingReportFifoException` rows displayed as “need owner action.” Reports now use `_credit_card_product_scope` and period-only product aggregation. The separate `credit_card_inventory_summary` → `_credit_card_fifo_scope` → `_apply_funding_allocation_history` path remains unchanged, as do Consignment calculations and funding ledger/payment behavior.

Source boundaries are:

- Square catalog supplies SKU and base identity.
- Square sales and itemized returns supply customer transaction facts.
- Square inventory supplies current quantities only.
- Version 1 ordering supplies saved internal purchase-order lines, SKUs, quantities, and costs without V2 mutation.
- V2 `order_payments.vendor_id` supplies the current financial assignment while the original purchase-order vendor remains unchanged.
- V2 order payment methods establish Credit Card PO membership; saved PO line costs supply report costs. Legacy funding SKU mappings do not override this scope.
- V2 reports preserve period calculations and source links.
- V2 ledger rows preserve payments, replenishment, charges, credits, and reversals.
- V2 funding accounts store owner-entered APR terms; carrying-cost figures are estimates, while actual interest requires a ledger charge.

Credit Card purchase balance entries are created only at the existing explicit owner-confirmed order-payment initialization point, never by a GET or page view. Full card numbers are not stored.
