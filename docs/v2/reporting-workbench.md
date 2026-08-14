# Unified Reporting Workbench V2

Status: implemented, not deployed (2026-08-12).

## Architecture

`/v2/reports` is one shared workbench whose controls and result columns are selected by a report definition. Its engines remain separate:

- Sales Analysis reads immutable `consignment_sale_facts` line facts. Those facts preserve Square product/variation identity, SKU, store, gross sales, discounts, net sales, and synchronized timestamps. COGS is used only when the fact has an authoritative `extended_cogs_snapshot`; affected aggregates become unknown if any included line lacks cost.
- Stock Value reuses `fetch_current_inventory`, which combines the Square catalog, live Square on-hand counts, active internal stores, and the existing vendor-cost precedence. It is current-only. Current retail price comes only from the Square catalog variation `price_money`; missing cost or retail price remains unknown rather than zero. The executive summary and Inventory by Vendor section aggregate the complete filtered result set, while Known Potential Gross Profit includes only positions where both authoritative retail and cost are known.
- Replenishment / Replacement PO reads local sales facts plus Ordering's catalog identity, vendor SKU configuration, lifecycle, and freshness-aware current-inventory tables. Selected-period replacement and trailing-28-day weeks-of-supply calculations remain distinct. Preview is computed only; finalization creates normal `PurchaseOrder`, `PurchaseOrderLine`, and store-allocation records, then opens the existing draft-order screen.

The workbench service owns deterministic term parsing, include/exclude matching, grouping, report result contracts, relative dates, and private Saved View CRUD. The route owns authenticated store validation, CSRF, rendering, and audit events. Report definitions are not combined into a universal query.

## Search semantics

Comma, semicolon, and newline delimiters create trimmed, case-insensitively deduplicated terms. Search is case-insensitive contains matching over product name, variation name, SKU, and Square variation ID. Includes default to Match Any and can use Match All. Exclusions always use Match Any: a product matching any active exclusion term is removed. Excluded product names are shown with the result.

## Saved Views

Saved Views are private to `principal_id` and preserve report type, terms, stores, grouping, sort, metrics, vendor/lifecycle filters, and date definition. Date definitions may be fixed, relative, or “choose when run.” Create, update, and delete require the Reporting Workbench permission, CSRF validation, and write V2 audit events.

## Known source limitations

- The local Square sales fact synchronization must be complete through the selected end date. The report visibly warns when the synchronization state does not prove coverage.
- The sales fact writer imports all Square order lines, but authoritative historical COGS currently exists only where effective-dated attribution/cost produced a snapshot. It is never inferred from a present-day catalog cost.
- Current Stock Value uses the established operational vendor-cost pipeline. The repository has no universal FIFO layer, and existing data-source documentation identifies current cost provenance as a validation item. The workbench does not introduce a competing valuation model.
- Returns are not netted into this first Sales Analysis engine. Units and revenue represent synchronized completed sale lines; a future return-aware engine must use immutable return facts explicitly.
- Historical Stock Value is unsupported because the authoritative inventory source is current on-hand state.
- Replenishment Target Weeks recommendations are withheld unless every selected active store has a fresh trusted inventory observation. Archived and No Future Reorder products are never eligible for generated previews.

## Deployment boundary

Migration `20260812_0019` creates `reporting_saved_views`. Its successor `20260814_0020` adds the nullable unique purchase-order creation idempotency key and permits `replenishment` Saved Views. The chain is `20260805_0017 → 20260810_0018 → 20260812_0019 → 20260814_0020`; it excludes the unrelated unfinished local Funding Account consolidation migration that reused the `0018` ordinal.
