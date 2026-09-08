# Period COGS reports for funding accounts

This owner-preview module provides manually dated COGS reports for Consignment and Credit Card funding accounts. It remains inside the principal-scoped `order_payments_v2` feature; both report action gates default to off. It creates no schedules, automatic periods, payments, finalizations, or emails.

The owner selects an account, exact sales start and end dates, optional stores, and an optional exact SKU or product filter. A Consignment report first loads qualifying internal purchase orders whose current `order_payments.vendor_id` is the selected account's financial vendor. The unique exact-normalized SKUs on saved, non-removed, positively ordered purchase-order lines are the complete eligibility boundary. Square sales and itemized returns are queried only for that set. The saved purchase-order line cost supplies effective cost; the latest assigned order cost not later than the transaction date is used when the same SKU occurs on multiple orders. Product names, the original purchase-order vendor, global mappings, and partial SKUs never determine Consignment inclusion.

Credit Card reports use catalog variation identity and the quantities and costs on purchase orders assigned to funding accounts. Sales are allocated to the oldest outstanding funded order quantity for the same variation, across funding accounts, without requiring the sale to occur after the order or receipt timestamp. Unassigned purchase orders are not treated as opening inventory. Consignment reports never fall back to Credit Card mappings for eligibility. No qualifying assigned orders or no usable purchase-order variations fails closed before a report row is created. Missing identity or cost remains a blocking setup issue.

Overlapping non-voided reports produce an acknowledgement warning but do not remove previously reported sales. Proceeding saves the overlap acknowledgement and calculates the complete selected range.

Finalized reports preserve account and date scope, assigned purchase-order and line snapshots, eligible SKUs, source fact IDs, cost and inventory snapshots, calculated and adjusted values, actor, and timestamp. Later order reassignment changes only future draft eligibility and cannot rewrite saved finalized records. Adjustments, payments, replenishment, card activity, corrections, reversals, and voids remain append-only or preserve their original rows. Unfinalized drafts with no downstream financial references may be deleted; deletion cascades only draft-owned calculation rows and records a lightweight audit event. Finalized reports cannot be deleted and use void instead.

Credit-card allocation exceptions are retained only when sales exceed configured funded capacity (or when malformed return data cannot be reconciled), not when a sale predates a receipt. Each capacity exception snapshots catalog identity, sale time, affected quantity, and sold-versus-funded evidence. A pending exception blocks finalization. **Ignore for This Report** records an audited owner decision and excludes only the over-capacity quantity from that draft. **Include Anyway** requires an owner-entered positive unit cost and records an audited manual cost-basis line without creating or consuming a purchase-order quantity. Old drafts containing only pending chronology-era exceptions are rebuilt under the corrected semantics when finalization is attempted; finalized history and drafts containing completed owner decisions are not rewritten. **Discard Report** accepts a blank optional reason, deletes only draft-owned rows, and leaves Square facts, purchase orders, receipts, and funded quantities unchanged.

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
