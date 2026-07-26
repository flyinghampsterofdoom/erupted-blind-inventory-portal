# Product Lifecycle workspace data contract

Status date: 2026-07-25. Implementation state: Ordering-owned catalog identity correction implemented and PostgreSQL-verified locally at schema head `20260725_0008`; not deployed or exposed beyond the existing owner canary.

## Ownership boundary

Product Lifecycle and Archived Products are Ordering surfaces. Customer Touchscreen, Flavor Finder, `touchscreen_square_variation_cache`, `touchscreen_store_inventory_cache`, and every touchscreen synchronization process are outside the Ordering data contract. Ordering routes neither read nor populate those tables, never trigger a touchscreen refresh, and do not use touchscreen facts as fallbacks.

The workspace GET routes are database-only. They do not call Square, execute recommendation calculations, or write lifecycle/catalog state. The explicit owner-only catalog refresh is a separate CSRF-protected POST and performs only bulk paginated Square catalog reads.

## Root source analysis

| Required field | Existing sources inspected | Production completeness | Selected authority |
|---|---|---|---|
| Square variation ID | `vendor_sku_configs.square_variation_id`; lifecycle override identity | 824 active default mapped variations observed in the owner canary | V1-owned vendor mapping; retained lifecycle identity for unmapped archived records |
| SKU | `vendor_sku_configs.sku`; historical lifecycle snapshot; Square catalog | Mapping source is populated for mapped records | Mapping first, Ordering catalog identity second, historical lifecycle snapshot only for an unmapped recovery row |
| Product name | Vendor mapping has no name; purchase-order/count snapshots are historical and incomplete; touchscreen catalog cache had 0 production rows | No pre-existing complete reliable local source | New `ordering_catalog_identity` read model populated from Square catalog |
| Vendor | `vendor_sku_configs.vendor_id` joined to `vendors` | Complete for active default mappings | Existing V1 Ordering mapping relationship |
| Lifecycle | `ordering_product_lifecycle`; absent row | Sparse overrides by design | Persisted override; absence resolves to `ACTIVE` |
| Actor/timestamp | lifecycle audit events and lifecycle row timestamps | Complete for explicit transitions; absent for sparse Active | Latest lifecycle audit with row timestamp fallback |
| Inventory/store relevance | Live Square and touchscreen caches were inspected | No complete Ordering-owned local read source | Deferred; filters are omitted |
| Category | Square catalog can supply it, but no completeness contract is approved | Unverified | Deferred; filter is omitted |

The production canary measured 824 mapped variations, two lifecycle overrides, zero touchscreen catalog-cache rows, and zero touchscreen inventory-cache rows. That evidence ruled out touchscreen data and historical snapshots as complete workspace sources.

## Ordering catalog identity model

Revision `20260725_0008` adds two V2-owned tables without backfill or V1 changes:

- `ordering_catalog_identity`, keyed by Square variation ID, stores Square item ID, SKU, item/variation/display names, deletion evidence, Square update time, and last-seen/source timestamps.
- `ordering_catalog_refresh_state`, a singleton, records the last outcome, expected/covered/missing mapped counts, attempt time, last complete refresh, error summary, and owner principal.

The model contains catalog identity/display metadata only. It contains no inventory, sales, recommendation, customer, Flavor Finder, or touchscreen state.

## Refresh and failure contract

`POST /v2/ordering/products/catalog/refresh` requires the existing feature gate, effective `management.admin`, explicit `ordering.lifecycle.manage`, and CSRF validation. It uses the existing Ordering read gateway and `/v2/catalog/search-catalog-items` pagination. It does not make one request per product and cannot call a Square write endpoint.

- A complete result returns every currently mapped variation and advances `last_successful_at`.
- A partial result upserts valid returned metadata, records `PARTIAL`, and does not claim a complete refresh.
- A failed result records `FAILED` while preserving every prior identity row and the last complete timestamp.
- Empty fields never replace a known non-empty name or SKU.
- Refresh never changes lifecycle state.
- One V2 audit event records the outcome and counts without catalog payloads or credentials.

No scheduled worker and no passive GET mutation are introduced in this milestone.

## Coverage and search behavior

Coverage is evaluated against the complete current active/default mapped variation population. The workspace exposes expected, covered, and missing counts plus the latest refresh outcome and timestamps.

Rows without identity metadata remain present and display `Product name unavailable`; SKU, variation ID, vendor/mapping state, and lifecycle controls remain available. Product-name search matches only known names and preserves the unfiltered population count in the page context. SKU search works independently. An explicit Product-name state filter selects known or unknown names. SKU/variation IDs are never presented as product names.

Vendor, lifecycle, product-name, SKU, mapping-state, deterministic sorting, server pagination, current-page selection, bulk lifecycle actions, and Archived Products restore remain supported. Inventory-state, store-relevance, and category filters are explicitly deferred until Ordering owns complete reliable local sources.

## Rollback

Application rollback may return to code at `20260725_0007`; that code safely ignores the additive tables. Operational rollback must retain populated identity/refresh tables. The migration downgrade exists for disposable migration verification only and must not be used to drop populated production metadata during an application rollback.
