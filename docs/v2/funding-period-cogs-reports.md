# Period COGS reports for funding accounts

This owner-preview module provides manually dated COGS reports for Consignment and Credit Card funding accounts. It remains inside the principal-scoped `order_payments_v2` feature; both report action gates default to off. It creates no schedules, automatic periods, payments, finalizations, or emails.

The owner selects an account, exact sales start and end dates, optional stores, and an optional exact SKU or product filter. Completed Square sales and itemized returns are matched only by exact normalized SKU against effective-dated V2 mappings. Product names and partial SKUs never determine inclusion. Missing SKU, missing mapping, conflicting mappings, and missing effective cost remain visible report exclusions.

Overlapping non-voided reports produce an acknowledgement warning but do not remove previously reported sales. Proceeding saves the overlap acknowledgement and calculates the complete selected range.

Finalized reports preserve account and date scope, included mapping and source IDs, cost and inventory snapshots, calculated and adjusted values, actor, and timestamp. Later mapping changes cannot rewrite those saved records. Adjustments, payments, replenishment, card activity, corrections, reversals, and voids remain append-only or preserve their original rows.

Source boundaries are:

- Square catalog supplies SKU and base identity.
- Square sales and itemized returns supply customer transaction facts.
- Square inventory supplies current quantities only.
- Version 1 ordering retains internal purchase orders, saved costs, and receipts without V2 mutation.
- V2 financial assignment retains original vendor, financial vendor, and payment method separately.
- V2 funding mappings supply effective SKU-to-account cost history.
- V2 reports preserve period calculations and source links.
- V2 ledger rows preserve payments, replenishment, charges, credits, and reversals.
- V2 funding accounts store owner-entered APR terms; carrying-cost figures are estimates, while actual interest requires a ledger charge.

Credit Card purchase balance entries are created only at the existing explicit owner-confirmed order-payment initialization point, never by a GET or page view. Full card numbers are not stored.
