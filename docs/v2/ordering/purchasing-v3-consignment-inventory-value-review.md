# Purchasing V3 Consignment Inventory Value Local Review

Big Wholesale and Streamline now display **Consignment Inventory Value**, calculated from current Square on-hand inventory and authoritative local unit COGS. Weekly recommended COGS payments, recorded actual payments, payment history and actual credit-card balances remain separate. No deployment, production data change, new accounting ledger, schema migration or Square mutation was performed.

Local preview: http://127.0.0.1:8763/management/purchasing/payments?monday=2026-10-19. The preview uses synthetic records, mocked Square responses and the previously established simulated Monday. External network access remains blocked.

## Exact replacement

The previous consignment display summed the remaining obligations of individual non-draft, non-voided funding reports:

```
SUM(MAX(0, MAX(0, report COGS + report charges − report credits)
           − unreversed payments allocated to that report))
```

That report query has been removed from the Monday recommendation/display path for consignment. Its replacement is:

```
Consignment Inventory Value(account) =
    ROUND_TO_CENTS(SUM over uniquely attributed Square variations (
        SUM(current Square IN_STOCK quantity across the four stores)
        × authoritative SKU unit COGS
    ))
```

This is an independent read-only valuation in `purchasing_payments_service.consignment_inventory_values`. The existing workspace GET response carries it separately as `inventory_values`. It does not enter the weekly recommendation token, change the weekly COGS calculation or run during Save Payments. Existing saved recommendation/history snapshots remain untouched.

Payments, payment allocations, report balances, reversals, vendor credits, invoices and financial adjustments are not valuation inputs. Pending purchase orders are not stock. Customer returns change the valuation only through changed Square on-hand quantities. Explicit Square zero counts are valued as zero; omitted counts remain unknown. Totals use Decimal arithmetic and round after summing full-precision SKU values.

## Sources and attribution

| Input | Existing authority used |
|---|---|
| Consignment account identity | Unique active `FundingAccount` with type `CONSIGNMENT`, matched through `Vendor.square_vendor_id` to the configured Big/Streamline canonical IDs. |
| Saved inventory financing | `OrderPayment.vendor_id`, `financial_treatment` and `payment_category_snapshot` on qualifying received PO lines. Purchasing `PurchaseOrder.vendor_id` and default supplier preferences do not override saved financing. |
| Default SKU association without received financing evidence | Live Square catalog variation vendor IDs, using the established Big/Streamline canonical identities and existing explicit local cash exception. Local supplier options alone do not establish financing. |
| Current quantities | Existing `SquareOrderingReadGateway.fetch_current_inventory_counts`, the policy-controlled read endpoint `/v2/inventory/counts/batch-retrieve`, explicit `IN_STOCK` counts by variation and location. Existing store-location resolution supplies Andresen, Hwy 99, SR503 and Longview. |
| Current unit COGS with saved received financing | Latest qualifying received PO-line cost for that financing assignment, following the existing funding-account current-cost convention. Missing latest cost is incomplete; no older or supplier cost is substituted. |
| Configured unit COGS without a received PO assignment | Existing configured-cost resolver `_cost`, restricted to that financing vendor's active `VendorSkuConfig` rows and explicit variation IDs. Historical supplier fallback is disabled. Changing the default purchasing supplier cannot hide or reassign the consignment vendor's configured cost. Conflicting costs are incomplete. |

PO/receiving records supply financing identity and saved cost only. Their cumulative quantities are never used to reconstruct current on-hand. Multiple same-account lots contribute one current Square quantity per variation, not one quantity per PO. Uniform saved card/bank financing is excluded from consignment even if the current catalog vendor is Big or Streamline.

## Mixed financing and incomplete data

Square's current count is aggregated by variation/location. The inspected data does not provide a reliable current unit split for a SKU with conflicting Big, Streamline or card/bank received financing. The valuation therefore identifies that SKU and marks the affected consignment total **Incomplete**. It does not assign all units to a vendor, invent proportions, select the newest financing lot or use FIFO.

The expandable valuation details identify product/SKU, Square variation ID, per-store quantities, unit COGS, source mapping/PO-line references and repair guidance. Missing costs, conflicting mappings, unknown identities, missing store mappings or omitted Square counts also produce explicit incomplete warnings. A known subtotal is labeled as a partial amount, never as the account total. The detail table is populated only when expanded.

No production attribution/data-quality inspection was performed. Reliable totals for actual mixed-financing SKUs still require authoritative current attribution evidence or correction of genuinely erroneous saved assignments through existing authorized workflows; this change introduces no new financial writer or lot-allocation workflow.

## Independent multi-store verification

These synthetic Square quantities were checked against local authoritative SKU costs in a PostgreSQL **read-only transaction**, independently of the service's SKU-value aggregation:

| Account / product | Andresen | Hwy 99 | SR503 | Longview | Total on hand | Unit COGS | Inventory value |
|---|---:|---:|---:|---:|---:|---:|---:|
| Big / Product A | 25 | 25 | 25 | 25 | 100 | $5.00 | $500.00 |
| Big / Product B | 20 | 20 | 20 | 15 | 75 | $8.00 | $600.00 |
| Big / Product C | 10 | 10 | 15 | 15 | 50 | $10.00 | $500.00 |
| **Big total** | | | | | | | **$1,600.00** |
| Streamline / Product D | 10 | 10 | 10 | 10 | 40 | $4.00 | $160.00 |
| Streamline / Product E | 5 | 10 | 5 | 10 | 30 | $7.00 | $210.00 |
| **Streamline total** | | | | | | | **$370.00** |

The actual valuation service returned the same totals and complete states. See `output/purchasing-consignment-inventory-value/independent-valuation.json` for inputs, independently calculated values, service results and SKU-level source references.

The existing demo's weekly recommendations remain Big $4,500 / Streamline $2,200 / Card 1 $3,100 / Card 2 $2,800. Recorded actual amounts remain $4,350 / $0 / $3,000 / $2,800. Card balances remain $28,000 / $25,200. Those figures do not determine the $1,600 / $370 inventory values.

## Verification results

- **531 relevant regression tests passed**: all purchasing tests, including PO/PDF, receiving/distribution, shipments, financial assignment and Monday payments; funding reports, consignment facts/configured products, existing payment workflows, Square read gateway and V2 shell/navigation. Log: `regression-final.log`. No tests were skipped in this selected run.
- **43 final focused PostgreSQL/HTTP tests passed**, including 18 new inventory-valuation tests and the 25 existing Monday tests. Log: `final-focused.log`.
- New tests cover multiple SKUs, four-store quantities, different costs, explicit zero, missing counts/costs, saved PO cost precedence, default supplier changes, supplier substitution, mixed financing, same-account lot deduplication, pending PO exclusion, Square quantity increases, vendor-credit/payment/reversal independence, ambiguous identities, conflicting costs, rounding and recommendation-token independence.
- Browser checks confirmed $1,600/$370, per-SKU/per-store source details, unchanged recorded payments/cards/recommendations, and a two-unit $5 Square increase changing only Big's inventory value to $1,610. Returning the mock counts restored $1,600.
- Removing Longview's Product B count produced **Incomplete**, an exact SKU/location warning and a clearly partial $1,000 known subtotal. It did not present zero or $1,000 as the total. Streamline remained $370.
- Valuation refresh/reload preserves saved actuals and does not create a payment draft for a previously recorded week. Explicit correction cancellation was verified without submitting payments.
- **All 17 before/after historical table fingerprints match**, including funding accounts/reports/lines, payments/allocations/adjustments, actual activity/checkpoints, ledger entries, weekly batches/entries, sales facts, POs/lines/receipts/allocations and audit log. Evidence: `history-preservation.json`.
- JavaScript syntax and `git diff --check` passed. No full unrelated application suite was rerun for this display correction; the separate previously documented scheduling issue is outside this scope.

## Changed files and screenshots

Implementation changes are confined to:

- `app/services/purchasing_payments_service.py`: remove report-based consignment balance aggregation; add the independent read-only inventory valuation to workspace reads.
- `app/templates/purchasing/payments.html`: explicit Consignment Inventory Value / Actual Card Balance labels and compact expandable valuation details.
- `app/static/v2/monday-payments.js`: render/refresh current valuations independently of frozen recommendations and preserve payment draft state.
- `tests/test_purchasing_consignment_inventory_value.py`: new valuation tests.
- `tests/test_purchasing_monday_payments.py`: isolate Square inventory reads in the existing Monday fixtures.

No changes were made to recommendation engines, PO/PDF/receiving implementations, Square synchronization, actual payment writers, card-balance authority, models, migrations or production configuration. The local-only preview harness adds synthetic current-stock responses and SKU cost fixtures; it retains its external-network and financial-writer restrictions.

Screenshots in `output/purchasing-consignment-inventory-value/`:

- `monday-inventory-values.jpg`: corrected Monday Payments screen.
- `sku-valuation-breakdown.jpg`: representative multi-store SKU valuation.
- `incomplete-value-warning.jpg` and `missing-sku-store-detail.jpg`: controlled missing-count scenario.

The preview is restored to complete synthetic values. Owner review is the next action. Deployment remains unauthorized.
