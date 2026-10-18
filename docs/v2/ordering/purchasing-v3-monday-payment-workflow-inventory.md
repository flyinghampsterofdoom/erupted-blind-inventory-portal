# Monday COGS payment workflow inventory

October 17, 2026 · Source inspection only; no production actions or new payment UI.

**The consolidated four-account Monday screen is not implemented in the inspected code.** No matching route/template was found in the Purchasing V3 worktree, canonical application checkout or other local application checkouts. The owner clarified that it is a requirement, not an assumed existing screen, and excluded building it from the accounting correction. The routes below actually exist and are registered by `app/main.py`; no new Monday URL has been invented.

Supplier, purchase-lot financing, recommended COGS payment and actual payment are distinct. The current correction protects financing and leaves payment recording explicit. Creating a PO or receiving stock does not record an actual payment.

## Existing report and payment routes

Route definitions are in `app/routers/v2_funding_reports.py` unless marked legacy.

| Method and actual path | Current support |
| --- | --- |
| GET `/v2/funding-accounts` | Account list/summary; not four editable Monday payments |
| GET `/v2/funding-accounts/{account_id}` | One account's assigned purchases, reports, activity and account/vendor payment controls |
| GET `/v2/funding-accounts/reports/new` | Report form; account/vendor, explicit date range and optional store/SKU scope |
| POST `/v2/funding-accounts/reports` | Calculate one account/vendor report, verify sales coverage and handle overlaps; commit that report |
| GET `/v2/funding-accounts/{account_id}/reports/combined/new` and POST `/v2/funding-accounts/{account_id}/reports/combined` | Combine vendor reports **within one credit-card account**; not Big + Streamline + both cards |
| GET `/v2/funding-accounts/{account_id}/reports/{report_id}` | Individual/combined report detail, balances and payment controls |
| POST `/v2/funding-accounts/{account_id}/reports/{report_id}/finalize` | Finalize that report with existing source/readiness checks |
| POST `/v2/funding-accounts/{account_id}/payments` | One account's actual Payment/Replenishment, explicit amount/date and report allocations |
| POST `/v2/funding-accounts/{account_id}/vendor-payments/{vendor_id}` | Edited amount or paid-in-full against that account/vendor's outstanding reports |
| POST `/v2/funding-accounts/{account_id}/reports/{combined_report_id}/vendor-payments/{report_id}` | Pay one vendor child of that account's combined report |
| POST `/v2/funding-accounts/{account_id}/reports/{report_id}/combined-payments` | One actual payment allocated across vendor reports within one card account |
| POST `/v2/funding-accounts/{account_id}/payments/{payment_id}/reverse` | Explicit audited reversal; not deletion or silent replacement |
| POST `/v2/funding-accounts/{account_id}/balance` | Owner card checkpoint/correction; not COGS recommendation or payment recording |

Additional existing routes in `app/routers/v2_order_payments.py`:

| Actual path | Current support |
| --- | --- |
| GET `/v2/order-payments`; POST `/v2/order-payments/orders/{order_id}/financial-assignment` | All POs and an audited PO-specific financial assignment; assignment alone records no actual charge/payment |
| GET `/v2/payment-methods`; GET/POST `/v2/vendors/{vendor_id}/payment-settings` | Existing methods and vendor-default configuration |
| GET `/v2/consignment`; GET `/v2/consignment/{vendor_id}` | Legacy consignment overview/vendor detail |
| POST `/v2/consignment/{vendor_id}/reports`; GET `/v2/consignment/{vendor_id}/reports/{report_id}` | Vendor COGS report generation/detail through existing financial infrastructure |
| POST `/v2/consignment/{vendor_id}/cash-settlements` | Exceptional explicit vendor cash settlement with existing confirmation and reason; not a general four-account Monday save |

Feature/capability checks, CSRF and existing default-off consignment/card action gates apply. Actual-card writes also use the configured-owner authority boundary when Phase 1 is enabled. These are registered code paths, not a claim that production has them enabled. Their action flags were not enabled in production or in the guarded browser preview.

## Reusable backend components

| Existing component | Reuse boundary |
| --- | --- |
| `v2_funding_reports_service.funding_report_source_readiness` / `assert_funding_report_source_ready` | Closed-period sales coverage and America/Los_Angeles date boundaries; reject incomplete evidence |
| `calculate_report`, `calculate_combined_report`, `consignment_product_report.populate` | Existing period COGS reports and per-account vendor aggregation. They are not a four-account recommendation generator; reconcile their legacy scopes with the approved pooled-card requirement before reuse |
| `report_position`, `account_summary`, existing report/payment allocation models | Outstanding report amounts, prior settlements and account activity; recommendation and issuer balance must remain distinct |
| `record_payment`, `record_compact_payment` | Explicit edited amounts, report allocations, existing audits and reversals. Functions use the caller's SQLAlchemy session; each current HTTP payment action commits its own transaction |
| `card_balance_service.record_actual_activity`, `existing_actual_request`, `actual_card_balance` | Sole actual-card authority, per-account locking/idempotent activity and checkpoints; do not add another balance writer |
| `OrderPayment`, financing assignment/correction services, existing audit records | Purchase-specific financial attribution independent of supplier; neither recommended payment nor actual payment |
| Native V2 tables/forms and current feature/permission/CSRF infrastructure | Existing visual language and access controls; a future owner-only batch must preserve the established boundaries |

## Missing consolidated functionality

1. A single screen showing Big, Streamline and both configured cards, with one common previous Sunday–Saturday period and following-Monday context. Existing report forms accept dates but do not implement that consolidated weekly workflow.
2. An approved four-account recommendation calculation/aggregation that populates each recommended amount and total, validates account mappings and avoids duplicate pooled credit-card COGS. Existing vendor-report totals cannot simply be relabeled as that result. No FIFO financing attribution should be introduced.
3. Four editable actual payment amounts and a live actual total, distinct from recommendations, with one Save Payments action.
4. One server operation validating and saving all four amounts atomically, with durable batch identity, matching-payload retry detection, correct account/report allocations and one reproducible result. Calling the current four HTTP actions sequentially would allow partial saves.
5. Complete batch idempotency for **both consignment accounts and cards**. Existing actual-card request keys are useful but do not provide a cross-account batch identity; `FundingPayment` has no general batch idempotency key. Consignment `record_payment` does not deduplicate using the optional key. `record_compact_payment` also checks current outstanding amounts before delegating, so retry UX cannot be assumed to work merely by forwarding a card key.
6. Clear pending/saved/error/retry behavior, stale-data handling, four-account audit linkage, and database/browser tests for double clicks, lost responses, rollback after one row fails and concurrent saves.

No Monday route, UI, batch writer, recommendation engine, schema or new accounting workflow was built in this correction task. These gaps are a separate implementation task requiring owner authorization.

## Verified during the accounting correction

The 16-case PostgreSQL accounting matrix passes, including supplier-independent existing lot assignments, new purchase financing, bank overrides, audit preservation and report account membership. The affected purchasing/receiving regression passes **107 tests**. The original **180-unit / ten-command** inventory reconciliation remains exact.

One backend test uses four **finalized synthetic reports** for Sunday October 11 through Saturday October 17 and records edited amounts on Monday October 19: Big $10, Streamline $20, Card 1 $25, Card 2 $35. Correct accounts, remaining amounts and actual card balances are verified in PostgreSQL. Each uses the existing per-account service. This is payment-routing evidence, not a four-account atomic Save Payments test or proof of Monday recommendation calculations.

See [Accounting correction and scheduling investigation](purchasing-v3-accounting-correction-review.md) for corrected behavior, distinct regression counts, historical preservation and deployment limitations. No deployment or live Square mutation occurred.
