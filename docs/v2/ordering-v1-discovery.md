# V1 Ordering Tool discovery record

Status: discovery and parity planning only

Canonical owner: **V1 canonical**

Discovery date: 2026-07-16

Governing principle: [V1 Preservation Guarantee](./v1-preservation-guarantee.md)

## Executive summary

The V1 Ordering Tool is a synchronous FastAPI/Jinja/PostgreSQL workflow for management users. It:

1. imports Square vendors and catalog vendor assignments into local vendor/SKU configuration;
2. reads live Square catalog variations, completed sales, and per-location inventory;
3. combines those facts with local vendor mappings, pack sizes, minimum quantities, store-level par/level settings, and open in-transit allocations;
4. creates one editable local purchase-order draft per selected vendor;
5. allows manual line, quantity, allocation, catalog-refresh, invoice, and receiving changes;
6. generates a vendor PDF when the draft is submitted;
7. records per-store received quantities through manual entry or barcode scans; and
8. pushes received quantities to Square inventory with durable per-line/store sync events.

The main route also includes emergency true-on-hand drafts that directly set Square inventory. A separate Stock Coverage Purchase Report can create an Ordering Tool draft.

There is no automatic vendor/order schedule, background worker, email sender, accounting integration, vendor portal submission, or independent Current Orders/Order History/Order Payments page. The main Ordering Tool table combines all standard and emergency records, and payment fields are embedded in purchase-order detail.

Square remains authoritative for item identity, variation identity, SKU, catalog facts, sales, locations, and live inventory. Erupted Admin owns vendor-specific operating configuration, par/level decisions, drafts, purchase-order records, receiving state, payment status, PDF template text, generated file references, notes, and audit facts.

## Entry points and navigation

- Management dashboard card: `Erupted Ordering Tool` → `/management/ordering-tool`.
- The dashboard card is visible only to literal `ADMIN`/legacy `MANAGER` because its catalog entry uses `requires_admin`; a principal granted `management.admin` through an override can call the route directly but may not see the card.
- The Reports hub exposes Stock Coverage Purchase, which can create a draft and redirect to the Ordering Tool detail page.
- All V1 Ordering pages extend `app/templates/base.html`.
- V1 pages hard-code Home, Dashboard, Ordering Tool, and local subpage links. They do not accept or validate a `return_to` parameter for returning to `/v2/...`.
- V1 navigation and routes must remain unchanged during any V2 navigation-link milestone.

## Route inventory

All paths below are prefixed by `/management`. `AD` means effective `management.admin`; `RA` means literal `ADMIN`.

| Method and path | Behavior | Permission | Primary effects |
|---|---|---:|---|
| GET `/ordering-tool` | Vendor selection, generation controls, combined standard/emergency order table | AD | Reads vendors, POs, lines, emergency drafts |
| GET `/ordering-tool/emergency-editor` | Open/start emergency inventory draft | AD | Reads vendors, stores, mappings, live Square catalog/on-hand |
| POST `/ordering-tool/emergency-editor/start-draft` | Create emergency draft for vendor | AD | Writes emergency draft |
| POST `/ordering-tool/emergency-editor/{draft_id}/add-sku` | Add mapped SKU and seed live per-store quantities | AD | Reads Square; writes draft line JSON |
| POST `/ordering-tool/emergency-editor/{draft_id}/save` | Save per-store true-count quantities | AD | Writes draft line JSON and audit |
| POST `/ordering-tool/emergency-editor/{draft_id}/push` | Set exact on-hand in Square | AD | Writes Square sync events, Square inventory, draft status, audit |
| GET `/ordering-tool/mappings` | Vendor SKU mapping editor/filter | AD | Reads vendors, mappings, live catalog names |
| POST `/ordering-tool/mappings/upsert` | Add/update one mapping | AD | Writes mapping and audit |
| POST `/ordering-tool/mappings/import` | Import mapping CSV | AD | Upserts mappings and audit |
| POST `/ordering-tool/mappings/bulk-save` | Save displayed mapping rows | AD | Upserts mappings and audit |
| POST `/ordering-tool/mappings/auto-fill` | Fill missing Square variation IDs | AD | Reads Square catalog; writes mappings and audit |
| POST `/ordering-tool/vendors/sync` | Sync vendors then vendor/SKU assignments | AD | Reads Square; writes vendors/mappings and audit |
| GET `/ordering-tool/par-levels` | Vendor list for par/level management | AD | Reads active vendors |
| GET `/ordering-tool/par-levels/{vendor_id}` | Live store-level par/level matrix | AD | Reads Square sales/on-hand and local pars/mappings |
| POST `/ordering-tool/par-levels/{vendor_id}/save` | Save manual store level/par values | AD | Writes `par_levels` and audit |
| POST `/ordering-tool/par-levels/{vendor_id}/prefill` | Materialize best-guess manual values | AD | Reads Square; writes `par_levels` and audit |
| GET `/ordering-tool/pdf-templates` | Generic/vendor PDF template assignments | AD | Reads vendors/templates |
| POST `/ordering-tool/pdf-templates/save` | Create/update generic and selected vendor templates | AD | Writes templates and audit |
| POST `/ordering-tool/pdf-templates/{template_id}/edit` | Edit name/disclaimer | AD | Writes template and audit |
| POST `/ordering-tool/generate` | Generate recommendation-only vendor drafts | AD | Reads Square/local inputs; writes POs, lines, allocations, pars, audit |
| POST `/ordering-tool/generate-full-stock` | Generate drafts including zero-quantity active mappings | AD | Same, with all active vendor SKUs |
| GET `/ordering-tool/orders/{purchase_order_id}` | Editable detail, live 30-day sales/on-hand, receiving/payment state | AD | Reads PO graph, mappings, Square, sync events |
| GET `/ordering-tool/orders/{purchase_order_id}/pdf` | Download/regenerate PDF | AD | Reads PO/template/lines; may write file and `pdf_path` in session |
| POST `/ordering-tool/orders/{purchase_order_id}/save` | Save allocations/removals and optional received fields | AD | Writes PO lines/allocations and audit |
| POST `/ordering-tool/orders/{purchase_order_id}/invoice` | Save PAID/UNPAID metadata | AD | Writes PO payment fields and audit |
| POST `/ordering-tool/orders/{purchase_order_id}/add-line` | Add or restore mapped SKU | AD | Reads mapping/catalog; writes line/allocations and audit |
| POST `/ordering-tool/orders/{purchase_order_id}/refresh-lines` | Refresh draft labels, SKU, GTIN, price and cost | AD | Reads Square; overwrites draft snapshot fields and audits |
| POST `/ordering-tool/orders/{purchase_order_id}/submit` | Save and submit draft | AD | DRAFT→IN_TRANSIT, generates PDF, writes actor/timestamps/audit |
| POST `/ordering-tool/orders/{purchase_order_id}/received-quantities` | Save per-store received counts | AD | Writes allocations/line totals and audit |
| POST `/ordering-tool/orders/{purchase_order_id}/scan-barcode` | Add a receiving scan | AD | Writes allocation/line; JSON response; audit |
| POST `/ordering-tool/orders/{purchase_order_id}/scan-barcode/cancel` | Reverse a scan | AD | Writes allocation/line; JSON response; audit |
| POST `/ordering-tool/orders/{purchase_order_id}/receive` | Add received quantities to Square inventory | AD | Writes sync events/Square/PO status/audit |
| POST `/ordering-tool/orders/{purchase_order_id}/receive-retry-failed` | Retry only failed receive targets | AD | Reuses sync-event keys; writes Square/status/audit |
| POST `/ordering-tool/orders/{purchase_order_id}/delete` | Permanently discard DRAFT or IN_TRANSIT order | AD | Deletes PO graph and current PDF; audit remains |
| GET `/reports/stock-coverage-purchase` | Live coverage recommendations | RA | Reads Square and local mappings |
| GET `/reports/stock-coverage-purchase/export.csv` | Export current report | RA | In-memory CSV response |
| POST `/reports/stock-coverage-purchase/create-order` | Create editable vendor draft from visible report rows | RA | Writes PO graph and audit |

## Router, template, JavaScript, and CSS inventory

### Router/controller

- `app/routers/management.py`
  - owns all Ordering Tool and Stock Coverage handoff routes;
  - applies authentication, effective capability or literal-role checks, CSRF verification, audit logging, commits, redirects, and user-facing error conversion.

### Templates

- `management_ordering_tool.html` — generation controls and combined order history.
- `management_ordering_order_detail.html` — draft edit, live reference data, payment, receiving, barcode modal, failure display.
- `management_ordering_mappings.html` — single, bulk, CSV, and Square-assisted mapping management.
- `management_ordering_par_levels.html` — vendor landing.
- `management_ordering_par_levels_vendor.html` — store-by-SKU level/par matrix.
- `management_ordering_pdf_templates.html` — generic/vendor template assignment and editing.
- `management_ordering_emergency_editor.html` — emergency true-on-hand draft and Square push.
- `management_stock_coverage_purchase.html` — related report and create-order handoff.
- `base.html` — V1 shell, common inline CSS, CSRF-aware logout, and autosave code. Ordering forms do not opt into `data-autosave`.

### JavaScript and CSS

There are no Ordering-specific static files.

- Ordering detail uses inline CSS and JavaScript for:
  - horizontal table containment;
  - client-side line sorting;
  - allocation-derived order quantities and extended cost;
  - invoice-field visibility;
  - received totals/discrepancies;
  - barcode modal, scan fetch, cancellation fetch, and HTML-escaped results.
- Mapping management uses inline JavaScript for active-row sorting.
- Other Ordering pages use shared `base.html` CSS plus inline layout styles.
- The V2 CSS/JavaScript files are not dependencies of V1 Ordering.

## Service inventory

| Service | Responsibility |
|---|---|
| `ordering_service.py` | Lazy singleton defaults and vendor-specific math overrides |
| `purchase_order_math_service.py` | Sales average, confidence, level/par, MOQ, pack rounding, recommended quantity |
| `purchase_order_generation_service.py` | Vendor/store/SKU iteration, open-in-transit lookup, par precedence |
| `purchase_order_admin_service.py` | Configuration, generation persistence, PO editing, invoice, receiving, PDF, mappings |
| `square_ordering_data_service.py` | Square catalog/vendor assignment, on-hand, sales, and snapshot reads |
| `square_vendor_service.py` | Square vendor sync and soft deactivation |
| `ordering_emergency_service.py` | Emergency draft, live seed, exact inventory push |
| `inventory_velocity_report_service.py` | Stock coverage report and purchase suggestions |
| `audit_service.py` | Free-form operational audit events |
| `access_control_service.py` | Effective permission precedence and V2 navigation permissions |
| `dashboard_layout_service.py` | V1 dashboard card visibility and layout |

## End-to-end standard workflow

1. An authorized user opens the management dashboard and selects Erupted Ordering Tool.
2. The user may manually sync Square vendors. Vendor sync also synchronizes Square vendor assignments into local `vendor_sku_configs`.
3. The user may edit mappings, costs, pack sizes, minimum quantities, default-vendor choice, and active state; or import CSV.
4. The user may open a vendor par matrix. It reads live Square sales and on-hand, calculates living suggestions, and permits manual store-specific level/par values.
5. On the main page, the user selects one or more vendors and reorder/stock-up/history windows.
6. Generation synchronizes selected vendors’ mappings, builds a live Square snapshot, calculates each active store/SKU recommendation, subtracts open in-transit allocations, applies manual par/level precedence, MOQ, and pack rounding, and creates one DRAFT PO per vendor.
7. The user opens a draft. The page attempts live 30-day sales and on-hand reads; failures there are suppressed and displayed as empty/zero reference values.
8. The user edits store allocations. Total order quantity is the sum of store allocations. The user can remove, restore, or add mapped SKUs and refresh DRAFT catalog fields.
9. Submit first saves the current allocations, removes zero unreceived lines, requires at least one positive line, changes the PO to IN_TRANSIT, records submitter/timestamps, and creates a local PDF.
10. The application does not send the PDF or submit to a vendor. The user downloads it and handles vendor delivery outside the application.
11. While IN_TRANSIT, the user may continue editing lines/allocations, record invoice payment state, and enter or scan received quantities.
12. Barcode scans match line SKU, variation ID, line GTIN, or mapping GTIN. A mapped pack scan increments received units by pack size. Unexpected barcodes create an overage line.
13. “Send Received Qty To Stores” issues Square inventory ADJUSTMENT writes for each line/store with a positive received quantity. Successful targets are skipped on later calls; failed targets can be retried.
14. If every attempted target succeeds, the PO becomes SENT_TO_STORES. There is no implemented transition to COMPLETED or a separate closed/history archive.

## Alternative workflows

### Full-stock generation

Uses the same recommendation math but includes active non-default mappings and zero-quantity lines, allowing a user to build a broader order manually.

### Stock Coverage Purchase Report

Uses current Square catalog, sales, inventory, inventory-change history, and local vendor costs to create vendor suggestions. The user selects one vendor and creates a DRAFT PO with report-derived store splits. Catalog failure during draft creation is tolerated; local mapping and report labels become fallback metadata.

### Emergency on-hand

The user selects a vendor, adds mapped SKUs, seeds current per-store Square on-hand into JSON, edits exact quantities, and pushes PHYSICAL_COUNT changes. The draft becomes PUSHED only if all targets succeed.

## Reports and exports

- Stock Coverage Purchase HTML and CSV are the direct report-to-order entry point.
- Inventory Velocity, Targeted SKU Demand, Stock Value, COGS, and Sales by Vendor reuse the same Square/catalog/mapping data boundaries but are separate reporting workflows.
- Vendor mapping CSV is input only; no mapping export exists.
- Purchase-order PDF is the only direct PO export.
- No PO CSV/XLSX, vendor portal format, EDI, accounting export, or email attachment workflow exists.

## PDF and output behavior

- ReportLab generates US Letter PDFs under `generated/purchase_orders/`.
- Filename on disk: `purchase_order_{id}_{UTC timestamp}.pdf`.
- Download filename: `purchase-order-{id}.pdf`.
- Vendor-specific active template wins; otherwise active generic template; otherwise built-in company header with no disclaimer.
- The configurable template currently changes only the stored name and legal disclaimer; the name does not select a different layout.
- Lines are sorted by item/variation. Quantity shows individual units and an approximate pack count when pack size exceeds one.
- PDF is generated on submit and regenerated when missing or when PO `updated_at` is later than `submitted_at`.
- The exact originally sent artifact is not versioned or immutable. IN_TRANSIT edits can make the next download different.
- Old generated files are not systematically removed when a new PDF replaces them.
- Local-instance files have no object storage, shared-volume contract, backup, or archival job.
- `vendor_contacts` and PO email fields exist, but no email sender uses them.

## Session, authorization, error, audit, and cleanup behavior

- Standard authenticated `web_sessions` middleware is required; Ordering has no Current Store dependency.
- POST routes require the existing CSRF cookie/form token.
- Square calls are synchronous and may hold a request open up to configured timeout.
- API/network errors normally abort generation/sync and redirect or return a 400. Detail-page live on-hand/sales failures are silently converted to empty data.
- `audit_log` receives 25 Ordering action names, but it does not preserve before/after row values or a formal transition history.
- `square_sync_events` preserves request, response/error, attempts, and idempotency for inventory writes.
- DRAFT and IN_TRANSIT POs can be hard-deleted with cascading lines/allocations/receipts; the current referenced PDF is best-effort deleted.
- No automatic draft expiration, archive, PDF cleanup, session cleanup, audit retention, Square reconciliation, or failed-sync worker exists.

## Current V2 navigation mapping

Four V2 Inventory children now form a local-only, default-disabled navigation bridge under `ordering_v1_links_v2`. The bridge changes no V1 behavior or Ordering ownership.

| V2 child | Existing V1 destination | Initial planning decision |
|---|---|---|
| Ordering Tool | `/management/ordering-tool` | Implemented locally as a default-disabled V1 link |
| Par / Level Manager | `/management/ordering-tool/par-levels` | Implemented locally as a default-disabled V1 link |
| Vendor SKU Mappings | `/management/ordering-tool/mappings` | Implemented locally as a default-disabled V1 link |
| PDF Templates | `/management/ordering-tool/pdf-templates` | Implemented locally as a default-disabled V1 link |
| Current Orders | No dedicated route/filter | Remain unavailable; do not mislabel combined history |
| Order History | Main page contains an unfiltered history table but no dedicated deep link | Remain unavailable until a truthful destination exists |
| Order Payments | Payment is embedded in order detail; no list route | Remain unavailable |

The existing V1 routes do not support a server-validated return to `/v2`. The bridge relies on ordinary same-application navigation and does not change V1 pages. Returning to any `/v2/...` page restores the V2 shell.

## Evidence and test coverage

Direct tests cover:

- default/override validation and recommendation math;
- manual zero pars and store-specific par precedence;
- include-zero/full-stock recommendation behavior;
- default-vendor mapping conflicts;
- sales aggregation;
- receiving store priority, barcode/GTIN matching, pack increments, zero/removal behavior;
- stock-coverage draft creation.

Major route, PDF byte/layout, end-to-end Square failure, concurrency, audit completeness, invoice lifecycle, emergency retry, and filesystem retention behaviors lack comprehensive automated coverage.
