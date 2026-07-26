# Product Lifecycle workspace data contract

Status date: 2026-07-25. Implementation state: Owner-only production canary remains live on commit `0eac95e22ac24543554193d8d7600cce11f7d505` and schema revision `20260725_0008`. The local, undeployed repository at `20260725_0009` adds the approved Ordering-owned current-inventory read model and owner-only refresh control. The owner has accepted partial catalog-identity coverage of `823/824` (`99.88%`). No staff or global exposure is enabled, and deployment of the current-inventory checkpoint is not authorized.

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
| Current inventory | Historical count snapshots and live Square readers were inspected; touchscreen data is excluded | The local model is empty until an explicit owner refresh and is valid only to its recorded coverage/freshness | `ordering_current_inventory`, populated only by the owner-only Ordering count refresh; no lifecycle GET remote reads |
| Store relevance | Existing active stores and their Square location IDs define required company-total scope | Reliable inventory-state-derived relevance policy remains unapproved | Deferred; do not infer relevance from Touchscreen data |
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

Vendor, lifecycle, product-name, SKU, mapping-state, current-inventory review state, deterministic sorting, server pagination, current-page selection, bulk lifecycle actions, and Archived Products restore remain supported. Store-relevance and category filters remain deferred.

## Current Inventory column

The local `20260725_0009` implementation replaces the accepted placeholder with an Ordering-owned read model. An explicit owner-only, CSRF-protected POST bulk-reads current `IN_STOCK` counts from Square, writes a refresh-run record and only explicitly returned store/variation pairs, and preserves omitted or failed last-valid rows. It does not write Square, lifecycle state, V1 records, or Touchscreen data.

Product Lifecycle and Archived Products display a trusted company total only when every active store has Fresh evidence from the latest successful run. Fresh means 0–24 hours inclusive; Stale means over 24 through 72 hours; older, failed, omitted, unresolved, or location-mismatched evidence is operationally Unknown. Stale and critical rows preserve labeled last-known quantities but do not participate in numeric sorting or Positive/Zero filters. A Fresh explicit zero is distinct from missing evidence. Per-store expansion shows quantity, state, retrieval age, Square `calculated_at`, and the blocking reason.

Rendering either lifecycle page continues to make zero Square calls, zero touchscreen reads, and no writes. The workspace uses nine bounded local repository queries regardless of product count. `ORD-DEC-037` is implemented and locally verified; migration, deployment, and the first production inventory refresh remain unauthorized.

## Production owner-canary verification

Authenticated production verification was completed with owner principal `6` at 1440 × 1000, 1100 × 900, and 390 × 844. The [live UX evidence index](./evidence/product-lifecycle-catalog/README.md) records the desktop, laptop, mobile, selection, confirmation-dialog, Archived Products, and incomplete-identity captures.

The responsive workspace had no document-level horizontal overflow at any tested width. Search and lifecycle filters remained usable; the bulk toolbar stayed visible above the results; buttons, selects, checkboxes, the optional note field, labels, and focus indicators remained usable; and the responsive rows retained legible product names, SKUs, vendor, lifecycle status, and selection state. No lifecycle mutation was submitted while capturing evidence.

The owner explicitly accepted the following production catalog-identity state:

- expected mapped identities: `824`
- covered/named identities: `823`
- unresolved identities: `1`
- coverage: `99.88%`
- unresolved variation: SKU `Y956832`, Square variation ID `ELA77RJ6VMTS56DD2OOHLIZ7`, vendor `Vapetasia`

The unresolved variation is absent from the current Square catalog response. The application does not infer that it is deleted, discontinued, archived, or No Future Reorder. It remains visible in Product Lifecycle as `Product name unavailable`, is correctly found by SKU search, is not falsely found as a product-name match, and remains available for owner-controlled lifecycle management. The visible refresh state remains partial.

Production route and isolation checks confirmed:

- owner principal `6`: Product Lifecycle `GET` returned HTTP `200`
- owner principal `6`: Archived Products `GET` returned HTTP `200`
- non-exposed store principal `9`: Product Lifecycle `GET` returned HTTP `404`
- V1 Ordering remained available with HTTP `200`
- Product Lifecycle `GET` completed with zero Square calls, zero touchscreen-table reads, and zero database writes
- the request used bounded local reads (11 total SQL statements including authentication and capability checks)
- feature and capability exposure remained unchanged
- production schema remained `20260725_0008`
- deployed commit remained `0eac95e22ac24543554193d8d7600cce11f7d505`

## Rollback

After a future `20260725_0009` deployment, application rollback may return to the deployed `20260725_0008` code; that code safely ignores the additive current-inventory tables. Operational rollback must retain populated catalog, lifecycle, refresh-run, and last-valid inventory evidence. The `0009` downgrade exists for disposable migration verification only and must not be used to drop populated production evidence during an application rollback.
