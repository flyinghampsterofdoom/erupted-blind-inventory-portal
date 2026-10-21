# V1 external integration map

## Integration overview

Square is the only implemented network integration. PostgreSQL and the local filesystem are operational dependencies. “Email” is not implemented: the variance notification writes an audit event, and purchase-order contact/email fields are not connected to a sender. There are no incoming webhooks.

No credentials are reproduced here. Configuration names only are documented.

## Square

### Shared configuration

- `SQUARE_ACCESS_TOKEN`: Bearer credential used by all Square clients.
- `SQUARE_APPLICATION_ID`: configured but not referenced by current application code.
- `SQUARE_API_BASE_URL`: defaults to `https://connect.squareup.com`.
- `SQUARE_API_VERSION`: optional `Square-Version` header.
- `SQUARE_TIMEOUT_SECONDS`: urllib timeout, default 3600 seconds.
- `SQUARE_READ_ONLY`: checked only by `SquareSnapshotProvider`; it does **not** prevent other inventory-write services.
- `SNAPSHOT_PROVIDER`: chooses mock or Square for rotating counts at import time through a cached provider.

The code has several independent urllib clients with duplicated headers, pagination, error parsing, and URL normalization. There is no official SDK, shared retry policy, exponential backoff, circuit breaker, rate-limit coordination, cache, or request telemetry.

### Integration points

| Integration | Endpoints/calls | Direction and data | Trigger/schedule | Persistence/snapshots | Failure, retry, idempotency | V2 risks |
|---|---|---|---|---|---|---|
| Store sync CLI | `GET /v2/locations` | Square→`stores`: name, ID, active status | Manual `python -m app.sync_square_stores`; not registered/scheduled | Local store rows become authoritative references; optional soft-deactivate missing | One request; raises on HTTP/network/API errors; no retry | No UI entry point; local stores without Square IDs and deactivation semantics need a decision |
| Campaign/category sync | `POST /v2/catalog/search-catalog-items`, `/v2/catalog/search` | Square reporting categories→`campaigns` | Manual CLI or `/management/groups/sync-campaigns` | Category name stored in `label`/`category_filter`; optional deactivation | Cursor pagination; route catches errors; no retry/idempotency record | Category renames alter campaign identity by text; current count history retains campaign FK but semantics can drift |
| Count group coverage audit | Same catalog endpoints | Reads current variations/categories and compares coverage to group mappings | Manual management GET | Result is not stored; only audit-log action | Raises/catches at route; no retry | Live catalog can produce non-repeatable audit output |
| Rotating count snapshot | Catalog search, category search, inventory batch retrieve | Square→snapshot: variation IDs/SKUs/names and expected on-hand | Store generates/submits a count | `snapshot_lines` preserve catalog labels and expected value; submit refreshes on-hand timestamp | Provider raises; no retry. `SQUARE_READ_ONLY=true` required by this provider | Provider is cached at import, complicating config changes/tests; category filter is exact name match |
| Count Square push | `POST /v2/inventory/changes/batch-create` | Local counted quantity→Square physical count | Manual admin full/recount push and automatic 3-match closeout | `square_sync_events` stores request/response/error; snapshots/entries remain history | Count pushes use UUID idempotency keys, so manual repeat creates a new Square operation. Event status PENDING/SUCCESS/FAILED; manual retry is another route invocation | Duplicate operational intent can create multiple events; permissions use literal ADMIN; auto-closeout occurs inside submit workflow |
| Full admin store count | Catalog/on-hand helpers plus inventory batch-create | Square live inventory→draft, then local full count→Square | Admin page/submission | Admin count/lines and sync events stored | Per-line/store sync events; failure surfaces to UI; no background retry | Submit couples DB transaction and network writes; partial Square success is possible |
| Vendor sync | `POST /v2/vendors/search` | Square vendors→local `vendors` | Manual ordering button | Upserts Square vendor ID/name/active/last sync | Pagination; route error handling; no retry | No schedule; vendor contacts/settings not populated |
| Vendor SKU mapping sync | Catalog search-catalog-items | Square catalog vendor assignments/SKUs/costs/variation IDs→`vendor_sku_configs` | Manual auto-fill/sync during ordering | Local mapping becomes critical join for reports and orders | No retry or sync run table | Default-vendor rules and first-vendor cost selection can change report/order results |
| Ordering snapshot/generation | Catalog search, inventory counts, orders search | Square catalog/on-hand/sales→recommendations | Manual order generation/report | PO lines/allocations snapshot quantities, confidence, cost; source Square responses not retained | No central retry; request failure aborts route | Recalculation cannot reproduce historical source exactly without Square history; long timeout and many paginated calls |
| Emergency on-hand | Inventory batch-create | Local true on-hand JSON→Square | Manual push | Draft/line JSON plus sync events | Fresh key includes UUID for each line/store; failures stored; draft marked PUSHED only if all succeed | Retry after partial success may repush successful locations because keys change |
| Purchase-order receiving | Inventory batch-create | Received store allocation quantities→Square | Manual receive/retry-failed | Deterministic sync event key `purchase-order-receive-{po}-{line}-{store}` with payload/result | Failed events reset to PENDING and reuse key; successful events are skipped, providing useful idempotency | Strongest idempotency implementation, but DB/network transaction boundaries still require validation |
| Cash reconciliation | locations detail, payments search/list, refunds, cash-drawer shifts/events | Square cash events→expected daily totals | Browser filter/load | Expected is live; verification rows may snapshot expected cents; actuals and batches stored | Falls back between payments search/list paths; errors returned to fetch UI; no retry/cache | Historical Square corrections change recomputed expected values; timezone comes from Square location; shift APIs can be permission-sensitive |
| Sales reports | locations, orders search, payments, team-members search | Square→live sales/employee/location reports | Page/CSV request | No report snapshot/cache | Cursor pagination; raises on failures; no retry | Same date request can change; employee attribution uses largest completed payment and current team-member data |
| COGS | catalog plus orders search; local mapping cost fallback | Square orders/catalog + local cost→live COGS | Page request | Not stored | No retry/cache | Cost is current, not transaction-time cost; historical COGS may drift |
| Inventory value | catalog and inventory counts | Square current quantities/cost/price→valuation | Page/CSV request | As-of timestamp only in response | No retry/cache | First vendor cost/current price, not historical cost; negative quantities are included |
| Velocity/stock coverage/targeted demand | orders search, inventory changes batch-retrieve, inventory counts, catalog | Live/historical Square movement→demand, stockout correction, reorder suggestions | Page/CSV; stock coverage can create PO | PO creation snapshots selected recommendations; reports otherwise transient | Batched pagination; no retry/cache | Complex derived values depend on Square event completeness, current mappings/costs, and timezone/date boundaries |

### Square write safety

- `SquareSnapshotProvider` refuses to initialize unless `SQUARE_READ_ONLY=true`, but `count_square_sync_service`, `admin_store_count_service`, emergency ordering, and receiving still issue inventory writes under that same configuration.
- There is no global dry-run flag for writes.
- `square_sync_events` is the only durable request/result log. It is shared by count, emergency, admin-count, and receiving sync types.
- No webhook consumes Square updates; all synchronization is pull-on-demand or direct push.

## PostgreSQL

| Aspect | Current behavior | Risk/compatibility note |
|---|---|---|
| Driver | SQLAlchemy + psycopg, `pool_pre_ping=True` | All user workflows depend synchronously on DB |
| Setup | Bootstrap installs PostgreSQL 16, creates DB, reapplies `sql/schema.sql`, then seeds | Idempotent SQL acts as migration system; no version ledger/rollback |
| Startup | `ensure_runtime_schema` executes additive GTIN columns | Starting any app instance can mutate schema |
| Extensions/types | `citext`, INET, JSON, PostgreSQL enum types | V2 must remain PostgreSQL-aware or normalize these types deliberately |
| Updated timestamps | DB trigger `set_updated_at()` on many tables | ORM assignment alone does not explain timestamp behavior; preserve triggers during cutover |

## Filesystem and document generation

- Purchase-order PDFs are generated with ReportLab under `generated/purchase_orders/` and referenced by `purchase_orders.pdf_path`.
- A stale/missing PDF is regenerated on download/submit; replacing a draft attempts to unlink the old file.
- Files are local-instance state. There is no object storage, backup, or multi-instance coordination.
- Admin full-store count exports an in-memory XLSX with OpenPyXL.
- Other exports are in-memory CSV streams.

## Notification/email status

- `send_variance_report_stub` records `VARIANCE_EMAIL_STUB_SENT` in `audit_log`; it sends no network message.
- `vendor_contacts`, `purchase_orders.email_sent_at`, and `email_sent_by_principal_id` have no active route/service use.
- No SMTP/API credentials or email provider are configured.

## Other external dependencies

- Homebrew/GitHub install script: bootstrap may download Homebrew if absent and install Python/PostgreSQL.
- Package installation uses PyPI via pip during bootstrap.
- No Slack, Google, accounting, identity-provider, webhook, storage, or analytics integration is present in the repository.
