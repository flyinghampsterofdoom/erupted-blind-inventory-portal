# V1 jobs and scripts inventory

## Finding

There is no application scheduler, cron registration, Celery/RQ worker, background task, webhook consumer, or recurring reconciliation job in the repository. All business synchronization/report work is request-driven or manually invoked. The only recurring browser behavior is autosave/session polling in `base.html`.

## Server and setup tasks

| Task/script | Trigger/frequency | Inputs | Outputs/tables | Failure handling | Registration status |
|---|---|---|---|---|---|
| `scripts/bootstrap_and_run.sh` | Manual CLI or either macOS `.command` launcher; every run | Mode (`run` or `setup-only`), environment, optional `PORT` | Creates `.env` if absent, virtualenv/dependencies, PostgreSQL database; applies all of `sql/schema.sql`; runs seed; starts Uvicorn | `set -euo pipefail`; some install/DB creation commands intentionally tolerate failure; waits up to 10 s for Postgres | Active primary local launcher per README |
| `Setup Blind Inventory Portal.command` | Manual double-click | None | Calls bootstrap `setup-only` | Shell exits on failure, then pauses on success | Active convenience launcher |
| `Start Blind Inventory Portal.command` | Manual double-click | None | Calls full bootstrap then starts server | Shell exits on failure | Active convenience launcher |
| `app.main._ensure_runtime_schema` | Every FastAPI startup | Current DB connection/config | Adds `gtin` columns to `vendor_sku_configs` and `purchase_order_lines` if absent | Startup fails if DDL fails | Actively registered `startup` event; schema-mutating |
| SQL updated-at triggers | Every UPDATE on registered tables | Row mutation | Updates `updated_at` | DB-level behavior | Registered by `sql/schema.sql`; not a scheduled job |

## Data imports and synchronization scripts

| Script/task | Trigger/frequency | Inputs | Outputs/tables | Failure handling/idempotency | Registration status |
|---|---|---|---|---|---|
| `python -m app.sync_square_stores` | Manual CLI only | Square token/base/version/timeout; `--deactivate-missing` | Upserts `stores` by `square_location_id`; optional soft deactivation | Raises on HTTP/network/API error; DB transaction; reruns update existing IDs | Present, documented only indirectly; not called by bootstrap or UI |
| `python -m app.sync_square_campaigns` | Manual CLI; same function also called by management route | `--min-items`, `--deactivate-missing`, Square catalog | Upserts/soft-deactivates `campaigns` keyed by category-filter text | Cursor pagination, transaction, exception on Square failure | CLI is documented; UI route is active; not scheduled |
| Vendor sync | Manual POST `/management/ordering-tool/vendors/sync` | Square vendor search | Upserts/soft-deactivates `vendors` | Transaction; route redirects with error | Active UI action, not a standalone script/schedule |
| Vendor SKU CSV import | Manual multipart POST | User CSV with mapping columns | Upserts `vendor_sku_configs` | Parses whole upload in memory; errors redirect | Active UI import |
| Vendor SKU Square auto-fill/sync | Manual ordering POST | Current Square catalog/vendor assignments | Updates mapping variation IDs/cost/pack metadata | Transaction; no durable run record | Active UI action |

## Seed/backfill behavior

| Task | Trigger | Data affected | Operational concern |
|---|---|---|---|
| `app.seed_example.seed` | Every bootstrap | Ensures Downtown mock store, demo campaigns/groups/rotation, ADMIN `manager`, LEAD `lead1`, STORE `store1` with known example passwords | Intended for local MVP, but bootstrap invokes it unconditionally. It is idempotent by names, not environment-gated |
| Default dashboard categories | First management dashboard/settings access when table empty | Inserts category rows | A read-looking request can write |
| Default opening checklist items | First checklist access per store | Inserts checklist item hierarchy | No explicit migration/version of template; later code default changes do not necessarily update existing stores |
| Default daily chore tasks | Chore service initialization | Inserts task rows for active stores | Global editor semantics depend on per-store copies |
| Default non-sellable items | Stock-take service access if empty | Inserts catalog items | Read can write; catalog is global |
| Current inventory/par row initialization | Change box, master safe, store par service access | Inserts denomination/settings/par rows | Read can materialize current-state rows |
| Ordering math settings | First ordering calculation | Inserts singleton defaults from environment | Historical behavior depends on when settings were first materialized |

## User-triggered reconciliation and cleanup

| Operation | Trigger | Inputs/outputs | Failure handling | Notes |
|---|---|---|---|---|
| Cash reconciliation load | Browser filter submit | Live Square expected cash + local actual/history | Errors shown in page; no retry/cache | Not scheduled despite reconciliation semantics |
| Cash verification save | Browser Save | Upserts actuals; appends verification and optional batch | Transaction rollback on error | Expected snapshot can differ from later live recomputation |
| Count closeout | Store count submit | Evaluates 3-match recount and may push Square immediately | Push failures audited; submission still has complex partial outcomes | Synchronous hidden integration inside submit |
| PO receiving retry | Manual retry-failed POST | Reuses deterministic sync events for failed store/line pushes | Successes skipped, failures retried | No worker automatically retries |
| Session purge | Manual admin POST | Hard-deletes selected count sessions; cascades related rows per FK | Transaction and audit action | Operational cleanup, not retention policy |
| Draft deletions | Manual UI | Hard-deletes admin counts, chore drafts, change-box counts, PO drafts depending on module | Confirmation in browser, transaction | No recycle bin/backfill |
| PDF regeneration/cleanup | PO PDF download/submit and draft edits | Deletes stale local file, writes new PDF, updates `pdf_path` | Exceptions surface to route | Local filesystem lifecycle only |

## Exports

Exports are synchronous request responses, not jobs: admin-count XLSX, purchase-order PDF, nine CSV route variants (four sales, targeted demand, stock coverage, inventory velocity, stock value, session variance). They are regenerated per request and are not registered or retained, except PO PDFs.

## Browser recurring behavior

- Dirty `data-autosave=true` forms save every 30 seconds.
- Session liveness is checked every 15 seconds, but only when at least one autosave form exists because `base.html` returns early when no autosave forms are found.
- Visibility change and page unload trigger best-effort autosave; unload uses `navigator.sendBeacon`.
- Store chore checklist adds an additional immediate fetch save on checkbox changes.
- These are client timers, not server jobs, and cease when the page is closed.

## Operationally important scripts absent from repository

No backup, restore, archival, audit retention, session cleanup, PDF cleanup, dead-letter retry, Square reconciliation, cache warming, or scheduled report-delivery script was found.
