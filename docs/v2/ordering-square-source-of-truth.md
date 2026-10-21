# Ordering Square source-of-truth record

This record applies the [V1 Preservation Guarantee](./v1-preservation-guarantee.md). It does not authorize a new local SKU, catalog, inventory, location, or sales system of record.

## Authority statement

Square owns:

- item and variation identity;
- Square catalog object and variation IDs;
- SKU and UPC/GTIN catalog facts;
- current item/variation names and prices;
- vendor assignments and vendor-provided costs as represented by Square;
- locations and Square location IDs;
- completed orders, line items, quantities, and sales dates;
- live Square inventory counts and changes.

Erupted Admin stores operational references, caches, overrides, and historical snapshots needed to run the Ordering Tool. Those local copies do not become authoritative for Square identity or sales.

## Configuration and authentication

| Setting | Use |
|---|---|
| `SQUARE_ACCESS_TOKEN` | Bearer token on every Ordering Square request |
| `SQUARE_API_BASE_URL` | Defaults to `https://connect.squareup.com` |
| `SQUARE_API_VERSION` | Optional `Square-Version` header |
| `SQUARE_TIMEOUT_SECONDS` | urllib timeout; default 3600 seconds |
| `SQUARE_APPLICATION_ID` | Configured but unused by Ordering code |
| `SQUARE_READ_ONLY` | Does not protect Ordering write services |
| `SNAPSHOT_PROVIDER` | Does not select Ordering data; Ordering calls Square directly |

No credential value is stored in documentation or emitted by discovery.

## API map

| Endpoint | Method | Use | Pagination/batching |
|---|---|---|---|
| `/v2/vendors/search` | POST | Vendor ID/name/status sync | Cursor |
| `/v2/catalog/search-catalog-items` | POST | Items, variations, SKU, UPC, price, vendor assignment/cost | Cursor, limit 100 |
| `/v2/inventory/batch-retrieve-counts` | POST | Per-location IN_STOCK on-hand | Variation chunks of 100 plus cursor |
| `/v2/orders/search` | POST | COMPLETED order line quantities by location/day | Cursor, limit 500 |
| `/v2/inventory/changes/batch-create` | POST | Receiving ADJUSTMENT writes and emergency PHYSICAL_COUNT writes | One line/store change per request in current services |
| `/v2/inventory/changes/batch-retrieve` | POST | Stock-coverage stockout reconstruction | Variation chunks of 500 plus cursor |

The code uses `urllib.request`, not the official Square SDK.

## Identifier behavior

| Identifier | Square owner | Local representation and behavior |
|---|---|---|
| Location ID | Yes | `stores.square_location_id`; Ordering includes active stores with IDs |
| Vendor ID | Yes | `vendors.square_vendor_id`; local numeric `vendors.id` is internal FK |
| Catalog variation ID | Yes | `vendor_sku_configs.square_variation_id`; snapshotted to `purchase_order_lines.variation_id` |
| Item ID | Yes | Not stored on PO lines; item name is copied instead |
| SKU | Yes | Used as primary human/mapping key in local configuration |
| UPC/GTIN | Yes | Cached in mapping and PO line for barcode matching |
| Local PO/line/store allocation IDs | No | Erupted Admin operational identities |

## SKU, vendor, and duplicate behavior

- Catalog-by-SKU lookup keeps the first variation encountered for a duplicate SKU; it does not report the duplicate.
- Vendor sync chooses the first Square vendor assignment by `ordinal`.
- If sibling variations have no direct vendor assignment and the item has exactly one unambiguous vendor, that vendor is inherited.
- A Square variation without an SKU may be mapped locally as `VAR::{variation_id}`.
- Only one active default vendor is allowed per SKU by a partial unique index.
- Sync skips a new default mapping if the SKU is already defaulted to another local vendor.
- A local vendor/SKU row may be manually assigned a Square variation ID.
- When multiple local SKUs resolve to one variation during generation, the service warns and substitutes `SKU::{sku}` for duplicate PO line keys so both lines can exist.
- Missing catalog metadata causes the SKU to be omitted from a standard Square snapshot; if all selected mappings fail, generation is rejected.
- Mapping sync does not deactivate a local mapping merely because it disappears from the latest catalog response.

## Catalog deletion and archival behavior

Ordering catalog search does not pass an explicit include-deleted flag and contains no dedicated deleted/archived item reconciliation. Vendor sync deactivates vendors Square marks INACTIVE and also deactivates previously known vendors absent from the response. SKU mappings have no equivalent absence/deletion reconciliation, so stale active mappings can remain.

## Sales behavior

- The Ordering snapshot searches completed Square orders across all active configured locations.
- Each line item is grouped by `location_id`, `catalog_object_id`, and the UTC date parsed from `closed_at`.
- Refunds, returns, void semantics, substitutions, and timezone normalization are not separately modeled in Ordering math.
- A zero is inserted for every no-sale day in the requested window.
- There is no local ordering sales cache or sync timestamp.
- Detail-page “Sales Volume” is a separate live 30-day total across all active stores.

## Inventory behavior

- On-hand reads request `IN_STOCK` counts for mapped variation IDs and active Square locations.
- Missing count rows become zero.
- Standard generation clamps negative current on-hand to zero when converting to an integer.
- Receiving pushes an `ADJUSTMENT` from `NONE` to `IN_STOCK` for the entered received quantity.
- Emergency pushes a `PHYSICAL_COUNT` exact quantity with `ignore_unchanged_counts=false`.
- Pack size affects recommendation rounding, PDF pack display, and barcode scan increments. Receiving sends individual-unit quantity to Square and does not divide by pack size.

## Cost and price behavior

| Field | Square fact | Local behavior |
|---|---|---|
| Current unit price | Catalog variation `price_money` | Copied to PO line; refreshed only by explicit draft refresh/new generation |
| Vendor cost | Variation vendor-info price | Mapping sync copies selected vendor cost into `vendor_sku_configs.unit_cost` |
| Local unit cost | Erupted operating field | May be manually edited/imported and is used by generation |
| PO unit cost | Snapshot | Copied from mapping; draft refresh may replace it with current Square vendor cost |
| Cost history | Not provided by current flow | No versioned local history |

## Refresh, cache, and stale-data behavior

- All Ordering Square reads are live and request-driven.
- Local vendor/mapping rows are persistent caches, but there is no catalog/sales/inventory response cache.
- `vendors.last_synced_at` exists; mappings have only ordinary `updated_at`.
- No response ETag, catalog version, sales watermark, inventory watermark, or unified sync-run record is stored.
- A user may create an order from stale local vendor/mapping settings combined with live Square data.
- PO lines snapshot labels, identifiers, costs, prices, and quantities, but DRAFT refresh and IN_TRANSIT edits mean they are not guaranteed immutable historical facts.

## Failure and retry behavior

- HTTP errors expose status and response body in a raised `RuntimeError`; network errors expose the urllib reason.
- A Square `errors` array is treated as failure.
- HTTP 429/rate limits receive no special backoff or `Retry-After` handling.
- There is no automatic retry, exponential backoff, circuit breaker, shared rate limiter, or request telemetry.
- Standard generation, vendor/mapping sync, mapping pages, par pages, and emergency catalog reads generally fail the request when Square is unavailable.
- PO detail suppresses live on-hand and 30-day sales errors, displaying zero/blank reference values without a stale-data warning.
- Stock-coverage-to-draft creation tolerates catalog failure and falls back to local/report metadata.
- Receiving failed-only retry is manual and reuses deterministic `purchase-order-receive-{po}-{line}-{store}` keys.
- Emergency retry has no failed-only path and creates fresh UUID-bearing keys, so a partial-success draft can replay previously successful targets.

## Production versus local behavior

Ordering does not use the mock snapshot provider. If the environment has no Square token, live generation and related pages fail. The same code path and configured base URL perform reads and writes; there is no built-in sandbox/production label, dry-run gate, or global write-disable flag.
