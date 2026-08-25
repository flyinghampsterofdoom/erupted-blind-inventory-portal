# Period COGS reports for funding accounts

This owner-preview module provides manually dated COGS reports for Consignment and Credit Card funding accounts. It remains inside the principal-scoped `order_payments_v2` feature; both report action gates default to off. It creates no schedules, automatic periods, payments, finalizations, or emails.

The owner selects an account, exact sales start and end dates, optional stores, and an optional exact SKU or product filter. A Consignment report first loads qualifying internal purchase orders whose current `order_payments.vendor_id` is the selected account's financial vendor. The unique exact-normalized SKUs on saved, non-removed, positively ordered purchase-order lines are the complete eligibility boundary. Square sales and itemized returns are queried only for that set. The saved purchase-order line cost supplies effective cost; the latest assigned order cost not later than the transaction date is used when the same SKU occurs on multiple orders. Product names, the original purchase-order vendor, global mappings, and partial SKUs never determine Consignment inclusion.

Credit Card reports continue to use their selected account's effective-dated funding mappings. Consignment reports never fall back to those mappings for eligibility. No qualifying assigned orders or no usable purchase-order SKUs fails closed before a report row is created. Missing SKU or cost lines remain visible as setup issues in the saved purchase-order source summary.

Overlapping non-voided reports produce an acknowledgement warning but do not remove previously reported sales. Proceeding saves the overlap acknowledgement and calculates the complete selected range.

Finalized reports preserve account and date scope, assigned purchase-order and line snapshots, eligible SKUs, source fact IDs, cost and inventory snapshots, calculated and adjusted values, actor, and timestamp. Later order reassignment changes only future draft eligibility and cannot rewrite saved finalized records. Adjustments, payments, replenishment, card activity, corrections, reversals, and voids remain append-only or preserve their original rows. Unfinalized drafts with no downstream financial references may be deleted; deletion cascades only draft-owned calculation rows and records a lightweight audit event. Finalized reports cannot be deleted and use void instead.

Credit-card FIFO gaps are retained as draft-owned, per-sale report exceptions instead of being exposed as variation-ID errors. Each exception snapshots the catalog identity, sale time, affected quantity, and sold-versus-received evidence. A pending exception blocks finalization. **Ignore for This Report** records an audited owner decision and excludes only the unmatched quantity from that draft; it does not repair inventory history, and the sale may appear as an exception in a future report. **Include Anyway** requires an owner-entered positive unit cost, records an audited manual cost-basis line, and never creates or consumes a purchase-order lot. **Discard Report** uses draft deletion and leaves Square facts, purchase orders, receipts, and inventory unchanged. Resolved exception decisions are copied into the finalized report snapshot.

Source boundaries are:

- Square catalog supplies SKU and base identity.
- Square sales and itemized returns supply customer transaction facts.
- Square inventory supplies current quantities only.
- Version 1 ordering supplies saved internal purchase-order lines, SKUs, quantities, and costs without V2 mutation.
- V2 `order_payments.vendor_id` supplies the current financial assignment while the original purchase-order vendor remains unchanged.
- V2 funding mappings supply Credit Card SKU-to-account cost history; they do not establish Consignment eligibility.
- V2 reports preserve period calculations and source links.
- V2 ledger rows preserve payments, replenishment, charges, credits, and reversals.
- V2 funding accounts store owner-entered APR terms; carrying-cost figures are estimates, while actual interest requires a ledger charge.

Credit Card purchase balance entries are created only at the existing explicit owner-confirmed order-payment initialization point, never by a GET or page view. Full card numbers are not stored.
