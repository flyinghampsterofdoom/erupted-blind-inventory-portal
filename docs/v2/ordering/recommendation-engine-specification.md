# Deterministic recommendation-engine specification

Status: proposed first-version specification. Owner decisions in the decision register remain prerequisites.

## Principle

The engine produces inspectable recommendations, never purchase orders or Square writes. A human selects, ignores, excludes, or overrides each output. Every run has an algorithm version, as-of time, source timestamps, store scope, and immutable input digest.

## Inputs per SKU and store

`sellable_on_hand`, qualified `pending_inbound`, reserved `pending_outbound`, non-sellable quantity, daily sales history, stockout intervals, order-cycle days, vendor lead-time days, safety-stock days/units, manual par/target, MOQ, pack quantity, unit cost, product status, exclusions, preferred/eligible vendors, mapping completeness, and data freshness.

All quantities have explicit units. Missing values remain missing; they are not silently coerced to zero.

## Proposed deterministic calculation

After approved stockout treatment, calculate:

```text
adjusted_daily_velocity = adjusted_units_sold / eligible_observation_days
coverage_days = order_cycle_days + lead_time_days
target_units_raw = max(manual_target_if_applicable,
                       adjusted_daily_velocity * coverage_days + safety_stock_units)
projected_available = sellable_on_hand + qualified_pending_inbound
                      - reserved_pending_outbound
suggested_before_rounding = max(0, target_units_raw - projected_available)
after_moq = 0 if suggested_before_rounding = 0
            else max(suggested_before_rounding, vendor_moq)
suggested_final = ceil(after_moq / pack_quantity) * pack_quantity
projected_stockout_date = as_of_date +
                          (projected_available / adjusted_daily_velocity)
```

An approved maximum-inventory guard may cap or block the output, but must never round below MOQ/pack silently. Conflicts produce a warning requiring a human decision.

## Explanation payload

Every output contains:

- why it appeared, including threshold crossed;
- source quantities, velocity window, eligible/stockout days, lead time, order cycle, and freshness;
- projected stockout date or “not calculable” reason;
- raw target, projected availability, pre-round suggestion, MOQ adjustment, pack adjustment, and final suggestion;
- chosen/preferred vendor and alternatives considered;
- confidence band plus contributing penalties, never a false precision-only score;
- blocking errors and non-blocking warnings;
- active merchandising decisions and the algorithm/input versions.

## Edge cases

| Case | Proposed result |
|---|---|
| New product | Use an approved launch baseline/manual target; otherwise show low-confidence review with no fabricated velocity |
| No recent sales | Recommend zero only when fresh inventory/history and product status support it; otherwise warn |
| Currently zero | Project stockout as now; distinguish true zero from stale/missing inventory |
| Stocked out during window | Exclude or correct approved stockout days; show both observed and adjusted velocity |
| Declining sales | Use deterministic recency comparison and warning; do not autonomously suppress |
| Incomplete mapping | Block PO eligibility while still showing the inventory concern |
| Missing vendor price | Show quantity recommendation but block priced draft/approval as policy determines |
| Missing lead time | Use no hidden default; either owner-approved documented default with warning or block |
| Conflicting inventory | Block final quantity and display source conflict |
| Do not reorder | Suppress actionable quantity, preserve an explainable excluded record, and respect scope/dates |
| Multiple vendors | Apply approved preference/fallback policy; never silently switch |
| Rounding exceeds maximum | Require override or vendor/policy correction; explain both constraints |

## Confidence proposal

Confidence is categorical (`HIGH`, `MEDIUM`, `LOW`, `BLOCKED`) derived from explicit checks: freshness, sufficient eligible days, mapping completeness, inventory consistency, lead-time availability, stockout adjustment magnitude, and product maturity. Thresholds require owner approval and are versioned. Confidence never authorizes automatic ordering.

## Determinism acceptance

Identical normalized inputs and algorithm version must produce byte-equivalent calculation fields and stable explanation codes. Display prose may be localized separately; stored reason codes and numeric evidence remain stable.
