# Ordering Square integration audit

No Square call was executed during discovery.

## Configuration boundary

Confirmed: Ordering uses direct `urllib` clients with bearer token, optional Square version, configured base URL, and `SQUARE_TIMEOUT_SECONDS` (default one hour). `SQUARE_APPLICATION_ID` is unused here. `SNAPSHOT_PROVIDER` does not select Ordering data. Critically, `SQUARE_READ_ONLY` protects only `square_snapshot_provider`; it does **not** guard Ordering receive, emergency, admin count, or count/recount writers.

## Read operations

| Endpoint | Trigger | Data | Pagination / failure / audit | Unintentional execution risk |
|---|---|---|---|---|
| POST `/v2/vendors/search` | Manual vendor sync | vendor IDs, names, status | Cursor; no automatic retry; route audit only after service result | Only explicit POST, but same capability as ordinary admin |
| POST `/v2/catalog/search-catalog-items` | Mapping page/sync/autofill, par page, generation, PO detail/refresh/add-line, velocity/demand/COGS helpers | items, variations, SKU, UPC, price, vendor assignments/cost | Cursor limit 100; duplicate SKU first-wins in some adapters; many failures abort, detail suppresses some | GET pages can trigger live reads; no freshness record |
| POST `/v2/inventory/batch-retrieve-counts` | Generation, par, detail, emergency seed, velocity/demand | IN_STOCK counts per variation/location | Variation chunks of 100 plus cursor; missing becomes zero | Detail failure can look like zero inventory |
| POST `/v2/orders/search` | Generation, par, detail, COGS, velocity/demand | COMPLETED orders, closed time, line quantities/money | Cursor, typically 500; UTC date grouping; no refund/stockout correction in ordering math | Read-only but synchronous and potentially one-hour request |
| POST `/v2/inventory/changes/batch-retrieve` | Velocity/Stock Coverage/Targeted Demand | inventory changes used to infer zero-stock days | Variation chunks up to 500 plus cursor | Interpretation is inferred stockout history, not a stored fact |

## Write operations

| Workflow | Change | Key | Retry / partial success | Reconciliation and audit | Guard |
|---|---|---|---|---|---|
| PO receive | `ADJUSTMENT`, NONE -> IN_STOCK, received individual units | Deterministic `purchase-order-receive-{po}-{line}-{store}` | Successful targets skipped; failed-only action reuses event/key and increments attempt | `square_sync_events` stores request/response/error; route audit summarizes | `management.admin`; not `SQUARE_READ_ONLY` |
| Emergency on-hand | `PHYSICAL_COUNT`, exact IN_STOCK | Fresh UUID-bearing key every target/every push | Any failure leaves DRAFT; next push can replay successes | Sync event each attempt; route audit summary | `management.admin`; not `SQUARE_READ_ONLY` |
| Admin full count | `PHYSICAL_COUNT`, exact IN_STOCK | Fresh UUID per line | Partial remote success then service raises; no failed-only retry | Sync events; count stays draft on failure | `management.admin`; not `SQUARE_READ_ONLY` |
| Session/recount/manual closeout | `PHYSICAL_COUNT`, exact IN_STOCK | Fresh UUID per row | No deterministic retry; automatic recount closeout can invoke writer during store submission | Sync events and route/store audit | Mixed literal ADMIN and store-submission path; not `SQUARE_READ_ONLY` |

## Failure and atomicity findings

- Local database and Square are not atomic. Square may succeed before the database event/status commit fails.
- Timeout/network failure after remote success produces an unknown outcome; current code records FAILED when it catches a runtime error, but cannot prove remote failure.
- No 429-specific backoff, `Retry-After`, circuit breaker, shared limiter, or background reconciliation exists.
- PO receiving has the strongest current idempotency but its key identifies a line/store, not a received-quantity version. Editing received quantity after a successful event does not create a new target command because success is skipped.
- Emergency and count writers use new keys and can replay successful physical counts.
- Detail-page read errors are suppressed and presented as blank/zero without explicit stale/unavailable state.
- Request payloads and some raw response/error bodies are stored; secret redaction and retention need review.

## Proposed boundary

Phase 1 must use a dedicated read gateway returning timestamped, captured snapshots and explicit stale/partial/unavailable results. No V2 code should import V1 write clients. A later write gateway must require dry-run, stable command identity, target-level idempotency, durable PREPARED state committed before remote call, outcome-unknown reconciliation, explicit approval capability, and per-target audit. Until that milestone, V2 Square is read-only by design and review, not merely by the current ineffective flag.
