# V2 touchscreen flavor finder

Status: additive owner-preview module behind `touchscreen_v2`. It is disabled by default. V1 routes, data ownership, navigation, and Square workflows remain unchanged.

## Architecture

The management module lives under `/v2/touchscreen/*`. The customer application lives under `/touchscreen/{device_token}` and has no employee navigation. Device tokens are revealed once, stored only as SHA-256 hashes, bound to one store, and independently revocable. Customer APIs always derive the store from the authenticated device and ignore customer-supplied store scope.

The application remains single-business. ADMIN is the Owner persona. ADMIN and MANAGER receive all touchscreen capabilities by default; LEAD and STORE receive none.

## Local Square read model

Square remains authoritative, but customer interactions never call Square. `touchscreen_square_variation_cache` stores identity and sellability metadata; `touchscreen_store_inventory_cache` stores store/variation quantity; `touchscreen_sync_runs` records completeness and freshness.

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

Set `V2_ENABLED_FEATURES=touchscreen_v2` only for an approved global preview, or add `<principal_id>:touchscreen_v2` to `V2_PRINCIPAL_FEATURES` for a named tester. Exposure does not change authorization or canonical ownership. Rollback disables exposure first and restores the prior application commit; the additive migration may remain without affecting V1.
