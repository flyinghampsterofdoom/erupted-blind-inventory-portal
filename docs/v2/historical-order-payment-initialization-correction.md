# Historical order-payment initialization correction

## Root cause

The original Order Payments list called `backfill_placed_order_payments()` and committed during a GET. That classified every eligible historical V1 purchase order as an immutable invoice before the owner had configured the vendor's financial treatment. The detail and consignment GET routes also performed synchronization writes.

## Corrected read contract

`GET /v2/order-payments`, its repeated refresh, the financial-assignment queue GET, order detail GET, and consignment GET are non-mutating. Historical V1 orders without a V2 record are displayed as `UNINITIALIZED` or `BLOCKED`; they have no paid/unpaid control. Cost, scope, vendor, and proposed states are derived for display only.

## Vendor classification

`vendor_payment_classifications` records an explicit owner decision separately from `vendor_payment_settings`, which remains the current default pointer. Each version captures method, category, masked label, Terms duration, consignment designation, effective date, optional internal note, actor, and timestamp. Changing the current default supersedes the current classification version but never rewrites an initialized order.

## Financial-assignment queue

`/v2/order-payments/backfill` presents qualifying purchase orders as a processing queue. Each order exposes its read-only Original Vendor, a Financial Vendor dropdown, a Payment Method dropdown, collapsed Optional Notes, and Save. Saving immediately removes a completed order and advances to the next queue item. Owners can also select orders across original vendors and apply one Financial Vendor, Payment Method, and optional note from the bulk panel. There is no preview step, review wizard, confirmation checkbox, or step-based UI.

Each save recomputes every safety rule and creates one `order_payment_backfill_operations` audit operation for the submitted queue batch, including when selected orders span original vendors. Every created/skipped/blocked order is linked to that operation through `order_payment_backfill_results`, whose immutable proposed-state snapshot retains the order's actual source vendor. The success message reports the operation ID, financial settlement, and payment method while explicitly confirming that purchase-order identity and receipt lineage were preserved and that the action was audited.

Initialization is blocked for unconfigured vendors, inactive or missing methods, unreliable vendors, ineligible V1 states, incomplete saved V1 line costs, existing V2 records, consignment orders with no canonical receipt, and consignment receipts that lack reconciling canonical store-allocation quantities. Confirmed consignment rows use replenishment treatment and synchronize only canonical received quantities at saved V1 line cost. They never acquire paid/unpaid fields.

## Future orders

The existing V1 submit-to-`IN_TRANSIT` lifecycle event initializes ordinary orders after V1 has deliberately placed them. Consignment orders remain uninitialized at placement and initialize only from the V1 receive lifecycle after a positive canonical receipt exists; an order discarded before receipt never enters consignment. The V2 observer writes no V1 field. Missing/ineffective classification, incomplete costs, or unreconciled receipt allocations leave the order uninitialized. List viewing is never a fallback trigger.

## Exact production cleanup

Migration `20260728_0012` contains the closed payment/order/vendor ID mapping recorded during the blocked preview, the exact UTC creation timestamp, actor 6, initialization-only event text, and null financial-action fields. Cleanup runs only when either none of the target IDs exist or all 39 payments and exactly one matching event each satisfy every check. It refuses partial sets, altered provenance, vendor defaults, extra audit references, or downstream replenishment/receipt/ledger references. It deletes only the 39 matching initialization events and 39 matching payments. Payment methods and all V1 records are excluded.

The migration is intentionally unable to reconstruct removed defect rows on downgrade; those rows were invalid preview artifacts, not financial history.
