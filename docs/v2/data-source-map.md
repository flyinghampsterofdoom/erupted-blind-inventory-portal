# V2 definitive data-source map

Status date: 2026-08-03
Scope: repository-backed current architecture; no deployment or canonical-owner transfer is authorized by
this document.

## How canonical status is established

A model or table name is not evidence of authority. A source is marked canonical here only when the
repository has an active writer and an active business read path, or when an external API is explicitly
treated as authoritative and the local persistence contract is visible. The audit used `app/models.py`,
active routers and services, migrations, tests, and the existing V1 discovery records. The configured
Render hostname is not resolvable from this workstation, so production row presence is **unknown** unless
the repository itself proves persistence. No live data was queried and no deployment was performed.

Source classifications used below:

- **External authoritative fact**: a fact owned by Square or another named external system.
- **Internal authoritative fact**: an Erupted Admin fact with an active internal writer and reader.
- **Internal enrichment**: local policy or metadata attached to an externally identified object.
- **Immutable historical snapshot**: a frozen local copy used to reproduce a past economic or operational event.
- **Derived calculation**: reproducible output whose inputs, formula, and missing-data rule must be named.
- **Explicit owner override**: an audited correction that never masquerades as source data.
- **Legacy or unused structure**: present in schema/code but not canonical for new V2 behavior.
- **Unknown or unresolved source**: repository evidence is insufficient or competing active paths exist.

## Source registry

| Domain | Fact | Class | Authority | Exact source | Canonical writer | Read path | Local snapshot | Write-back | Missing behavior | Status |
|---|---|---|---|---|---|---|---|---|---|---|
| Catalog | Square item and variation IDs | External authoritative fact | Square Catalog | Catalog item/variation `id`; synchronized to `ordering_catalog_identity.square_item_id` and `.square_variation_id` | Square; local sync `refresh_ordering_catalog_identity` through `SquareOrderingReadGateway` | Ordering lifecycle repository; consignment fact import | Synchronized current identity; sale/PO facts freeze IDs separately | Never to Square | Missing mapped identity remains uncovered/unknown; no invented ID | Verified |
| Catalog | Base item/variation name and SKU | External authoritative fact | Square Catalog | Catalog `item_data.name`, variation name/SKU; `ordering_catalog_identity` | Same catalog refresh | Lifecycle UI; fact importer enrichment | Current synchronized identity; historical PO/sale/report rows freeze names/SKU | Never to Square | Refresh retains prior last-known nonempty field; new missing identity is labeled unavailable, not canonicalized from another system | Verified with documented last-known behavior |
| Catalog | GTIN/barcode | External fact with transitional local copy | Square Catalog plus internal vendor mapping | Square variation `upc`; `vendor_sku_configs.gtin`; `purchase_order_lines.gtin` | Legacy `sync_vendor_sku_configs_from_square`, owner CSV/upsert, PO save/refresh | V1 receiving barcode scan | PO line freezes GTIN; V2 `ordering_catalog_identity` does not store it | No Square write | Barcode matching may use saved PO GTIN or active vendor mapping; no V2 catalog GTIN exists | Transitional |
| Catalog | Square category/reporting category | External authoritative fact | Square Catalog | Live catalog category data in count/report services | Square; `sync_campaigns` for count campaigns | Count-group audit and legacy reports | No general V2 catalog-category snapshot | No | Feature must report unavailable; may not infer from product text | Partially implemented |
| Catalog | Catalog deletion/presence | External authoritative fact | Square Catalog | `ordering_catalog_identity.square_is_deleted`, `last_seen_at`, refresh coverage state | Catalog refresh | Ordering lifecycle workspace | Synchronized status/evidence | No | Missing coverage is PARTIAL/FAILED; Square deletion never archives internally | Verified |
| Locations | Internal store to Square location mapping | Internal authoritative mapping | Erupted Admin | `stores.id`, `stores.square_location_id` | Existing store administration/configuration | Ordering gateway, sales facts, reports, touchscreen | Store/location ID copied into economic and inventory facts where required | No automatic Square write | Missing or duplicate mapping blocks/marks source incomplete | Verified |
| Catalog enrichment | Preferred/default vendor and vendor SKU mapping | Internal enrichment with transitional Square sync | Erupted Admin | `vendors`, `vendor_sku_configs`, especially `is_default_vendor`, `square_variation_id`, SKU | Owner upsert/CSV in `purchase_order_admin_service`; legacy `sync_vendor_sku_configs_from_square` also writes | V1/V2 ordering readers and report mappings | PO and immutable sale facts snapshot selected vendor/cost separately | Internal tables only | Missing mapping excludes or blocks ordering; no product-name inference | Active, dual writer documented |
| Vendors | Vendor identity and availability | External authoritative fact | Square Vendor API | `POST /v2/vendors/search`; Square vendor `id` | `sync_vendors_from_square` writes `vendors.square_vendor_id`, name, active state, and synchronization time | V1 ordering and V2 correction selectors | `vendors`; `square_vendor_id` is required and unique | No Square vendor creation from V2 | Missing/inactive blocks with “Vendor not available” and directs the owner to Square | Verified vendor-agnostic boundary; functional readiness requires two distinct eligible registry records, not any named vendor |
| Order Payments | Original PO vendor | Immutable historical source fact | V1 purchase order | `purchase_orders.vendor_id` | Existing V1 PO writers only | V2 detail and assignment audit | Vendor identity is also frozen on each correction | Never changed by V2 | Missing source vendor blocks correction | Verified read-only boundary |
| Order Payments | Current financial vendor and override | Internal assignment / explicit owner override | V2 | `order_payments.vendor_id`; `vendor_assignment_operations`; `vendor_assignment_changes` | Initialization defaults from the PO; owner-only preview/confirm correction | Payment detail, event writers, audit history | Source/prior/new vendor IDs, names, Square IDs, states, impact, actor, reason, bulk operation and transfer IDs | V2 only | Only active Square-synchronized vendors are accepted; unavailable vendor blocks the operation | Append-only correction audit; V1 vendor unchanged |
| Order Payments | Manual payment and reversal | Append-only internal financial fact | V2 | `order_manual_payment_entries` | `record_manual_order_payment`, `reverse_manual_order_payment` | `order_financial_position`, detail history | Payment, reversal, replacement rows | V2 only | Ordinary invoices only; method, amount, date and reason validated | Multiple/partial payments and visible overpayment |
| Order Payments | Amount adjustment | Explicit owner override | V2 | `order_balance_adjustments` | Create/reverse/replace services | `order_financial_position`, detail history | Original, prior, resulting amounts and correction links | V2 only | PO lines, product cost, inventory and source facts never change | Append-only charge/credit history |
| Consignment | Vendor assignment transfer | Append-only internal financial fact | V2 | `consignment_ledger_entries` typed transfer out/in | `confirm_vendor_reassignment` | Balance and ledger history | Equal linked transfer IDs on the assignment change | V2 only | Posted source events remain untouched | Signed global net zero; future order receipt entries use the current financial vendor |
| Catalog enrichment | Product lifecycle/archive/NFR | Internal authoritative fact | Erupted Admin | `ordering_product_lifecycle` | `transition_lifecycle` via V2 Ordering routes | `v2_ordering_data_coordinator`, lifecycle repository, recommendation service | Lifecycle row and audits preserve transition evidence | Internal only; never Square | Absent lifecycle row means ACTIVE policy; Square deletion is only external evidence | Verified |
| Catalog enrichment | Ordering eligibility | Derived calculation | Erupted Admin policy | Active default vendor mapping + lifecycle + required Square evidence + recommendation blockers | No direct writer; calculated by coordinator/recommendation service | V2 Ordering dashboard | Inputs/evidence shown; not a historical master flag | No | Required-source failure blocks actionability; no trusted zero substitution | Verified derived fact |
| Catalog enrichment | Effective-dated consignment/vendor attribution | Internal authoritative fact | Erupted Admin | `vendor_variation_assignments` | `create_assignment`; owner attribution routes | `attribution_at`, immutable fact sync | Selected vendor/consignment state frozen on each fact | Internal only | Missing/overlap becomes blocker; no current mapping fallback | Verified |
| Catalog enrichment | Effective-dated consignment cost | Internal authoritative fact | Erupted Admin | `vendor_variation_costs` | `create_cost`; owner attribution routes | `attribution_at` | Unit cost and extended COGS frozen on sale/return/report facts | Internal only | Missing or ambiguous cost blocks attribution/finalization | Verified |
| Flavor Finder | Flavor metadata, tags, visibility, recommendations | Internal authoritative fact | Erupted Admin | `touchscreen_flavors`, category/link, SKU-link, store-override, recommendation, and media tables | Touchscreen management/media services and V2 routes | Touchscreen catalog/detail services | Internal persisted configuration | Internal only | Unmapped/unpublished/stale cache is hidden or explicitly unavailable | Verified |
| Digital signage eligibility | Product-linked signage eligibility | Unknown or unresolved source | None implemented | No product-to-signage eligibility model exists | None | None | None | None | Must not be claimed or inferred from Square | Unsupported |
| Customer sales | Completed order and sale-line economics | External authoritative fact | Square Orders | `/v2/orders/search`; order ID + line UID, quantity, timestamp, location, discounts/tax/net/tender reference | Square; `synchronize_square_facts`/`import_square_orders` persist the COGS subset | V2 COGS report service reads `consignment_sale_facts`; other legacy reports read Square live | `consignment_sale_facts` is immutable economic history for consignment COGS | Never to Square | Incomplete identity/location/vendor/cost remains blocked | Verified locally; external reconciliation pending |
| Customer returns | Itemized return identity and quantity | External authoritative fact | Square Orders/Returns | Return order ID + return UID + return-line UID and original order/line IDs | Square fact sync | V2 COGS report service | `consignment_return_facts` copies original sale vendor/cost/name snapshots | Never to Square | Unmatched, over-return, and unitemized refund remain blocked | Verified locally; external reconciliation pending |
| Customer refunds | Unitemized refund amount | External authoritative fact | Square refund embedded in order source | Square refund ID/amount | Square fact sync | Attribution queue only until resolved/excluded | Immutable unresolved return fact | Never to Square | Never fabricates item, vendor, quantity, or COGS | Verified locally |
| Employee sales attribution | Payment/team-member attribution | External authoritative fact | Square Payments and Team Members | `/v2/payments`, `/v2/team-members/search` | Square | `build_employee_sales_report` | None; live report only | No | Unattributed bucket; never mapped to internal schedule automatically | Verified live boundary |
| Inventory | Current count per variation/location | External authoritative fact | Square Inventory | `/v2/inventory/counts/batch-retrieve` | Square; `refresh_ordering_current_inventory` | Ordering inventory/lifecycle readers | `ordering_current_inventory` synchronized current read model plus refresh-run evidence | Never to Square from V2 read model | Omitted pair is not synthesized as zero; coverage becomes PARTIAL/FAILED/UNKNOWN | Verified |
| Inventory | Fresh/stale/critical/unknown and negative warning | Derived calculation | Erupted Admin | Refresh timestamps, expected scope, `effective_freshness`, lifecycle repository | Derived at read; refresh writes evidence | V2 Ordering | Refresh run and observation timestamps persisted | No | Nonfresh evidence blocks trusted aggregate; signed negative remains visible | Verified |
| Inventory | Historical COGS-report inventory | Immutable historical snapshot | V2 | `consignment_inventory_snapshots` from `ordering_current_inventory` | `generate_report` | Final report/email detail | Quantity, cost, value, name/SKU, store, retrieval time frozen | No | Ambiguity/staleness becomes warning; current inventory never infers sales | Implemented, external validation pending |
| Ordering | Purchase-order identity/vendor/date/lifecycle | Internal authoritative fact | V1 | `purchase_orders`, `vendors` | `generate_purchase_orders`, `create_purchase_order_from_stock_coverage_rows`, `submit_purchase_order`, receiving routes | `purchase_order_admin_service`; V2 Order Payments service/router | V2 payment links by PO ID but does not replace V1 | V2: no write-back | Only placed statuses qualify; missing PO is 404 | Verified |
| Ordering | Saved product/SKU/ordered quantity/historical line cost | Immutable V1 order snapshot | V1 | `purchase_order_lines` | V1 PO generation/edit/catalog-refresh before/within allowed lifecycle | V1 detail and V2 payment detail | Saved line itself is historical source; V2 payment freezes total/completeness | V2: no write-back | Missing cost marks snapshot incomplete and now blocks PAID/receipt valuation | Verified |
| Ordering | Store scope | Internal authoritative fact | V1 | `purchase_order_store_allocations` joined to `stores` | V1 PO generation/edit services | V1 detail; V2 `purchase_order_scope_labels` | Receipt lineage freezes exact allocation/store | V2: no write-back | No allocation means organization-wide display only; it cannot prove a received store | Verified |
| Ordering | Extended cost and order total | Derived calculation from authoritative V1 snapshot | V1 | `ordered_qty × purchase_order_lines.unit_cost`; sum of active lines | Derived by V1/V2 services | PO detail and `_order_cost_snapshot` | `order_payments.order_amount` plus completeness flag | No V1 write | Missing positive-quantity cost marks incomplete; payment cannot be marked paid | Verified |
| Ordering | Missing quantity and receipt state | Derived calculation | V1 | `max(ordered_qty - received_qty_total, 0)` and received-vs-ordered comparison | Derived | V1/V2 detail | Receipt deltas frozen by V2 for consignment | No | Reconciliation failure blocks settlement | Verified |
| Ordering | Partial-order relationship/follow-up PO | Unknown or unresolved source | V1 schema has no active relationship | No parent/follow-up PO key or active creation path | None | None | None | No | Must not infer from status or vendor/SKU similarity | Unsupported |
| Receiving | Store received quantity | Internal authoritative fact | V1 | `purchase_order_store_allocations.id`, `.store_received_qty` | `save_purchase_order_received_quantities`, `scan_purchase_order_barcode`, `cancel_purchase_order_barcode_scan`; totals maintained by `_sync_line_received_totals` | V1 receiving; V2 `sync_consignment_replenishment` | V2 receipt-line lineage freezes source ID and positive delta | V2: no write-back | Missing allocation or mismatch to line aggregate blocks settlement | Verified |
| Receiving | Aggregate received quantity | Internal authoritative reconciliation | V1 | `purchase_order_lines.received_qty_total` | `_sync_line_received_totals` invoked by receiving writers | V1 detail; V2 reconciliation | Not used alone as receipt proof | V2: no write-back | Never substitutes for missing store allocation | Verified after audit correction |
| Receiving | Inventory push after receipt | External write side effect from V1 | V1 to Square Inventory | `square_sync_events`; `/v2/inventory/changes/batch-create` | `receive_purchase_order` | V1 retry/status UI | Idempotency/event evidence | Authorized V1 write to Square | Failure remains retryable; does not fabricate success | Active V1 behavior |
| Payments | Saved methods and vendor default | Internal authoritative fact | V2 | `payment_methods`, `vendor_payment_settings` | V2 owner mutations | V2 Order Payments/Consignment | Method/category/terms label copied to order payment | V2 only | Missing default leaves ordinary order UNPAID with no method; PAID requires method | Verified |
| Payments | Ordinary paid/unpaid, date, due date, event history | Internal authoritative fact | V2 | `order_payments`, `order_payment_events` | `ensure_order_payment`, `update_order_payment` | V2 payment list/detail | Initialization and every transition recorded | V2 only; never V1 | Existing order defaults UNPAID exactly once; incomplete V1 costs block PAID | Verified |
| Payments | V1 invoice fields | Legacy parallel operational fact | V1 | `purchase_orders.invoice_*` | `save_purchase_order_invoice` and V1 management routes | V1 PO UI only | V1 row | V1 only | May diverge from V2; V2 never imports or writes it | Active legacy ambiguity |
| Consignment settlement | Financial treatment/replenishment identity | Internal authoritative fact | V2 linked to V1 | `order_payments`, `consignment_replenishments` | `initialize_new_order_if_configured` from the vendor classification at the first canonical V1 receipt, or explicit historical backfill | V2 payment and consignment pages | Method/treatment/order value frozen | V2 only | Placement alone creates no consignment record; an order discarded before receipt never enters consignment; missing costs block received valuation | Verified |
| Consignment settlement | Receipt value, allocations, excess credit | Immutable historical snapshot plus derived allocation | V2 from V1 receipts | Receipt, receipt-line, receipt-allocation, replenishment, allocation, ledger tables | `sync_consignment_replenishment` | V2 payment detail and ledger/report views | Exact PO/line/allocation/store/cost/report/ledger lineage | No V1 write | Only positive reconciled allocation deltas settle; excess becomes credit | Verified |
| Consignment settlement | Cash settlement and typed adjustment | Explicit owner override | V2 | `consignment_ledger_entries` typed entries | Owner-only settlement/adjustment services where implemented | Ledger/balance/report components | Immutable typed entry and audit | V2 only | Requires type-specific validation/reason; no normal cash assumption | Cash settlement verified; generic adjustment UI coverage varies |
| COGS | Vendor attribution and historical unit cost | Internal authoritative fact → immutable snapshot | V2 | Effective-dated assignment/cost tables copied to sale facts | Owner history writers; fact sync selection at transaction time | Report generation reads fact snapshots | Frozen per fact and link | No source rewrite after finalization | Missing/ambiguous source blocks finalization; audited transaction override allowed pre-finalization | Verified locally |
| COGS | Ledger balance | Derived calculation | V2 | Typed `consignment_ledger_entries` | Finalization, void, receipt allocation, cash/credit writers | `consignment_balance` and report formulas | Report component snapshots preserve period evidence | V2 only | Never negative; credit remains separate | Verified locally |
| COGS | Report lines, fact links, finalization, void | Immutable historical snapshot/internal fact | V2 | `consignment_reports`, lines, fact links, ledger unique entries | `generate_report`, `finalize_report`, `void_report` | V2 report UI/email capture | Frozen lines, links, component totals, original+reversal retained | V2 only | Any blocker prevents finalization; duplicate finalization/void constrained | Verified locally; external sign-off pending |
| COGS | Vendor report recipient | Internal authoritative fact | V2 | `vendor_payment_settings.report_email` | V2 vendor settings owner mutation | `capture_test_email` | Delivery row freezes recipient/subject/body | No external delivery currently | Missing recipient blocks capture | Verified local capture only |
| Employees | Authentication identity, role, store assignment | Internal authoritative fact | Erupted Admin | `principals` | Existing access-control administration | Authentication, authorization, audit, daily log identity | Audit/submission rows reference principal | Internal only | Missing/inactive principal denies access | Verified |
| Employees | General internal employee identity | Internal authoritative fact | Erupted Admin | `employees`, optional `principal_id` | `employee_log_service` admin functions | Employee logs and scheduling | Log/shift rows reference employee | Internal only | No inference from Square sales/team member | Verified |
| Employees | Scheduling eligibility/preferences/time off | Internal authoritative fact | Erupted Admin | Employee scheduling profile/window/preference and time-off tables | V2 scheduling rules services | Board validation and coverage | Effective records persisted | Internal only | Missing/inactive employee blocks assignment; warnings are explicit | Verified |
| Employees | Daily-log identity | Internal authoritative fact | Erupted Admin | `daily_store_logs.submitted_by_principal_id` | `submit_daily_log` | Own receipt/history/management detail | Immutable submitter and timestamps | Internal only | Never substitutes employee text or Square team member | Verified |
| Employees | Cash attribution | Unknown/not implemented as employee master link | Square operational cash APIs | Cash drawer/payment/refund data lacks an internal employee canonical link in current service | Square | Cash reconciliation only | Aggregate expected/actual evidence | No employee write | Must not infer schedule or internal employee from cash activity | Unsupported link |
| Scheduling | Weeks, shifts, breaks, open shifts, draft/published state | Internal authoritative fact | Erupted Admin | `schedule_periods`, `schedule_shifts`, `schedule_shift_types` | `v2_scheduling_service` | Scheduling board/service/router | Revision/status/audit persisted | Internal only | Validation errors block mutation; no Square fallback | Verified |
| Scheduling | Templates and store-shift definitions | Internal authoritative fact | Erupted Admin | `schedule_templates`, `schedule_template_shifts`, `shift_templates`, `store_shifts` | Template and store-shift services | Schedule creation/placement UI | Source template/store-shift IDs retained | Internal only | Missing definition does not invoke Square | Verified |
| Scheduling | Coverage, time-off effects, warnings | Derived from internal authoritative facts | Erupted Admin | Coverage requirements, operating/special hours, time off, profiles, shifts, warnings | Rules services; coverage evaluator | Scheduling board | Warnings persisted per evaluation | Internal only | Missing rules do not import employee sales activity | Verified |
| Digital signage | Displays, credentials, media, groups, windows, priority, permanent items, publication | Internal authoritative fact | Erupted Admin | `digital_signage_*` tables and configured media storage | Digital signage and media services/routes | Management and display playlist routes | Database config plus immutable content hash/storage object | Internal/storage only | Missing/archived/disabled content is excluded; no Square substitution | Verified |
| Daily Store Logs | Store/date/content/submission lock/status/review | Internal authoritative fact | Erupted Admin | `daily_store_logs`, `daily_store_log_actions` | `submit_daily_log`, `perform_management_action` | Receipt/history/detail/completion dashboard | Submission fingerprint, submitter, state transitions retained | Internal only | Duplicate/invalid submissions conflict; no Square fallback | Verified |
| Reporting | Sales transactions/gross by store/vendor/employee | External live fact plus internal mapping enrichment | Square | Orders, Payments, Team Members, Locations; `VendorSkuConfig` for vendor grouping | Square; report services are readers | `sales_transactions_report_service` | No durable report snapshot | No | Unattributed/unmapped rows remain explicit | Active live reports |
| Reporting | Inventory velocity/stock coverage/targeted demand | Derived calculation | Square + V1/V2 enrichment | Square Orders/Inventory/changes/catalog + stores/vendor config/PO supply | External APIs and internal config writers | Named report services | Usually none; generated response/export | No | Missing source produces unavailable/warning behavior per report; not a universal Square fallback | Active operational reports |
| Reporting | Legacy COGS by category | Legacy or unsafe for historical accounting | Live Square + mutable current internal cost | `cogs_report_service.build_cogs_report` and `VendorSkuConfig.unit_cost` | Square/current config | Legacy management report | None | No | Missing cost becomes zero and current cost is applied historically; never canonical for V2 consignment COGS | Active legacy; deprecate later |
| Reporting | Cash reconciliation | External facts plus internal authoritative actual/verification | Square + Erupted Admin | Square Payments/Refunds/Cash Drawers; local cash actual and verification tables | Square and cash reconciliation service | Cash reconciliation UI/reports | Expected aggregates and internal verification history | Internal actuals only | Missing Square/store mapping errors explicitly | Active |

## Domain contracts and historical requirements

### Square catalog and internal enrichment

`SquareOrderingReadGateway` is the narrow V2 read boundary for catalog identity and inventory. The
`refresh_ordering_catalog_identity` writer synchronizes only IDs, names, SKU, deletion evidence, and source
timestamps into `ordering_catalog_identity`. It deliberately does not mutate lifecycle or touchscreen
records. A failed refresh retains prior synchronized rows and records FAILED/PARTIAL coverage; this is
last-known cache behavior, not a claim that old fields are current.

Internal lifecycle is independently owned by `ordering_product_lifecycle`. `NO_FUTURE_REORDER` blocks a
recommendation, and `ARCHIVED` removes a variation from the ordering dashboard. Square deletion is shown
as external evidence and never triggers an internal lifecycle transition. Preferred vendor, ordering SKU,
pack/minimum, and operational current cost live in `vendor_sku_configs`; the table has both owner-driven
and legacy Square-vendor-sync writers, so its provenance must be inspected before treating a row as an
owner decision.

Unsupported today: a general V2 Square category snapshot, catalog GTIN in `ordering_catalog_identity`,
catalog location-availability objects, arbitrary internal tags/categories/notes beyond the named lifecycle
and touchscreen models, and product-level digital-signage eligibility.

### Square sales, returns, and inventory

Most general sales reports remain live Square reads. Consignment accounting is different: Square source
objects are persisted once into immutable `consignment_sale_facts` and `consignment_return_facts` keyed by
source IDs. Vendor and cost are selected from effective-dated internal history at transaction time and
copied into the fact. Itemized returns copy the original sale snapshot; an unmatched or unitemized refund
cannot borrow today's mapping.

`ordering_current_inventory` is a synchronized current read model, not sales history. Missing Square pairs
are never stored as zero. Freshness and completeness are internal derived states. A COGS report copies the
current observations into `consignment_inventory_snapshots`; those inventory facts stay separate from
period sale facts and cannot be used to infer units sold.

### V1 ordering and receiving

The active order source is `purchase_orders` → `purchase_order_lines` →
`purchase_order_store_allocations`, with `vendors` and `stores` providing identity. V1 directly persists no
single order-total column; total and extended costs derive from the saved line cost snapshot. Store scope
also exists at allocation level, not as an order-level scope field.

Active creation/edit/submission writers include `generate_purchase_orders`,
`create_purchase_order_from_stock_coverage_rows`, `save_purchase_order_lines`,
`refresh_purchase_order_lines_from_catalog`, and `submit_purchase_order`. Active receiving writers are
`save_purchase_order_received_quantities`, `scan_purchase_order_barcode`,
`cancel_purchase_order_barcode_scan`, `_sync_line_received_totals`, and `receive_purchase_order`.

Canonical receipt proof is the exact allocation row and its cumulative `store_received_qty`.
`received_qty_total` is the reconciled line aggregate. It is not an independent substitute for missing
allocation rows. Missing quantity and receipt status are derived; a later/follow-up partial PO relationship
is not currently modeled and must not be inferred.

### V2 payments and consignment accounting

V2 payment and settlement tables reference V1 purchase-order IDs and never invoke V1 writers. Ordinary
orders default UNPAID exactly once. Payment method/category/label/Terms and order value are snapshotted.
An incomplete cost snapshot remains visible and cannot be marked PAID.

Consignment receipt synchronization requires V1 allocation rows and exact agreement between their sum and
the V1 line aggregate. Only positive deltas at the saved PO-line cost create immutable receipt and ledger
lineage. Ordered-but-unreceived quantities never settle. Square customer orders appear only in the COGS
fact pipeline, never in the payment list/detail or replenishment identity.

Vendor correction choices come only from the Square Vendor API synchronization registry. V2 has no
free-text or vendor-creation path. `purchase_orders.vendor_id` remains the historical source vendor;
`order_payments.vendor_id` is the current financial vendor and initially defaults to it. Both render when
they differ. A single or bulk correction freezes its preview impact and identity snapshots. Posted ordinary
payment effects move through reversal/replacement events, and posted consignment effects move through equal
transfer-out/transfer-in entries; original event rows remain intact.

For ordinary invoices, current amount owed is the original order snapshot plus signed active adjustments.
Remaining amount subtracts active payment and replacement events and adds reversals. Derived status can be
unpaid, partially paid, paid, or overpaid. Overpayment is preserved and shown, never clamped. Consignment
cash settlement stays a separately typed exceptional action.

### Employees and scheduling

`principals` is the login/authorization identity. `employees` is the internal employee identity used by
logs and scheduling and may link one-to-one to a principal. Square team members are fetched only for the
live Employee Sales report and have no automatic link to either table. Sales or cash activity therefore
cannot establish scheduling eligibility, store assignment, role, or daily-log identity.

All scheduling facts are internal. Square supplies no schedule, time-off, template, coverage, or warning
state. Warnings are derived only from internal shifts, profiles, availability, time off, operating hours,
special hours, coverage requirements, and compensation rules.

### Digital signage and Daily Store Logs

Digital signage operation is entirely internal: displays, device sessions, media metadata/storage keys,
groups, display membership, items, timing, priority, permanent-content rules, enablement, and playlist
selection. The generic `campaigns` table is a Square-synchronized inventory-count concept and is unrelated
to digital signage campaigns.

Daily Store Logs are local submissions keyed by internal store and business date. Submitter, confirmation,
fingerprint, contents, lifecycle, follow-up, and management actions are persisted internally. Current code
does not mix Square sales/cash into log completion.

## Misleading, legacy, dormant, or parallel structures

| Name | Intended/implied purpose | Active writes | Active reads | Data exists | Canonical? | Disposition |
|---|---|---|---|---|---|---|
| `purchase_order_receipts` / `PurchaseOrderReceipt` | Receipt header/status | None found | None found | Unknown in production | No | Retain dormant; inspect production before later deprecation/migration |
| `purchase_order_receipt_lines` / `PurchaseOrderReceiptLine` | Receipt line facts | None found | None found | Unknown in production | No | Retain dormant; do not use as receipt proof |
| `PurchaseOrderStatus.RECEIVED_SPLIT_PENDING`, `COMPLETED`, `CANCELLED` | Additional lifecycle states | No active transition found | Some list/filter code accepts them | Unknown/manual data possible | Declared but not active lifecycle | Retain compatibility; never infer behavior from name |
| `purchase_orders.invoice_*` | V1 invoice/payment state | Active V1 `save_purchase_order_invoice` | Active V1 PO UI | Expected schema; row population unknown here | Canonical only to V1 UI, not V2 | Keep isolated; document possible divergence during canary |
| `cogs_report_service` | COGS report | Active live reader only | Active legacy management route | No persistent report | No for V2 accounting | Keep legacy labeled; later deprecate after verified replacement |
| `campaigns` | Square inventory-count campaigns | `sync_campaigns` | V1 count sessions/audits | Expected active | Yes for counting, not signage | Rename only in a future approved migration; document semantic boundary |
| `touchscreen_square_variation_cache` / `touchscreen_store_inventory_cache` | Touchscreen catalog/inventory read models | Touchscreen sync | Touchscreen catalog | Expected when sync runs | Canonical only inside touchscreen | Keep separate from Ordering read models |
| `ordering_catalog_identity` vs touchscreen cache | Similar Square identity copies | Separate bounded syncs | Separate feature readers | Expected when refreshed | Each feature-scoped, neither universal catalog master | Consolidation requires an explicit migration; no silent cross-fallback |
| `vendors` / `vendor_sku_configs` Square sync | Internal vendor mapping enriched from Square Vendor Info | Both owner and Square-sync writers | Ordering and reports | Expected active | Mixed provenance | Retain; add provenance metadata only in a future approved migration |
| Square team members vs `employees`/`principals` | Similar employee identity names | Separate external/internal writers | Separate sales vs operations readers | External is live only | No shared master | Do not auto-link without explicit mapping design |

## Silent fallback audit

| Finding | Classification | Current treatment |
|---|---|---|
| Aggregate `purchase_order_lines.received_qty_total` previously settled consignment when no allocation existed | Unsafe and blocked | Corrected: missing canonical allocation rows now produce an integrity warning and no receipt/ledger entry |
| Incomplete V1 line costs previously allowed an ordinary V2 order to be marked PAID using a partial total | Unsafe and blocked | Corrected: PAID is rejected until the saved V1 cost snapshot is complete |
| Current cost used for historical legacy COGS | Unsafe for accounting | Remains only in `cogs_report_service`; explicitly noncanonical for V2 consignment and should be retired later, not silently reused |
| Square customer Order used as vendor PO | Unsafe | No occurrence in Order Payments; static and functional tests guard the boundary |
| Ordered quantity treated as received | Unsafe | No occurrence in settlement; positive reconciled received delta is required |
| Current name/SKU used in immutable sale history | Transitional display enrichment | Square line name wins; current identity may fill a missing source display field only at initial import and is frozen. It never supplies vendor/cost/economic identity |
| Current vendor/cost substituted into historical COGS | Unsafe | Immutable fact attribution uses effective-dated records; unresolved facts block. Owner transaction override requires reason and audit |
| Square deletion treated as internal archive | Unsafe | No occurrence; lifecycle and Square deletion remain separate |
| Current inventory used to infer sales | Unsafe | No occurrence in V2 COGS; inventory is a separate report snapshot |
| Employee sales/cash activity treated as schedule assignment | Unsafe | No occurrence; Square team member is isolated to live sales reporting |
| Missing vendor inferred from product text | Unsafe | No occurrence found |
| Missing receipt inferred from order completion/status | Unsafe | No occurrence; status does not prove receipt |
| Catalog refresh keeps prior nonempty fields when a returned object omits a field | Safe, explicitly documented last-known cache | `last_seen_at`, source timestamps, and refresh coverage distinguish freshness; no cross-system substitute |
| Ordering dashboard uses zero internally when Square evidence is absent | Safe only because explicitly non-actionable | `inventory_valid=false`, required-source blockers, and warnings prevent the zero from becoming a trusted count/recommendation |
| Legacy vendor sync inherits one unambiguous item-level vendor across sibling variations | Transitional and requires owner decision | Kept in V1 sync; provenance is mixed and must not seed effective-dated historical COGS automatically |
| Legacy vendor sync uses first vendor cost or zero when exact Square vendor cost is missing | Transitional/unsafe outside operational current ordering | Kept in V1 sync; PO line freezes the selected value, while V2 historical COGS never uses this fallback |
| V1 order date falls back ordered → submitted → created timestamp | Safe same-domain derivation | All timestamps are V1 facts and precedence is explicit |
| Storeless order displays “Organization-wide” | Safe display label, not receipt evidence | Settlement still requires canonical store allocations |

## Unsupported fields and source ambiguities

- Partial/follow-up purchase-order relationships are not implemented. `RECEIVED_SPLIT_PENDING` is a
  declared state, not a relationship record.
- There is no universal internal product master. Ordering identity and touchscreen caches are separately
  synchronized feature read models.
- `ordering_catalog_identity` does not persist GTIN, Square category, or location availability.
- Product-level internal categories/tags and digital-signage eligibility are not implemented outside the
  specifically named Flavor Finder and lifecycle structures.
- `VendorSkuConfig` has mixed owner/Square-sync provenance and represents current operational mapping/cost,
  not effective-dated historical COGS authority.
- V1 invoice fields and V2 order-payment state can coexist and diverge. The owner preview labels the latter
  as V2 and performs no synchronization in either direction.
- Current consignment inventory valuation uses active `VendorSkuConfig` mappings/costs in
  `inventory_snapshot`; whether these exactly match the legally applicable current consignment cost remains
  an external COGS-report validation item.
- Real email transport is unsupported. `capture_test_email` writes local delivery history only.
- Production population of dormant receipt tables and declared-but-unused PO statuses is unknown because
  the production database was not reachable during this nondeployment audit.

## Mandatory source declaration gate for future V2 work

Before implementation, every V2 feature brief must declare and Codex must verify:

1. Feature name and product owner.
2. Every required business fact.
3. Classification and authoritative system for each fact.
4. Exact existing table/model, writer/service, or external API and source identifier.
5. Canonical read path and any new persistence required.
6. Every write target and whether write-back to V1/Square is permitted.
7. Live, synchronized, snapshot, immutable, override, and derived semantics.
8. Historical snapshot requirements and retention/reversal behavior.
9. Derived formulas with signed components, time boundaries, and rounding.
10. Missing, stale, ambiguous, and conflicting-data behavior.
11. Allowed owner overrides, required reason/audit, and finalized-history restrictions.
12. Known legacy structures and unresolved ambiguities.

Codex must inspect active writers and readers rather than accepting a proposed table name. If repository
evidence contradicts the proposed authority, implementation stops and the conflict is reported. A feature
may not silently substitute a similarly named source.

## Order Payments pre-deployment audit result

- Payment list and detail read only V1 `PurchaseOrder`, `PurchaseOrderLine`,
  `PurchaseOrderStoreAllocation`, `Vendor`, and `Store` records.
- Saved V1 line names, SKU, quantities, `unit_cost`, and aggregate received quantity drive detail; current
  catalog and Square customer lines are not queried.
- Payment methods/defaults/snapshots/status/date/due date/events exist only in V2 tables.
- V2 imports no V1 ordering or receiving writer and performs no V1 write-back.
- Receipt sync requires canonical allocation rows and line-total reconciliation.
- Ordered-but-unreceived quantity produces no receipt value or settlement.
- Square sale/return facts are isolated to COGS routes/services.
- Store-principal guarded denial and mutation CSRF/owner dependencies remain tested.
- `V2_CONSIGNMENT_COGS_ACTIONS_ENABLED=false` independently server-blocks and visibly disables Square sync,
  attribution mutations, COGS-linked cash adjustments, report generation/finalization/void, and email capture.
- `order_payments_v2` remains absent from global feature configuration. No deployment or exposure change
  occurred during this audit.

Classification: the internal Order Payments/replenishment subset remains **READY FOR PRINCIPAL-SCOPED
OWNER PREVIEW AFTER MIGRATION DEPLOYMENT**. Square-derived COGS generation, real return/refund
reconciliation, current Square inventory validation, final vendor report approval, and real vendor-email
delivery remain externally blocked and must be labeled or disabled during the internal preview.

Verification result: the focused provenance/payment/receipt/COGS/authorization suite passed **36 tests**.
The full suite with disposable PostgreSQL 16.12 enabled passed **331 tests with 1 optional private-R2 test
skipped**. Fresh migration, supported downgrade/re-upgrade, and ORM/schema comparison remained green.

## Repository evidence index

- Models and constraints: `app/models.py`; migrations `20260728_0010` and `20260728_0011`.
- V1 ordering/receiving: `app/services/purchase_order_admin_service.py` and management PO routes.
- V2 payments/replenishment: `app/services/v2_order_payments_service.py`,
  `app/routers/v2_order_payments.py`, and order-payment templates.
- Catalog/inventory: `v2_ordering_square_gateway.py`, `v2_ordering_catalog_service.py`,
  `v2_ordering_inventory_refresh_service.py`, inventory repository, lifecycle services, and coordinator.
- Immutable sales/COGS: `v2_consignment_facts_service.py`.
- Employees/scheduling: `employee_log_service.py` and `v2_scheduling_*`/`v2_store_shift_service.py`.
- Signage/touchscreen: `digital_signage_*` and `touchscreen_*` services/routes.
- Daily logs: `v2_daily_store_log_service.py` and `v2_daily_store_logs.py`.
- Live/internal reports: `sales_transactions_report_service.py`, `inventory_velocity_report_service.py`,
  `targeted_sku_demand_report_service.py`, `stock_value_on_hand_service.py`, `cash_reconciliation_service.py`,
  and legacy `cogs_report_service.py`.
