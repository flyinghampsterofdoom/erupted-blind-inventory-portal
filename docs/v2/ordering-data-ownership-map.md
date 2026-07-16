# Ordering data ownership map

Canonical owner state: **V1 canonical**.

Classifications:

- **A** — Square-authoritative cached data
- **B** — V1-owned operational data
- **C** — safe shared reference data
- **D** — unsuitable for shared writes
- **E** — candidate for future V2-owned replacement only after explicit cutover

“V2 write” below means before cutover. Default is no shared writes.

## Table map

| Table/model | Purpose and important fields | Keys/constraints | V1 access | Classification | V2-safe read | V2-safe write before cutover | Main risks |
|---|---|---|---|---|---:|---:|---|
| `stores` / `Store` | Local store registry; Square location ID, name, active | PK `id`; unique Square ID in SQL | Read throughout; manual CLI writes | A/C | Yes | No | Null/stale IDs; local mock rows |
| `vendors` / `Vendor` | Square vendor cache; name/status/sync time | PK; unique `square_vendor_id` | Sync writes; all Ordering reads | A/C | Yes | No | Missing-vendor deactivation |
| `vendor_contacts` / `VendorContact` | Contact/email configuration | PK; vendor FK | No active Ordering route/service | B/E | Read-only after production-row check | No | Unknown operational/manual use |
| `ordering_math_settings` / `OrderingMathSetting` | Singleton default windows | PK/check `id=1`; validation checks | Lazily created/read | B/D/E | Yes | No | GET/calculation can create row |
| `vendor_ordering_settings` / `VendorOrderingSetting` | Vendor-specific math windows | Vendor PK/FK; validation checks | Read only in current code | B/D/E | Yes | No | No active management UI found |
| `vendor_sku_configs` / `VendorSkuConfig` | Vendor/SKU link, variation, GTIN, cost, pack, MOQ, default, active | Unique vendor+SKU; partial unique active default per SKU | Heavy read/write/sync/import | A+B+C+D | Yes with owner contract | No | Mixed Square cache and local operations; duplicate/stale mapping |
| `purchase_order_pdf_templates` / `PurchaseOrderPdfTemplate` | Generic/vendor name and disclaimer | Unique vendor; unique generic partial index | CRUD/read | B/D/E | Yes | No | “Template” does not version layout |
| `par_levels` / `ParLevel` | Global/store manual and suggested levels, confidence | Partial unique global/store indexes; nonnegative/confidence checks | Read/write/generation updates | B/D/E | Yes | No | Null vs zero; manual/dynamic overwrite |
| `purchase_orders` / `PurchaseOrder` | PO aggregate, status, math snapshot, notes, payment, PDF, actors/times | PK; vendor/principal FKs; status/payment checks | Full read/write/delete | B/D/E | Yes | No | No version; hard delete; unused states |
| `purchase_order_lines` / `PurchaseOrderLine` | Catalog snapshot, quantities, cost/price, confidence, removed | Unique order+variation; nonnegative checks | Full read/write | A snapshot+B/D/E | Yes | No | Mutable historical labels/cost; synthetic IDs |
| `purchase_order_store_allocations` / `PurchaseOrderStoreAllocation` | Expected/allocated/received per store | Unique line+store; nonnegative checks | Full read/write | B/D/E | Yes | No | Concurrent lost updates; zero semantics |
| `purchase_order_receipts` / `PurchaseOrderReceipt` | Intended receipt aggregate | PK; PO/actor FKs | No active service use | B/D | Only after production inspection | No | Unknown legacy data/intent |
| `purchase_order_receipt_lines` / `PurchaseOrderReceiptLine` | Intended receipt line facts | Unique receipt+PO line | No active service use | B/D | Only after production inspection | No | Orphaned design versus allocation receiving |
| `square_sync_events` / `SquareSyncEvent` | Durable Square command/result facts | Unique idempotency key; nullable domain FKs | Inventory writers append/update | B/C/D | Yes with sensitive payload controls | No | Polymorphic event types; DB/network boundary |
| `emergency_on_hand_drafts` / `EmergencyOnHandDraft` | Emergency aggregate/status/actors | PK; vendor/principal FKs | CRUD-like draft/push | B/D/E | Yes | No | Partial success remains editable DRAFT |
| `emergency_on_hand_draft_lines` / `EmergencyOnHandDraftLine` | SKU snapshot and JSON store quantities | Unique draft+SKU | Read/write | A snapshot+B/D/E | Yes | No | JSON store IDs; stale location set |
| `principals` | Actor identity and role | PK; username unique | Auth/attribution reads | C | Yes through approved identity service | No | Never duplicate identity |
| `web_sessions` | Browser session | Unique token; principal FK | Middleware reads/writes | D | No module-level direct read | No | Raw session security state |
| `audit_log` | Operational action facts | PK; actor/session FKs; JSON metadata | Manual append | C/D | Yes with access control | Append only through shared audit owner, not module dual-write | Incomplete/free-form history |
| `dashboard_categories`, `dashboard_card_assignments`, role category access | V1 dashboard visibility | Various unique/FKs | Dashboard reads/writes | C/D | Yes if needed | No | Visibility is not authorization |

## Important field source-of-truth matrix

| Field/group | Source system | Authoritative owner | Local storage | Refresh/sync | Square unavailable fallback |
|---|---|---|---|---|---|
| Item/variation ID | Square catalog | Square | Mapping and PO line snapshots | Live search/manual sync/refresh | Existing mapping/PO snapshot; generation may reject |
| SKU | Square catalog | Square | Mapping, par, PO, emergency rows | Live search/sync | Existing local SKU remains usable but may be stale |
| UPC/GTIN | Square catalog | Square | Mapping and PO line | Sync/refresh/scan enrichment | Existing cached value |
| Item/variation name | Square catalog | Square | PO/emergency snapshot | New line/generation/refresh | Mapping page may fail; PO snapshot remains |
| Unit price | Square catalog | Square | PO line snapshot | Generation/refresh | Prior PO value or null |
| Square vendor ID/name/status | Square vendors | Square | `vendors` | Manual sync | Last local cache |
| Vendor assignment | Square catalog vendor info, with local default rule | Square fact plus Erupted operating choice | Mapping | Sync/manual edit | Existing mapping |
| Unit cost | Square vendor info and local operations | Mixed; current effective generation owner is local mapping | Mapping and PO line | Sync/manual edit/refresh | Existing local value |
| Pack size/MOQ/order unit | Erupted Admin | V1 | Mapping | Manual edit/import | Existing local value |
| Store/location | Square location + local activation | Square identity; V1 operational activation | `stores` | Manual store sync | Existing local rows |
| Live on-hand | Square inventory | Square | Not cached for Ordering | Every generation/detail/report | Generation fails; detail shows zero; stock report fails |
| Sales transactions/line items | Square orders | Square | No Ordering cache; PO only stores derived output | Every generation/report | Generation/report fails |
| Reorder/stock-up/history windows | Erupted Admin | V1 | Config, settings, PO snapshot | Environment/lazy singleton/vendor override/user form | Local values |
| Manual level/par | Erupted Admin | V1 | `par_levels` | Manual save/prefill | Local values |
| Suggested level/par/confidence | Derived | V1 algorithm from Square/local inputs | Par and PO line snapshots | Generation/prefill | Cannot safely recompute without Square |
| Ordered quantity/store split | Erupted Admin | V1 | PO line/allocation | Draft/IN_TRANSIT edits | Local values |
| Received quantity | Erupted Admin observation | V1 until Square push; Square then owns live inventory result | Allocation/line totals | Manual/scan | Local values remain; push may fail |
| PO status | Erupted Admin | V1 | `purchase_orders.status` | Route transitions | Local value |
| Payment status/date/amount/note | Erupted Admin | V1 | PO fields | Manual save | Local value |
| PDF layout disclaimer | Erupted Admin | V1 | Template | Manual save | Built-in layout/no disclaimer |
| Generated PDF bytes/path | Local filesystem/V1 | V1 | File plus `pdf_path` | Submit/download regeneration | Missing file is regenerated if possible |
| Audit actor/action | Erupted Admin | Shared V1 audit | `audit_log` | Manual route calls | No fallback |
| Square request/result | Erupted Admin integration record | V1 | `square_sync_events` | Each write attempt | Failure stored for created events |

## Write ownership rule

Before a separately approved Ordering cutover:

- V1 is the only writer to Ordering operational tables.
- V2 may read approved reference and history data for a read-only view.
- V2 must not update mappings, pars, POs, allocations, payments, templates, emergency drafts, or sync events.
- V2 must not issue Square inventory writes.
- There are no dual writes, shadow writes, automatic backfills, or row migrations.
