# V2 touchscreen flavor finder

Status: additive owner-preview module. Management navigation and `/v2/touchscreen/*` routes are behind default-disabled `touchscreen_v2`. V1 routes, data ownership, navigation, and Square workflows remain unchanged.

## Architecture

The management module lives under `/v2/touchscreen/*`. The customer application lives under `/touchscreen/{device_token}` and has no employee navigation. The customer runtime is controlled by device credentials rather than `touchscreen_v2`, so disabling the management feature key does not stop already provisioned devices. Device tokens are revealed once, stored only as SHA-256 hashes, bound to one store, and independently revocable. Customer APIs always derive the store from the authenticated device and ignore customer-supplied store scope. A full-module runtime kill switch is deferred as TD-004.

The application remains single-business. ADMIN is the Owner persona. ADMIN and MANAGER receive all touchscreen capabilities by default; LEAD and STORE receive none.

## Local Square read model

Square remains authoritative, but customer interactions never call Square. `touchscreen_square_variation_cache` stores identity and sellability metadata; `touchscreen_store_inventory_cache` stores store/variation quantity; `touchscreen_sync_runs` records completeness and freshness.

These tables and synchronization processes are owned exclusively by Customer Touchscreen and Flavor Finder. Ordering Intelligence, Product Lifecycle, Archived Products, and future Stagnant Inventory do not read them, populate them, trigger their refresh, or treat them as fallback data. Ordering catalog identity is governed separately by the [Product Lifecycle workspace data contract](./ordering/product-lifecycle-workspace.md).

Synchronization validates the complete external response in memory before replacing the cache in one transaction. A timeout, API error, malformed response, partial matrix, or unexpectedly empty response records a failed run and leaves the previous successful cache intact. Active runs are never customer-visible. The command is:

```sh
python -m app.sync_touchscreen_inventory
```

Management can also run and inspect synchronization at `/v2/touchscreen/sync`. `TOUCHSCREEN_CACHE_MAX_AGE_MINUTES` controls fail-closed freshness. Once stale, customer endpoints return only a staff-facing unavailable message.

## Availability and classifications

A published, visible flavor appears only when an active/sellable explicitly linked variation has quantity above the global or store/flavor threshold at the device store. Salt/freebase and iced/non-iced values come only from management mappings. Categories and fruit varieties are managed values. Fruit multi-selection uses OR matching.

Directional recommendations pass the same publication, store, format, inventory, and freshness gates. Exact quantities and internal synchronization details are never returned to customer APIs.

## Media

Touchscreen images reuse `digital_signage_media_assets`, the private R2 adapter, and the existing decoded MIME/extension/dimension validation. New objects use `touchscreen/images/` keys. `touchscreen_flavor_media` owns flavor-specific roles and alt text. Square image fields are neither synchronized nor referenced. Missing images render a local CSS placeholder. Media archival checks include both Digital Signage and Touchscreen references.

## Exposure and rollback

Set `V2_ENABLED_FEATURES=touchscreen_v2` only for an approved global preview, or add `<principal_id>:touchscreen_v2` to `V2_PRINCIPAL_FEATURES` for a named tester. Exposure does not change authorization or canonical ownership. Rollback disables management exposure first, separately revokes provisioned device credentials when customer access must stop, and restores a schema-compatible application commit if necessary. Touchscreen revision `20260720_0006` remains a required ancestor of current schema head `20260725_0008`.

The current release-hardening run did not execute PostgreSQL integration or real R2 verification. Square remained read-only and no real Square request was made. See [V2 test verification](./v2-test-verification.md).
