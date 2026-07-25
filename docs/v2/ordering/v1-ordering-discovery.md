# V1 Ordering domain discovery

Status: confirmed current implementation unless labeled otherwise. V1 is canonical. No production database was queried, so actual row populations and manually used schema fields remain unverified.

## Evidence reviewed

- `app/routers/management.py`, related store count/session routes in `app/routers/store.py`.
- `app/models.py`, `sql/schema.sql`, Alembic baseline, configuration, and startup wiring.
- Ordering, Square, PDF, receiving, emergency inventory, COGS, inventory velocity, targeted-demand, count-sync, admin-count, and non-sellable services.
- Seven Ordering/report templates plus related count/non-sellable templates.
- Ordering math, generation, mapping, Square-data, receiving, velocity, demand, and recount tests.
- Existing `docs/v2/ordering-*`, V1 application/data/route/report/integration/permission maps.

## Common route contract

Unless a row says otherwise, all Ordering Tool routes are under `/management`, require an authenticated session plus effective `management.admin` (fallback ADMIN/MANAGER), use all active stores/vendors rather than principal store scope, and POSTs require CSRF. The router owns audit and commit. Validation failures are either HTTP 400 or redirect query errors; Square calls are synchronous. There is no automatic retry except the explicit receiving failed-only action. No Ordering route has optimistic concurrency or a request idempotency token.

Stock Coverage, Inventory Velocity, Targeted Demand, Stock Value, count Square push, and several sales reports require literal `ADMIN`, not `management.admin`. COGS requires `management.access`. These inconsistencies are confirmed current behavior.

## Ordering, vendor, par, and PDF routes

| Route | Inputs and validation | Reads | Writes / state / side effects | External call | Visible result | Tests and risk |
|---|---|---|---|---|---|---|
| GET `/management/ordering-tool` | None | vendors, POs/lines, emergency drafts | Lazy ordering defaults may be created indirectly | None | Generation controls and combined newest-100 standard/emergency table | No route test; “current/history/payment” are not separate resources |
| GET `/management/ordering-tool/mappings` | Optional vendor filter | vendors, mappings; live catalog names | None | Catalog search | Mapping editor | Square failure aborts; duplicate SKU lookup is first-response dependent |
| POST `/management/ordering-tool/mappings/upsert` | Active vendor, nonblank SKU, cost >=0, pack >=1, MOQ >=0, booleans | vendor, existing mapping | Upsert mapping; route audit | None | Redirect with status/error | Unit validation indirectly tested; concurrent default-vendor conflicts not route-tested |
| POST `/management/ordering-tool/mappings/import` | CSV requires `vendor_id`,`sku`; optional variation/cost/pack/MOQ/default/active | vendor/mappings | Per-row upserts in one transaction; valid rows survive row errors if transaction commits; audit summary | None | Processed/error counts | No full CSV/atomicity suite |
| POST `/management/ordering-tool/mappings/bulk-save` | Compact submitted rows; same field rules | mappings/vendors | Upserts displayed rows; audit | None | Redirect with counts/errors | Large-field regression test exists; concurrency missing |
| POST `/management/ordering-tool/mappings/auto-fill` | Optional vendor | active mappings; live catalog-by-SKU | Fills only missing variation IDs; audit | Catalog search | Updated/skipped counts | Duplicate SKU ambiguity risk |
| POST `/management/ordering-tool/vendors/sync` | None | current vendors/mappings | Upserts/deactivates vendors; syncs vendor assignments/costs into mappings; audit | Vendors and catalog search | Created/updated/deactivated counts | Incomplete Square response may deactivate vendors; no end-to-end test |
| GET `/management/ordering-tool/par-levels` | None | active vendors | None | None | Vendor landing | No route test |
| GET `/management/ordering-tool/par-levels/{vendor_id}` | Active vendor; lookback validated | vendor mappings, stores, pars, ordering defaults; live sales/on-hand | Lazy math singleton may be created | Catalog, orders, inventory counts | Store/SKU level-par matrix and confidence | Math tests exist; live page/error behavior untested |
| POST `/management/ordering-tool/par-levels/{vendor_id}/save` | Store/SKU, nullable nonnegative manual level/par | par rows | Upsert store pars; `MANUAL` if either value non-null; audit | None | Saved count | Null/zero semantics tested partially |
| POST `/management/ordering-tool/par-levels/{vendor_id}/prefill` | Active vendor, valid lookback | live sales/on-hand, mappings, pars | Fills missing manual values, locks manual, audit | Catalog/orders/inventory | Prefilled count | Converts suggestions into manual values; owner intent ambiguous |
| GET `/management/ordering-tool/pdf-templates` | None | vendors/templates | None | None | Generic/vendor assignments | No PDF behavior test |
| POST `/management/ordering-tool/pdf-templates/save` | Generic/vendor selections, name/disclaimer | templates/vendors | Create/update assignments; audit | None | Redirect | Template name does not change layout |
| POST `/management/ordering-tool/pdf-templates/{id}/edit` | Name/disclaimer | template | Update; audit | None | Redirect | No concurrency/versioning |

## Purchase-order and receiving routes

| Route | Inputs and validation | Reads | Writes / transition / side effects | External call | Visible result | Tests and risk |
|---|---|---|---|---|---|---|
| POST `/management/ordering-tool/generate` | Vendor IDs; reorder >=1; stock-up > reorder; lookback >=7 | mappings, settings, pars, open transit, stores; live catalog/sales/on-hand | Sync mappings; update suggested par/confidence; create one DRAFT PO/vendor with lines/allocations; audit | Catalog, orders, inventory | Redirect to created order(s), warnings | Math/generation unit tests; no route/concurrency fixture |
| POST `/management/ordering-tool/generate-full-stock` | Same | Same plus non-default mappings | Same, retaining zero lines | Same | Broad editable drafts | Include-zero tests exist |
| GET `/management/ordering-tool/orders/{id}` | Numeric PO ID | PO graph, mappings, sync events; live 30-day sales/on-hand | None | Catalog/orders/inventory; failures suppressed to blank/zero reference data | Editable detail appropriate to status | No route leakage/concurrency test |
| GET `/management/ordering-tool/orders/{id}/pdf` | Existing PO with lines | PO/lines/vendor/current template/current pack mappings | May regenerate file and flush `pdf_path` when missing/stale | None | PDF download | No golden/layout/history test; GET can mutate file/path |
| POST `/management/ordering-tool/orders/{id}/save` | Line quantities, removals, manual pars, store allocations; optional received fields | PO graph | Edits DRAFT or IN_TRANSIT; zeros/removes lines; updates pars/allocations/received totals; audit | None | Saved redirect | Last write wins; IN_TRANSIT remains mutable |
| POST `/management/ordering-tool/orders/{id}/invoice` | PAID/UNPAID; PAID requires date/amount; mismatch requires note | PO active lines | Overwrites four invoice fields; audit | None | Saved/error redirect | No partial/multi-payment or history tests |
| POST `/management/ordering-tool/orders/{id}/add-line` | SKU, initial qty | active vendor mapping, catalog, stores | Add or restore line/allocations; audit | Catalog | Redirect | Mapping/cost freshness risk |
| POST `/management/ordering-tool/orders/{id}/refresh-lines` | DRAFT only | live catalog/vendor costs | Overwrites label, SKU, GTIN, price, cost snapshot fields; audit | Catalog | Updated/missing counts | Historical drift if used before submit; no golden test |
| POST `/management/ordering-tool/orders/{id}/submit` | DRAFT, at least one positive line | PO graph/current template/current pack mappings | DRAFT -> IN_TRANSIT; records actor/times; writes filesystem PDF; audit | None | Submitted detail | No approval separation; DB/file atomicity absent |
| POST `/management/ordering-tool/orders/{id}/received-quantities` | IN_TRANSIT; nonnegative per line/store | active stores, PO graph | Creates missing allocations; overwrites received cells; recomputes totals; audit | None | Saved redirect | Partial receiving is numeric only; no receipt event |
| POST `/management/ordering-tool/orders/{id}/scan-barcode` | IN_TRANSIT, nonblank barcode | lines, mappings, allocations, active stores | Increment allocation by mapping pack; may create synthetic overage line; audit | None | JSON result | Receiving unit tests; exact scan event not persisted |
| POST `/management/ordering-tool/orders/{id}/scan-barcode/cancel` | IN_TRANSIT, line/store with qty >0 | line/allocation | Subtracts one unit; may remove synthetic line; audit | None | JSON result | Confirmed defect: pack scan cancellation subtracts one, not pack increment |
| POST `/management/ordering-tool/orders/{id}/receive` | IN_TRANSIT, positive received cells, valid variation/location | PO graph, mappings, prior sync events | Per-target sync events; on full success IN_TRANSIT -> SENT_TO_STORES; audit | Square inventory ADJUSTMENT | Success or partial-failure redirect | Deterministic target key; local/remote atomicity gap; service helpers tested, end-to-end writer not |
| POST `/management/ordering-tool/orders/{id}/receive-retry-failed` | Same; only FAILED targets | Same | Reuses events/idempotency keys, increments attempts; full success -> SENT_TO_STORES; audit | Square ADJUSTMENT | Retry outcome | No network-uncertainty/reconciliation test |
| POST `/management/ordering-tool/orders/{id}/delete` | DRAFT or IN_TRANSIT | PO and `pdf_path` | Hard-deletes graph; best-effort current file deletion; audit retains ID only | None | Main page | Operational-history loss risk; no retention test |

## Emergency on-hand routes

| Route | Inputs and validation | Reads | Writes / state / side effects | External call | Visible result | Tests and risk |
|---|---|---|---|---|---|---|
| GET `/management/ordering-tool/emergency-editor` | Optional numeric `draft_id` | vendors, draft/lines, active stores, mappings; live catalog/inventory for editor detail | None | Catalog and inventory reads | Current/new editor or friendly schema/load error | Broad admin scope; live-read freshness is not labeled |
| POST `/management/ordering-tool/emergency-editor/start-draft` | Active numeric `vendor_id` | vendor and available mappings | Creates DRAFT header/lines seeded by service; creator recorded | None | Redirect to draft | No request idempotency; double submission may create two drafts |
| POST `/management/ordering-tool/emergency-editor/{draft_id}/add-sku` | Nonblank SKU/GTIN lookup resolving within draft vendor | draft and vendor mappings | Adds/restores draft line | None | Redirect with matched SKU/error | Ambiguous lookup and concurrent add are not fully characterized |
| POST `/management/ordering-tool/emergency-editor/{draft_id}/save` | Decimal `qty__{line}__{store}` cells; invalid keys/decimals are silently skipped, service validates draft/state/scope | draft graph and stores | Overwrites requested physical-count quantities; route audit | None | Saved redirect | Silent form skips and last-write-wins; no row version |
| POST `/management/ordering-tool/emergency-editor/{draft_id}/push` | Same quantity form; only valid DRAFT targets are pushable | draft graph, mappings, stores | Saves cells, attempts per-target sync events; all success DRAFT -> PUSHED, any failure remains DRAFT; route audit | Square `PHYSICAL_COUNT` | Rendered attempted/succeeded/failed result | Fresh keys on every push can replay earlier successes; local/Square non-atomic; no reconciliation |

## Indirect creation, reports, counts, and replenishment dependencies

| Route/workflow | Permission/scope | Confirmed behavior and domain relevance |
|---|---|---|
| GET/CSV `/management/reports/stock-coverage-purchase` | literal ADMIN; optional one store | Live Square sales/inventory/changes plus mapping costs; stockout-adjusted demand and store splits |
| POST `/management/reports/stock-coverage-purchase/create-order` | literal ADMIN; optional one store; selected vendor | Creates a DRAFT PO from visible report rows; the only indirect PO writer |
| GET/CSV `/management/reports/inventory-velocity` | literal ADMIN; optional one store | Velocity, stockout adjustment, days supply, trend, reorder and transfers |
| GET/CSV `/management/reports/targeted-sku-demand` | literal ADMIN; optional one store and selected variations | Targeted store-specific purchase need and lost-demand adjustment |
| GET/CSV `/management/reports/stock-value-on-hand` | literal ADMIN | Current on-hand valued using current mapping costs |
| GET `/management/reports/cogs` | `management.access`; all active Square stores | Completed Square order quantities multiplied by current preferred mapping cost; not PO/payment-linked |
| GET/CSV `/management/reports/sales-by-vendor` | literal ADMIN | Live sales grouped through current vendor mapping |
| Admin/store count/session routes | mixed admin/store permissions | Snapshot/count/recount results can write exact Square physical counts; do not feed PO recommendations directly today |
| Non-sellable stock take routes | store/management permissions | Separate non-catalog supply domain; no subtraction from Ordering sellable inventory |
| Store par reset/delivery | `management.admin` | Replenishes change/non-sellable supplies and creates submitted non-sellable take; unrelated to vendor PO tables but shares “replenishment” terminology |

## Files and processes

- Services: `ordering_service`, `purchase_order_math_service`, `purchase_order_generation_service`, `purchase_order_admin_service`, `square_ordering_data_service`, `square_vendor_service`, `ordering_emergency_service`, `inventory_velocity_report_service`, `targeted_sku_demand_report_service`, `cogs_report_service`.
- Templates: `management_ordering_tool`, `management_ordering_order_detail`, `management_ordering_mappings`, `management_ordering_par_levels`, `management_ordering_par_levels_vendor`, `management_ordering_pdf_templates`, `management_ordering_emergency_editor`, `management_stock_coverage_purchase`.
- Imports/exports: mapping CSV input; stock coverage, velocity, targeted demand, stock value, and vendor sales CSV; PO PDF. No PO CSV/EDI/accounting export.
- Scheduled/background work: none. Vendor sync, recommendation generation, PDF generation, receiving writes, retries, and reports are request-driven. No email sender or vendor portal exists.

## Confirmed gaps

No current model or workflow represents recommendation decisions, do-not-reorder status, dated exclusions, vendor lead time, order placement acknowledgement, vendor submission, shipment, backorder, damage, mis-ship, transfer-in-flight, payment event/account/method, COGS allocation, or immutable order artifact. Receipt tables and several PO enum values exist but are unused by active services.
