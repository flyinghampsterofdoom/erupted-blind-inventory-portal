# Product Lifecycle workspace

Status: implemented locally for owner UX review; not deployed by this milestone.

The Product Lifecycle and Archived Products pages are owner-only, database-driven catalog-cleanup workspaces. GET filters, sorting, pagination, and page rendering do not call Square and do not execute the Ordering Intelligence calculation pipeline. Lifecycle transitions, sparse `ACTIVE` behavior, optimistic concurrency, atomic 250-row batches, restore-to-pre-archive behavior, and V2 audit semantics remain unchanged.

## Local data sources

| Workspace field | Current source | Missing-data behavior |
|---|---|---|
| Product name | `touchscreen_square_variation_cache.item_name` plus `variation_name`; lifecycle snapshot fallback | Falls back to the lifecycle product-name snapshot, then SKU; never triggers a Square read |
| SKU | Active default `vendor_sku_configs`; lifecycle snapshot fallback | Displays `SKU unavailable` when neither source exists |
| Vendor | Active default vendor mapping joined to `vendors` | Displays `Unknown vendor` for a retained lifecycle override whose mapping no longer exists |
| Lifecycle | `ordering_product_lifecycle`; missing row means `ACTIVE` | Sparse absence remains `ACTIVE` and creates no row during GET |
| Last changed / changed by | Latest V2 lifecycle audit event, with lifecycle timestamp fallback | Displays `Never` / `—` for sparse Active rows without lifecycle history |
| Store relevance | Existing `touchscreen_store_inventory_cache` location evidence | Explicit `Unknown` when no local store evidence exists |
| Inventory state | Sum of existing local touchscreen inventory-cache rows | Explicit `Unknown inventory` when no cache rows exist; zero is not conflated with unknown |
| Mapping state | Active default `vendor_sku_configs` mapping | Explicit `Unmapped` for retained lifecycle overrides without a current mapping; duplicate/ambiguous default mappings are not yet classified separately (TD-029) |
| Category | Not available from a supported local lifecycle/catalog source | Filter omitted; tracked as TD-029 |

The touchscreen cache is reused as an existing local evidence source; this milestone does not change its synchronization behavior or claim that it is a durable Ordering read model. TD-026 remains open for Ordering Intelligence request-time reads, and TD-029 covers lifecycle-workspace catalog/category coverage.

## Interaction contract

- Default view excludes Archived products, sorts product name ascending with Square variation ID as the stable tie-breaker, and shows 50 rows.
- Allowed page sizes are 25, 50, 100, and 250.
- Product, SKU, vendor, lifecycle, store relevance, inventory state, and mapping filters combine with AND semantics.
- Search is case-insensitive, whitespace-normalized substring matching.
- Selection is limited to rendered rows on the current page and clears naturally on GET navigation/filter changes.
- One confirmation dialog covers each submitted batch; no per-row confirmation is introduced.
- Archived Products uses the same search, vendor, store/inventory/mapping filters, sorting, pagination, selection, note, and confirmation system, with restore as its sole action.
- Technical identifiers and row version remain available in an expandable row detail rather than occupying primary table columns.

The repository builds the workspace in a bounded six-query path: lifecycle overrides, mappings plus local catalog names, local inventory/store evidence, lifecycle audit events, audit actors, and active stores. It does not execute product- or variation-level SQL.

The lifecycle-audit query count is fixed, but its returned row volume grows with lifecycle history because the current schema has no latest-event projection. This is acceptable for the narrow owner canary and tracked as TD-030 for monitoring and later read-model work.
