# Purchasing V3 payment configuration — local verification

Verified locally on 2026-10-18. No deployment, production data/configuration changes, new migrations, or live Square operations.

## Result

Payment Methods remains `/v2/payment-methods`, using the existing V2 shell. Admins can select distinct Monday card roles, rename funding accounts and their linked masked card methods, add a linked credit-card method/account, and maintain searchable vendor purchasing defaults. Changes use the existing audit log, CARD decisions, FundingAccount, PaymentMethod and VendorPaymentSetting records. No new table, balance authority, ledger, service module, or vendor-level Monday calculation was added.

Configuration POST `/v2/payment-methods/configuration` requires Admin permission and CSRF. A shared transaction lock serializes card-role decisions with Monday saves; configuration tokens reject stale edits. Audited request keys make acknowledged and lost-acknowledgement retries idempotent. Existing Manager access to legacy controls remains available; the new configuration actions require Admin. Balance corrections remain owner-only on the existing funding-account page.

## Exact Monday formula

Let P be weekly gross pooled credit-card sales COGS, B1/B2 the configured funding accounts' actual issuer balances, W1=max(B1,0), W2=max(B2,0).

- Card 1 = round_half_up_to_cent(P × W1 / (W1+W2)).
- Card 2 = P − Card 1.
- If either actual balance is unavailable, or both positive weights are zero, neither recommendation is invented. Owner input is requested. A missing funding-account role prevents recording payments.
- Big Wholesale and Streamline continue to use their separate weekly sales COGS. Their independent current-stock valuations are unchanged.
- Vendor defaults are never consulted for Monday split weights. Actual payment amounts remain independently editable and one atomic/idempotent Save Payments action is retained.

Example: P=$5,900, actual balances $31,000 / $28,000. Weights are 52.54237288% / 47.45762712%, giving recommendations $3,100 / $2,800. Moving the demo vendor to the other card leaves those recommendations unchanged. Selecting another synthetic card with a $10,000 owner-entered checkpoint instead gives $4,460.98 / $1,439.02, summing exactly to $5,900. A one-cent pool with equal balances gives $0.01 / $0.00.

## Prospective defaults and preservation

Vendor changes update only VendorPaymentSetting. They do not rewrite effective classifications, saved OrderPayment assignments, received lots, current inventory ownership, original POs, payments, report calculations or COGS history. A new purchasing line snapshots its default intended account; explicit SKU financing decisions and existing purchase-specific choices take precedence. Existing drafts keep their decisions across refresh. New purchases may use the new default without inheriting an old received lot's financing. Deliberate legacy PO initialization can use the default only for an order created after that setting; existing financial assignments always return unchanged.

Saved weekly recommendation snapshots retain account IDs, names, balances and calculations. Historical display uses frozen account names and identifies card balances as balances at recommendation. A changed account role invalidates an older server save token. A browser draft also retains its account identities: changing the recipient account blocks Save Payments until the owner reviews all actual amounts. No implicit application of a prior card's draft to another card.

## Verification

`output/purchasing-payment-configuration/regression-final.log`: **473 passed, no failures, no skips**, 91.24 seconds. Four existing deprecation/SQLAlchemy schema-sort warnings. This is the relevant purchasing/financial regression set, not a claim that the entire repository suite passed.

Includes 16 new configuration cases: renaming, switching roles and corresponding balance records, frozen payment history, vendor moves independent of Monday recommendations, future purchasing defaults, explicit SKU precedence, existing draft persistence, received-lot preservation, consignment valuation preservation, account creation/retry safety, invalid full-length digit rejection, unavailable balance handling, positive/zero/negative balance weights, exact penny allocation, concurrent stale edits, Admin/CSRF checks, stale Monday tokens, duplicate/missing roles. Existing tests cover payment atomicity/retries/corrections/checkpoints, purchasing/PO/PDF, receiving/partial shipments/concurrency, consignment, funding reports and financial assignment isolation.

An intermediate regression caught old received financing being carried into a new purchase. That implementation was removed, the approved new-purchase/old-lot separation restored, and the full relevant set rerun successfully. Earlier failing logs are retained as investigation evidence; `regression-final.log` is the final result.

Browser checks on isolated `127.0.0.1:8764`: renamed the demo card, created Demo New Card with optional last-four only, assigned it, saw unavailable recommendations before its balance existed, entered a synthetic checkpoint using the existing Adjust Balance form, confirmed the exact new proportional recommendations, moved a vendor without changing the Monday split, retained an independent actual-payment override after reload, verified changed-role draft blocking/review persistence, and verified an unconfigured Card 2 Needs Attention state with Save Payments disabled. No weekly payments were recorded in this preview verification. Restored the fully configured demo to Card 1=103, Card 2=104; these are synthetic IDs, not production mappings.

## Artifacts and production boundary

Local Payment Methods: http://127.0.0.1:8764/v2/payment-methods

Local Monday Payments: http://127.0.0.1:8764/management/purchasing/payments

Screenshots in `output/purchasing-payment-configuration/`: payment-methods.jpg, monday-balance-proportions.jpg, changed-account-draft-review.jpg, missing-card-needs-attention.jpg.

The isolated preview blocks external networking, uses mocked Square responses, and has no production credentials. Its existing 33 deliberately unresolved attribution fixtures still display incomplete consignment valuations; configuration does not guess or resolve those independent exceptions. The synthetic account created and balance checkpoint entered during browser checks remain available for local review.

For a later authorized production cutover, the owner-confirmed initial Card 1 is existing Chase Visa funding account **2**. Select that existing record through Payment Methods or supply the existing initial role configuration; no source-code identity constant was added. Card 2 still requires the owner's actual card identity and issuer balance. No production mapping, feature flag or record was changed. Existing release migrations and deployment prerequisites remain as documented in the prior integration review. Stop for owner review; deployment is not authorized.
