# V1 reports and exports inventory

## Report entry points

The hub is `GET /management/reports` (`management_reports.html`). It requires `management.access`, but almost all links are rendered only when the principal’s literal role is `ADMIN`. Routes themselves use a mixture of `management.access`, `management.admin`, and literal `ADMIN`.

All Square-backed reports are recalculated on request; none has a cache or snapshot table. “Active” means linked from the hub/dashboard or another active screen, not proven production usage.

| User-facing report/export | Route(s) | Purpose, filters, scope | Data/calculations | Format/permission | Usage, V2 category, parity concerns |
|---|---|---|---|---|---|
| Reports & Exports | `/management/reports` | Report launcher | Static template | HTML; `management.access` | Active dashboard card; V2 Reports hub. Visibility differs from backend permissions |
| COGS Report | `/management/reports/cogs` | Date range; all active Square-enabled stores | Square completed orders; catalog variation cost and local current vendor SKU cost. Totals cost of units sold | HTML; `management.access` | Active link/card; V2 Reports/Finance. Historical cost is current/fallback rather than transaction-time |
| Stock Value On Hand | HTML and `/export.csv`; optional local `store_id` | Current units, cost value, retail value, store totals/top items | Live Square catalog/current inventory; first vendor cost and current price; missing-cost/price counts | HTML+CSV; `management.admin` | Active card/hub; Inventory/Reports. Current-state only and negative quantities/cost ambiguity need validation |
| Inventory Velocity | HTML and `/export.csv`; `days` 15/30/45/60/90, end date, store/category/vendor/health/search filters, target days | Rank sales velocity, margin, supply, health, reorder, trend and transfer opportunities | Square orders, current inventory, inventory changes; local stores/vendors/mappings; stockout/lost-sales adjustment | HTML+CSV; literal `ADMIN` | Active hub; Inventory Analytics. Complex calculations have tests but depend on complete Square history/current mapping |
| Stock Coverage Purchase | HTML and `/export.csv`; days, end date, target months, top N, store/vendor/category/search | Ranked purchasing need and vendor summaries | Reuses velocity/stockout data; target days=`months*30`; per-store need; current inventory/cost | HTML+CSV; literal `ADMIN` | Active hub; Ordering Planning. “Create order” POST makes this report transactional |
| Create order from stock coverage | `/management/reports/stock-coverage-purchase/create-order` | Selected report rows/vendor/store splits | Creates editable `purchase_orders`, lines, allocations from transient report values | Redirect; literal `ADMIN` + CSRF | Active business action; V2 Ordering. Must validate report-to-order snapshot boundary |
| Targeted SKU Demand | HTML and `/export.csv`; selected variation IDs, lookback 15–180, target days, optional store/end date/search | Demand/current inventory/purchase need/cost/supply for selected SKUs | Live catalog, orders, inventory changes/counts; local vendor cost; stockout correction | HTML+CSV; literal `ADMIN` | Active hub; Ordering/Inventory. Search queries entire catalog; current mappings can change historical results |
| Count Push Trend / Square Recount Push | Same `/management/reports/count-square-sync`, optional `sync_scope=recount`, store/date/session | Review Square inventory push events and outcomes | `square_sync_events` joined to count/store context; sync type/status filters | HTML; literal `ADMIN` | Two duplicate hub entry points to one route/template. V2 Audits/Integration Health |
| Three Recount Matches | `/management/reports/recount-changes`; store/date | Show recount closeouts and variance details | Submitted count sessions, snapshots, entries; filters `recount_closed_out` and section | HTML; `management.admin` | Active hub label differs from function name. V2 Inventory/Audits; preserve exact three-match semantics |
| Session Variance Export | `/management/sessions/{id}/export.csv` | Single count session | Snapshot expected, counted, variance, recount metadata | CSV; `management.access`; also writes audit log | Active from session detail. Export GET has a DB write/commit; V2 Inventory/Audits |
| Full Store Count Export | `/management/store-count/{id}/excel` POST | One admin count, employee/store context | Admin count expected/counted/variance lines | XLSX; `management.admin` + CSRF | Active. POST export is unusual; V2 Inventory. Read/export route should preserve audit expectations |
| Purchase Order PDF | `/management/ordering-tool/orders/{id}/pdf` | One PO with vendor/items/allocations and disclaimer | Local PO snapshot plus selected PDF template | PDF file; `management.admin` | Active. Stored filesystem artifact may regenerate. V2 Ordering/Documents |
| Sales Transactions | HTML and `/export.csv`; Square location(s), start/end | Order-level datetime, items, discounts, tips, tax, paid, subtotal | Square locations and completed orders | HTML+CSV; literal `ADMIN` | Active hub; Reports/Sales. Store-local timezone and transaction inclusion must match |
| Gross Sales by Store | HTML and `/export.csv`; location(s), start/end | Monthly gross/order counts per store and totals | Square completed orders | HTML+CSV; literal `ADMIN` | Active hub; Reports/Sales. Validate gross definition and timezone boundaries |
| Sales by Vendor | HTML and `/export.csv`; vendor required, location(s), start/end | SKU/variation units/orders/gross/discount/net for mapped vendor | Square orders filtered through current `vendor_sku_configs` | HTML+CSV; literal `ADMIN` | Active hub; Reports/Vendor. Current mappings can reclassify historical sales |
| Employee Sales | HTML and `/export.csv`; location(s), start/end | Employee transaction/gross/net/discount/tips/tax/paid/averages | Square orders/payments/team members; employee attributed to largest completed payment | HTML+CSV; literal `ADMIN` | Active hub; Reports/People. Attribution heuristic and current employee identity require parity tests |
| Master Safe Change Usage | `/management/reports/master-safe-change-usage`; date/store | Denomination quantities/amounts taken from master safe through change forms | Local `change_form_submissions`/lines, store filters | HTML; `management.admin` | Active hub; Cash Operations/Audits. Derived from form line section codes |
| Cash verification batch detail | `/management/cash-reconciliation/verification-batches/{id}` | Stored verification batch and daily facts | Cash batch/verification/actual tables; expected snapshot when available | HTML; `management.admin` | Active from cash UI; V2 Cash Operations. Not on reports hub but report-like history |
| Count group coverage audit | `/management/groups/audit-count-groups` | Live coverage of catalog variations by groups/campaigns | Square catalog/categories + local group mapping | HTML; `management.groups` | Active group UI; Admin/Audits. Live non-repeatable result |

## Export formats and headers

- CSV exports stream UTF-8 text created with Python `csv.writer`; there is no common export service.
- XLSX export is generated in memory with OpenPyXL.
- PO PDF is generated by ReportLab and retained under `generated/purchase_orders`.
- Export filenames include report/date/filter context, except session/PO use record IDs.
- No export size limit, async generation, object storage, encryption, or retention policy exists.

## Duplicate and inconsistent entry points

- Count Push Trend and Square Recount Push are two forms targeting the same count-sync route with a hidden scope.
- Inventory velocity, stock coverage, and targeted demand repeat catalog, sales, stockout, inventory, vendor, and export concepts through related services.
- Stock coverage and ordering generation overlap in recommendation/order creation but use distinct entry points.
- Management session detail and count-sync/recount reports expose overlapping Square push history.
- COGS is available to all management; the other sales reports are literal ADMIN. Stock value uses capability admin. The hub’s role check does not express these distinctions.

## Validation priorities for V2 parity

1. Freeze representative Square responses and compare date/timezone boundaries.
2. Preserve current mapping/cost semantics explicitly or add an approved historical snapshot design.
3. Golden-test every CSV/XLSX/PDF header, numeric rounding, filter, and empty-state behavior.
4. Separate report computation from order creation while preserving the exact selected-row snapshot.
5. Resolve permission inconsistencies only through an explicit authorization decision gate.
