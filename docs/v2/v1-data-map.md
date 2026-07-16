# V1 database and data ownership map

## Database baseline

- PostgreSQL with `citext`, INET, JSON, numeric quantities/money, and 15 named enum types.
- `sql/schema.sql` is the only schema history. It contains 72 `CREATE TABLE IF NOT EXISTS` definitions, additive ALTERs, indexes, seed-like inserts, and `updated_at` triggers.
- SQLAlchemy declares the same 72 tables, but not every SQL-only index/constraint is expressed in ORM metadata. The deployed SQL schema—not ORM metadata alone—is the behavioral baseline.
- `app.main` runs two additive GTIN ALTER statements at every startup.
- Most IDs are BIGSERIAL/BIGINT. Composite fact/current tables use natural composite PKs.
- “Soft delete” means an `active` flag. Otherwise records are retained by status/history or explicitly hard-deleted.

Disposition terms are planning suggestions only: Preserve, Extend, Consolidate, Replace, Investigate.

## Identity, permissions, and system history

| Table / owner | Columns; PK/FK/constraints/indexes | Lifecycle, routes, authority, V2 disposition and hazards |
|---|---|---|
| `stores` / shared foundation | `id` PK; `name`, `square_location_id`, `active`, `created_at`; SQL UNIQUE Square ID, ORM omits unique flag | Read nearly everywhere; written by store-sync CLI/seed. Current authoritative local store registry imported from Square but may include local/mock rows. Soft deactivate. **Preserve/Extend**. Audit duplicate/null Square IDs before migration |
| `principals` / auth & user admin | `id` PK; CITEXT unique `username`; hash, enum `role`, nullable FK `store_id`, `custom_role_label`, `active`, timestamps; role/store CHECK; store index | Read every request; written login-user/group credential/admin routes and seed. Current identity state, soft deactivate. **Preserve**. Multiple UI owners and cosmetic role label |
| `role_permission_overrides` / access | `id` PK; enum `role`, text `permission_key`, `allowed`, updater FK, timestamps; unique role+key; role index | Access-control routes write; middleware/dependencies read. Authoritative override state. **Preserve**. Text key can outlive code definition |
| `principal_permission_overrides` / access | `id` PK; principal FK CASCADE, text key, allowed, updater FK, timestamps; unique principal+key; principal index | Same routes/read path; principal wins over role. **Preserve**. Delete means DEFAULT |
| `web_sessions` / auth | `id` PK; unique token, principal FK, IP, agent, created/seen/expires/revoked timestamps; token/principal indexes | Login creates, each request extends, logout revokes. Current+historical session state. **Replace/Preserve compatibility**. Raw bearer token storage and no cleanup |
| `auth_events` / security audit | `id` PK; attempted CITEXT username, success, failure text, principal FK nullable, IP/agent, created; created index | Login appends only. Historical fact. **Preserve**. Usernames retained even if unknown |
| `audit_log` / operational audit | `id` PK; actor FK nullable, text `action`, optional count-session FK, IP, JSON `metadata`, created; date/action indexes | Manually appended across most modules; session export GET also writes. Historical fact. **Preserve/Extend**. Free-form action/JSON, incomplete coverage, no retention |

## Counts, campaigns, rotation, and Square sync

| Table / owner | Columns; PK/FK/constraints/indexes | Lifecycle, routes, authority, V2 disposition and hazards |
|---|---|---|
| `campaigns` / count configuration | `id` PK; label, nullable category/brand filters, active, created | Group UI/CLI sync write; count generation reads. Current config with soft deactivate. **Preserve**. Category text is external identity |
| `count_groups` / count configuration | `id` PK; unique name, position, active, created; position index | Group CRUD/renumber reads+writes. Current config, soft deactivate. **Preserve** |
| `count_group_campaigns` / count configuration | composite PK/FKs `group_id`,`campaign_id`, created; group index | Group create/update replaces links. Current mapping. **Preserve**. No explicit campaign uniqueness: same campaign may be linked to multiple groups unless service prevents it |
| `store_rotation_state` / counts | store FK/PK; nullable next campaign/group FKs, updated; store/group indexes/triggers | Count generation and set-next-group read/write. Current state. **Preserve/Consolidate**. `next_campaign_id` is legacy beside group rotation |
| `store_forced_counts` / recount/admin | `id` PK; store, campaign/group, source-session, creator FKs; reason, created/consumed, active; active index | Force-recount creates; generation consumes. Queue/history hybrid. **Preserve**. Cyclic FK with count sessions and nullable legacy campaign |
| `count_sessions` / rotating counts | `id` PK; store/campaign/group/creator/submitter/source-forced FKs; employee, enum status, recount flags/timestamps/signature/stability, timestamps; store/status/group/forced indexes | Store generation/draft/submit and management unlock/purge read/write. Durable session aggregate/history. **Preserve**. Hard purge and cyclic FK migration hazard |
| `snapshot_lines` / rotating counts | composite PK session+variation; SKU/names, enum section, expected, prior variance, closed flag, catalog version, created; session/section index | Created at generation; expected/recount values updated at submit/closeout; session/detail/reports read. Historical snapshot with limited later mutation. **Preserve**. Variation IDs external/legacy; expected may be negative |
| `entries` / rotating counts | composite PK session+variation; counted numeric CHECK nonnegative, updater FK, updated | Draft/submit upsert; detail/report/export read. Count facts/current draft. **Preserve**. No created timestamp; blank versus zero decided before persistence |
| `store_recount_state` / recount | store PK/FK; active, prior signature, rounds, updated | Submit state machine reads/writes. Current derived state. **Preserve/Consolidate**. Signature semantics code-defined |
| `store_recount_items` / recount | composite store+variation PK; SKU/names, last variance, match/attempt counts, last qty, updated; store index and nonnegative checks | Submit/closeout mutates; next count reads. Current queue plus accumulated counters. **Preserve**. External variation ID and zero/removal semantics |
| `admin_store_counts` / full count | `id` PK; store/creator/submitter FKs; employee, enum status, expected timestamp, submit/timestamps; store/status indexes | Admin full count page/save/submit/delete. Draft/history aggregate. **Consolidate** with counts only after parity; hard-delete drafts |
| `admin_store_count_lines` / full count | composite count+variation PK; SKU/names, expected, nullable counted, updater FK, timestamps; count index | Admin full count write/read/export/Square submit. Snapshot/draft facts. **Consolidate** cautiously. Nullable counted distinguishes blank from zero |
| `square_sync_events` / integration audit | `id` PK; nullable PO/line/store FKs; text sync type; unique idempotency key; enum status; request/response JSON, error, attempts/timestamps; status/order indexes | Count/admin count/emergency/receiving writes; sync reports/read/retry. Historical integration command/result. **Preserve/Extend**. Polymorphic nullable ownership and free-form type; some idempotency keys are random |

## Store operations and forms

| Table / owner | Columns; PK/FK/constraints/indexes | Lifecycle, routes, authority, V2 disposition and hazards |
|---|---|---|
| `opening_checklist_items` / opening | `id` PK; store FK, position, prompt, enum parent/sub, self-parent FK, active, created; store index | Lazily seeded/read by store form; no V1 editor. Current template, soft deactivate possible only outside routes. **Preserve/Investigate** template versioning |
| `opening_checklist_submissions` / opening | `id` PK; store/creator FKs; names, enum notes type, notes, submitted/created; store-date index | Store submit appends; management list/detail reads. Historical fact. **Preserve** |
| `opening_checklist_answers` / opening | composite submission+item PK/FKs; enum Y/N/NA, created; submission index | Store submit appends; detail reads. Historical fact. **Preserve**. FK to mutable item prompt means prompt snapshot is not stored |
| `daily_chore_tasks` / chores | `id` PK; store FK, position, section/prompt, active, created; store index | Lazily seeded and admin add/reorder/delete writes; sheets read. Current per-store template, soft deactivate. **Consolidate/Preserve**. UI calls tasks global while data is per-store |
| `daily_chore_sheets` / chores | `id` PK; store/creator FKs; date, employee, enum status, submitted/timestamps; store-date index | Store create/save/restart/submit/delete; admin read/delete. Draft+historical aggregate. **Preserve**. No unique store/date constraint visible |
| `daily_chore_entries` / chores | composite sheet+task PK/FKs; completed, completed/updated/created | Draft/submit upsert; detail reads. Draft/history. **Preserve**. Task prompt not snapshotted |
| `non_sellable_items` / non-sellable | `id` PK; unique name, active, creator FK, timestamps; active index | Lazy defaults/admin/store-par catalog writes; takes/pars read. Global current catalog, soft deactivate. **Preserve**. Case-sensitive uniqueness vs user normalization |
| `non_sellable_stock_takes` / non-sellable | `id` PK; store/creator/submitter FKs; employee, enum status, submit/timestamps; store-date index | Store workflow and store-par delivery create/write; management read/unlock. Draft/history. **Preserve**. Multiple owning modules |
| `non_sellable_stock_take_lines` / non-sellable | composite take+item PK; snapshotted item name, quantity, timestamps; take index | Store save/submit and par delivery write; audit reads. Historical facts. **Preserve**. Quantity nonnegative SQL CHECK |
| `customer_request_items` / requests | `id` PK; name, unique normalized name, `request_count`, active, creator FK, timestamps; active index | Store submissions get/create; management create/reactivate/count writes. Current catalog+curated aggregate. **Investigate/Consolidate**. `request_count` may duplicate derivable line totals |
| `customer_request_submissions` / requests | `id` PK; store/creator FKs, notes, created; store-date index | Store append; management list. Historical fact. **Preserve** |
| `customer_request_lines` / requests | `id` PK; submission/item FKs; raw name, quantity, created; submission index | Store append; management list. Historical fact. **Preserve**. Raw and normalized identity intentionally coexist |
| `exchange_return_forms` / exchanges | `id` PK; store/creator FKs; employee/date/generated/ticket/item/reason/refund fields, created; store-date index | Store append; management list/detail. Historical fact. **Preserve**. Items/reason are free text, no external ticket ID FK |
| `employees` / employee logs | `id` PK; name, unique normalized name, lead visibility, active, creator FK, timestamps; active/visibility index | Employee admin writes; log entry/read filters. Current directory, soft deactivate. **Preserve**. Not linked to principals/Square team IDs |
| `employee_log_categories` / employee logs | `id` PK; label, unique normalized label, position, active, creator FK, timestamps; active index | Admin writes; entries/read. Current taxonomy, soft deactivate. **Preserve** |
| `employee_log_entries` / employee logs | `id` PK; employee FK, nullable category FK, snapshotted category label, note, creator FK, created; employee/category indexes | Management entry appends; report page reads. Historical fact. **Preserve**. Employee name itself not snapshotted |

## Cash, change, par, and audit state

| Table / owner | Columns; PK/FK/constraints/indexes | Lifecycle, routes, authority, V2 disposition and hazards |
|---|---|---|
| `change_box_counts` / change box | `id` PK; store/creator/submitter FKs; employee, enum status, total, submit/timestamps; store-date index | Store draft/submit; management read/hard-delete. History aggregate. **Preserve**. Submission also mutates live inventory |
| `change_box_count_lines` / change box | composite count+denomination PK; label, position, value, quantity, amount, timestamps; count index | Count write/read. Historical snapshot. **Preserve**. Line amount is derived but stored |
| `change_box_inventory_settings` / change current state | store PK/FK; target amount, updated | Lazy init/change form/audit/par read/write. Authoritative current setting. **Preserve/Consolidate** ownership |
| `change_box_inventory_lines` / change current state | composite store+denomination PK; label/value/quantity, updater FK, timestamps; store index | Count submit, change form, audit, par delivery, lazy init write. Authoritative current balance. **Preserve** with single-writer design; highest shared-owner risk |
| `change_form_submissions` / change forms | `id` PK; store/creator FKs; generated, employee/signature, created; store-date index | Store append; management/report reads. Historical fact. **Preserve** |
| `change_form_lines` / change forms | `id` PK; submission FK; text section, denomination code/label, quantity/value/amount, created; submission index | Store append; management/master-safe report reads. Historical fact. **Preserve**. `section` is text enum; amount derived/stored |
| `change_box_audit_submissions` / change audit | `id` PK; store/creator FKs; auditor, target, created; store-date index | Admin audit append; no dedicated history list beyond page context. Historical fact. **Preserve** |
| `change_box_audit_lines` / change audit | `id` PK; audit FK; denomination snapshot/qty/amount, created; audit index | Audit append. Historical fact. **Preserve** |
| `change_box_par_levels` / store par | composite store+denomination PK; level qty, par qty, updater FK, timestamps; store index | Store-par save/delivery reads+writes. Current state. **Preserve**. Level and current inventory can diverge |
| `non_sellable_par_levels` / store par | composite store+item PK/FKs; level qty, par qty, updater FK, timestamps; store index | Store-par save/delivery reads+writes. Current state. **Preserve** |
| `store_par_delivery_lines` / store par | `id` PK; store FK; text item type/key/label, value, quantity, creator FK, timestamps; unique store+type+key; nonnegative check/index | Stage replaces/upserts, deliver/clear deletes. Current queue, not durable delivery history. **Preserve/Extend**. Text item type and deletion lose queue history; delivered effects live elsewhere |
| `master_safe_inventory_settings` / master safe | singleton integer PK; target, updated | Lazy init/audit reads+writes. Global current state. **Preserve**. Singleton convention is code-defined |
| `master_safe_inventory_lines` / master safe | denomination PK; label/value/quantity, updater FK, timestamps | Audit/change-form logic reads/writes. Global current balance. **Preserve** |
| `master_safe_par_levels` / master safe | denomination PK; par amount CHECK nonnegative, updater FK, timestamps; updated index | Seed/save/read. Current target. **Preserve**. Amount rather than quantity differs from change-box par representation |
| `master_safe_audit_submissions` / master safe | `id` PK; auditor, target, creator FK, created; date index | Admin audit append; page/report reads. Historical fact. **Preserve**. No store because safe is global |
| `master_safe_audit_lines` / master safe | `id` PK; audit FK; denomination snapshot/qty/amount, created; audit index | Audit append. Historical fact. **Preserve** |
| `cash_reconciliation_actuals` / cash reconciliation | composite store+business date PK; actual cents, updater FK, timestamps; store-date index | Save upserts; actual/history reads. Authoritative current actual per day. **Preserve**. Zero is valid; no null state after row exists |
| `cash_reconciliation_verification_batches` / cash reconciliation | `id` PK; store/verifier FKs; range, day count, total drop cents, note, created; store-created index | Save may append; batch list/detail reads. Historical aggregate. **Preserve** |
| `cash_reconciliation_verifications` / cash reconciliation | `id` PK; nullable batch FK, store/verifier FKs; date, previous/actual/expected cents nullable as defined, note, created; store/batch indexes | Every changed/saved day appends; history/detail reads. Historical fact. **Preserve**. Expected nullable/live recomputation distinction |

## Dashboard and ordering reference data

| Table / owner | Columns; PK/FK/constraints/indexes | Lifecycle, routes, authority, V2 disposition and hazards |
|---|---|---|
| `dashboard_categories` / dashboard | `id` PK; unique name, position, active, timestamps; position index | Lazy default/admin CRUD; dashboard reads. Current UI config, soft deactivate. **Preserve/Replace** depending V2 navigation design |
| `dashboard_card_assignments` / dashboard | text `card_key` PK; nullable category FK, position, updater FK, timestamps; category index | Admin saves; dashboard reads. Current mapping to code-defined card registry. **Replace** when route registry changes; preserve adapter during cutover |
| `role_dashboard_category_access` / dashboard/access | `id` PK; enum role, category/updater FKs, allowed, timestamps; unique role+category/index | Access routes write; dashboard reads. Visibility config. **Preserve/Consolidate** with navigation policy, not backend permission |
| `ordering_math_settings` / ordering | singleton int PK; default weeks/lookback, updater FK, timestamps | Lazily created/read by generation. Current global settings. **Preserve**. Environment values only seed first row |
| `vendors` / ordering reference | `id` PK; unique Square vendor ID, name, active, sync/timestamps; active/name index | Square sync writes; ordering/reports read. Imported current reference, soft deactivate. **Preserve** |
| `vendor_contacts` / ordering reference | `id` PK; vendor/updater FKs; contact, required email-to, email-cc, active, timestamps; vendor index | No active route/service reference found. **Investigate** production data and external/manual use; do not retire yet |
| `vendor_ordering_settings` / ordering | vendor PK/FK; reorder/stock-up/lookback, updater FK, timestamps | Ordering effective math reads; no clear dedicated UI write found. Current override. **Investigate/Preserve** |
| `vendor_sku_configs` / product mapping | `id` PK; vendor/updater FKs; SKU, Square variation, GTIN, cost, pack, MOQ, default, active, timestamps; unique vendor+SKU; partial unique default-vendor index; vendor/variation indexes | Mapping UI/import/sync writes; ordering/receiving/reports read. Authoritative local mapping/current cost. **Preserve/Extend**. Critical null/zero/default-vendor semantics |
| `purchase_order_pdf_templates` / ordering docs | `id` PK; name/disclaimer, generic flag, nullable unique vendor FK, active, updater FK, timestamps; partial unique generic/index | PDF template admin writes; PDF generation reads. Current config. **Preserve/Replace storage**. Generic/vendor uniqueness is SQL-index dependent |
| `par_levels` / ordering | `id` PK; SKU, nullable vendor/store FKs; manual/suggested/stock-up, enum source/confidence, score, lock/streaks, updater FK, timestamps; partial global/store unique indexes and SKU index | Par UI/prefill/generation writes+reads. Current learned/manual state. **Preserve**. Nullable vendor/store and zero/manual semantics; uniqueness mostly SQL-only |

## Purchase orders, receiving, and emergency inventory

| Table / owner | Columns; PK/FK/constraints/indexes | Lifecycle, routes, authority, V2 disposition and hazards |
|---|---|---|
| `purchase_orders` / ordering | `id` PK; vendor/creator/submitter/email-sender FKs; enum status; snapshotted math; notes/PDF; text invoice status/date/amount/note; ordered/submitted/email/timestamps; vendor/status indexes and invoice checks | Generation/report creates; detail/invoice/submit/delete/receive writes; list/detail reads. Durable order aggregate. **Preserve**. Unused states/email fields and text invoice enum need audit |
| `purchase_order_lines` / ordering | `id` PK; PO FK; variation/SKU/GTIN/names; cost/price; suggested/ordered/received/in-transit; confidence/par snapshots; removed; timestamps; unique PO+variation, indexes | Generation/add/refresh/save/scan/receive writes; detail/PDF reads. Durable snapshot/current line. **Preserve**. Derived totals stored; nullable cost/SKU |
| `purchase_order_store_allocations` / ordering | `id` PK; line/store FKs; expected/allocated/manual par/store received/variance, timestamps; unique line+store, store index | Generation/save/barcode/receiving writes; detail/Square push reads. Current allocation plus snapshot fields. **Preserve**. Null received vs zero, variance derived/stored |
| `purchase_order_receipts` / receiving | `id` PK; PO/receiver FKs; enum status, received timestamp, partial flag, notes, timestamps; order index | Model/schema present; current receive service does not import/use it. **Investigate** production rows/legacy code before retirement |
| `purchase_order_receipt_lines` / receiving | `id` PK; receipt/PO-line FKs; expected/received/difference, notes, created; unique receipt+line/index | No active service use found. Historical design artifact or externally populated. **Investigate** |
| `emergency_on_hand_drafts` / emergency inventory | `id` PK; vendor/creator/submitter FKs; enum status, submit/timestamps; vendor/status indexes | Emergency routes create/save/push/list. Draft/history aggregate. **Preserve** |
| `emergency_on_hand_draft_lines` / emergency inventory | `id` PK; draft FK; SKU/names/nullable variation; JSON store quantities, timestamps; unique draft+SKU/index | Emergency add/save/push reads+writes. Draft snapshot. **Preserve/Extend**. JSON keys are store IDs and bypass relational FK validation |

## Enums and text-enum hazards

Database enums cover principal role; draft/submitted statuses; count section; checklist item/answer/notes; PO/confidence/par/receipt/emergency/sync states. Important text fields acting as enums include `purchase_orders.invoice_payment_status`, `square_sync_events.sync_type`, `store_par_delivery_lines.item_type`, `change_form_lines.section`, and `audit_log.action`/metadata conventions.

## Orphan, duplication, and migration hazards

- Many FKs omit explicit `ON DELETE` in the ORM; SQL cascade/set-null behavior must be read from `schema.sql` during migration design.
- Mutable template rows (opening items/chore tasks/employees) are referenced by history without always snapshotting display text.
- `count_sessions`↔`store_forced_counts` is cyclic.
- Separate rotating/admin count table families duplicate inventory snapshot/count concepts.
- Current change-box state has at least four writers.
- `store_par_delivery_lines` is deleted after action, so the audit log/effect records are the only delivery trace.
- External Square variation/location/vendor IDs are text and can be null or legacy/mock.
- SQL-only partial unique indexes protect default vendor, global/store par rows, generic PDF templates; preserve them in any migration tooling.
- Production distinct values must be inspected for every text-enum and nullable numeric field before constraints or consolidation.
