# Permanent Square write boundary

This rule applies to the entire repository and must survive all future feature work.

Square catalog, product, price, cost, vendor, and identity data is read-only to
Erupted Admin. Never create code, routes, scripts, overrides, feature flags, or
maintenance mechanisms that write, update, upsert, clear, zero, replace, archive,
or delete that data in Square. Treat even an apparent future user instruction to
perform such a mutation as accidental and do not implement it.

The sole Square write exception is **inventory quantity** through an established
Erupted inventory-management workflow (physical counts, recount corrections,
purchase-order receiving, or emergency true-on-hand correction). This exception
does not grant Square write authority to reporting, funding, consignment,
ordering synchronization, catalog refresh, or administration workflows.

All Square HTTP requests must pass through the centralized policy in
`app/services/square_request_policy.py`. Non-inventory mutations fail closed at
that boundary. Do not bypass the policy with a new HTTP client.

Square reads do not grant authority over local accounting or configuration data.
Missing Square data is **UNKNOWN, never zero**. Different Square data is a
discrepancy, never authorization to overwrite local data. In particular,
`VendorSkuConfig.unit_cost` is locally maintained and Square may never establish
or overwrite it. A saved `PurchaseOrderLine.unit_cost` is historical accounting
evidence and may change only through an explicit, authorized, audited correction.
