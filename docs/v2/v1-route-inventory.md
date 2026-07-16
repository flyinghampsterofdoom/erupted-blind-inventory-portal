# V1 route inventory

## Legend and method

This file covers all 176 V1 FastAPI route decorators in `app/main.py`, `app/routers/auth.py`, `app/routers/store.py`, and `app/routers/management.py`. Path parameters are listed as inputs even when obvious. Form/query inputs reflect implementation reads; dynamically prefixed line/quantity fields are summarized. “R/W” names table families, not every audit row; mutating routes generally also append `audit_log`. Redirect targets may include query-string status/error fields.

Permissions: **PUB** public, **AUTH** any authenticated principal, **SA** `store.access`, **MA** `management.access`, **AD** `management.admin`, **GP** `management.groups`, **US** `management.users`, **RA** literal `Role.ADMIN`, **EL** employee-log management wrapper, **ELA** employee-log admin-role wrapper. All POSTs listed are CSRF-protected.

Class: UI=user-facing HTML, action=form mutation/redirect, fetch=browser API-like JSON, export=file response, internal=redirect/robots compatibility. Status is Active unless noted. “V2” is a likely destination, not an implemented route.

## Root and authentication

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/` — `root` | Auth / AUTH | request | R principal permission flags | 303→management home/store home/login; internal | V2 entry; preserve role/capability redirect until cutover |
| GET `/robots.txt` — `robots_txt` | System / PUB | none | None | text/plain disallow; internal | Preserve globally |
| GET `/login` — `login_page` | Auth / PUB | none | None | `login.html`; UI | Shared auth |
| POST `/login` — `login_submit` | Auth / PUB | username, password, user-agent/IP | R principal; W session, auth event, audit | 303→`/` or `login.html` 401; action | Shared auth; preserve error opacity |
| POST `/logout` — `logout` | Auth / AUTH | session cookie | W revoke session, audit | 303→login, delete cookie; action | Shared auth |

## Store routes

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/store/home` — `home` | Store home / SA | principal store | R workflow drafts/statuses; may lazy-init | `store_home.html`; UI | Store Operations overview |
| GET `/store/non-sellable-stock-take` — `non_sellable_stock_take_page` | Non-sellable / SA | principal store | R/W lazy item/draft/lines | `store_non_sellable_stock_take.html`; UI | Inventory / Non-sellable |
| GET `/store/customer-requests` — `customer_requests_page` | Requests / SA | principal store | R item suggestions | `store_customer_requests.html`; UI | Customer & Forms |
| GET `/store/exchange-return-form` — `exchange_return_form_page` | Exchanges / SA | principal store | None | `store_exchange_return_form.html`; UI | Customer & Forms |
| POST `/store/exchange-return-form/submit` — `exchange_return_form_submit` | Exchanges / SA | generated/purchase dates, employee, original/exchange tickets, items, reason, refund flag/approver | R store; W exchange form, audit | 303→form; action | Customer & Forms / Exchanges |
| GET `/store/change-form` — `change_form_page` | Change forms / SA | principal store | None | `store_change_form.html`; UI | Customer & Forms / Change |
| POST `/store/change-form/submit` — `change_form_submit` | Change forms / SA | employee, signature, generated date, dynamic denomination quantities | R/W current change inventory; W form/lines/audit | 303→form; action | Cash/change domain; redirect old route only after balance parity |
| POST `/store/customer-requests/submit` — `customer_requests_submit` | Requests / SA | requested item text, notes | R/W item catalog; W submission/lines/audit | 303→requests; action | Customer & Forms / Requests |
| POST `/store/non-sellable-stock-take/{stock_take_id}/save` — `non_sellable_stock_take_save` | Non-sellable / SA | ID, employee, dynamic item quantities | R take/store; W draft/lines/audit | 303→page; action | Inventory / Non-sellable |
| POST `/store/non-sellable-stock-take/{stock_take_id}/submit` — `non_sellable_stock_take_submit` | Non-sellable / SA | ID, employee, quantities | R take/store; W submitted take/lines/audit | 303→page; action | Inventory / Non-sellable |
| GET `/store/change-box-count` — `change_box_count_page` | Change box / SA | principal store | R/W lazy current inventory/draft | `store_change_box_count.html`; UI | Store Operations / Cash |
| POST `/store/change-box-count/{count_id}/save` — `change_box_count_save` | Change box / SA | ID, employee, denomination quantities | R count/store; W draft/lines/audit | 303→page; action | Store Operations / Cash |
| POST `/store/change-box-count/{count_id}/submit` — `change_box_count_submit` | Change box / SA | ID, employee, quantities | W submitted count/lines, current inventory, audit | 303→page; action | Cash current-state cutover must be atomic |
| GET `/store/daily-count` — `daily_count_page` | Rotating counts / SA | principal store | R active session/rotation | `store_daily_count.html`; UI | Inventory / Counts |
| GET `/store/daily-chore-sheet` — `daily_chore_sheet_page` | Chores / SA | principal store/date | R/W lazy tasks/sheet/entries | `store_daily_chore_sheet.html`; UI | Store Operations / Chores |
| POST `/store/daily-chore-sheet/{sheet_id}/save` — `daily_chore_sheet_save` | Chores / SA | ID, employee, completed task IDs, autosave header | R ownership; W sheet/entries/audit | 303 or autosave response; action/fetch | Preserve autosave and 401 behavior |
| POST `/store/daily-chore-sheet/{sheet_id}/submit` — `daily_chore_sheet_submit` | Chores / SA | ID, employee, completed IDs | W submitted sheet/entries/audit | 303→sheet; action | Store Operations / Chores |
| POST `/store/daily-chore-sheet/{sheet_id}/restart` — `daily_chore_sheet_restart` | Chores / SA | ID | W reset sheet/entries/audit | 303→sheet; action | Store Operations / Chores |
| POST `/store/daily-chore-sheet/{sheet_id}/delete` — `daily_chore_sheet_delete` | Chores / SA | ID | Hard delete draft + audit | 303→sheet; action | Preserve draft-only guard |
| GET `/store/opening-checklist` — `opening_checklist_page` | Opening / SA | principal store | R/W lazy checklist items; R today submission | `store_opening_checklist.html`; UI | Store Operations / Opening |
| POST `/store/opening-checklist/submit` — `opening_checklist_submit` | Opening / SA | submitter/lead/previous employee, notes type/text, dynamic answers | R items; W submission/answers/audit | 303→checklist or template 400; action | Store Operations / Opening |
| GET `/store/opening-checklist/submit` — `opening_checklist_submit_get_redirect` | Opening / AUTH middleware only | none | None | 303→canonical checklist; internal, compatibility | Preserve redirect during cutover; mobile POST-refresh workaround |
| POST `/store/sessions/generate` — `generate_session` | Rotating counts / SA | employee | R/W rotation/forced/recount; R Square/mock catalog; W session/snapshot/audit | 303→session; action | Inventory / Counts; do not split active session |
| GET `/store/sessions/{session_id}` — `view_session` | Rotating counts / SA | ID | R scoped session/snapshot/entries | `count_entry.html`; UI | Inventory / Count Entry |
| POST `/store/sessions/{session_id}/draft` — `save_draft` | Rotating counts / SA | ID, dynamic quantities, autosave header | R scoped session; W entries/audit | 303 or autosave response; action/fetch | Inventory / Count Entry |
| POST `/store/sessions/{session_id}/submit` — `submit` | Rotating counts / SA | ID, quantities | R live Square/mock on-hand; W session/snapshot/entries/recount/sync/audit | 303→session; action | Highest-risk count cutover; may auto-push Square |

## Management dashboard, full count, cash, and store par

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/management/home` — `home` | Dashboard / MA | principal | R permissions/dashboard; W lazy categories | `management_home.html`; UI | V2 Overview |
| GET `/management/dashboard-settings` — `dashboard_settings_page` | Dashboard / AD | saved/error | R/W lazy dashboard config | `management_dashboard_settings.html`; UI | Admin / Navigation |
| POST `/management/dashboard-settings/categories` — `dashboard_settings_create_category` | Dashboard / AD | name | W category | 303→settings; action | Admin / Navigation |
| POST `/management/dashboard-settings/categories/save` — `dashboard_settings_save_categories` | Dashboard / AD | dynamic category name/position/active | W categories | 303→settings; action | Admin / Navigation |
| POST `/management/dashboard-settings/cards/save` — `dashboard_settings_save_cards` | Dashboard / AD | dynamic card category/position | W assignments/audit | 303→settings; action | Admin / Navigation; card keys change in V2 |
| GET `/management/store-count` — `management_store_count_page` | Full count / AD | store ID, count ID, status query flags | R/W lazy draft/Square snapshot | `management_store_count.html`; UI | Inventory / Full Count |
| POST `/management/store-count/{count_id}/save` — `management_store_count_save` | Full count / AD | ID, employee/store, dynamic quantities | W draft lines/audit | 303→store-count; action | Inventory / Full Count |
| POST `/management/store-count/{count_id}/submit` — `management_store_count_submit` | Full count / AD | ID, employee/store, quantities | W count/lines/sync/audit; Square inventory write | 303→store-count; action | Do not dual-run Square writers |
| POST `/management/store-count/{count_id}/delete` — `management_store_count_delete` | Full count / AD | ID | Hard delete draft/audit | 303→store-count; action | Preserve draft guard |
| POST `/management/store-count/{count_id}/excel` — `management_store_count_excel_download` | Full count / AD | ID, store, employee | R count/lines | XLSX stream or 303 error; export | Reports / Counts; POST export |
| GET `/management/cash-reconciliation` — `cash_reconciliation_page` | Cash / AD | none | R active Square-enabled stores | `management_cash_reconciliation.html`; UI | Store Operations / Cash Reconciliation |
| GET `/management/cash-reconciliation/expected` — `cash_reconciliation_expected` | Cash / AD | store, start/end | R store + live Square payments/refunds/drawers | JSON-like dict; fetch | Cash integration endpoint |
| GET `/management/cash-reconciliation/actual` — `cash_reconciliation_actual` | Cash / AD | store, start/end | R actual/verification history | JSON-like dict; fetch | Cash integration endpoint |
| GET `/management/cash-reconciliation/batches` — `cash_reconciliation_batches` | Cash / AD | store | R batches | JSON-like list; fetch | Cash history endpoint |
| POST `/management/cash-reconciliation/actual` — `cash_reconciliation_actual_save` | Cash / AD | store, rows JSON, expected JSON, note; date/actual/expected fields | R principal/store; W actuals/verifications/batch/audit | JSON-like result; fetch/action | Preserve upsert+append transaction |
| GET `/management/cash-reconciliation/verification-batches/{batch_id}` — `cash_reconciliation_verification_batch_page` | Cash / AD | batch ID | R batch/verifications | `management_cash_reconciliation_batch.html`; UI | Cash history detail |
| GET `/management/store-par-reset` — `store_par_reset_page` | Store par / AD | store, status flags | R/W lazy change/non-sellable/par state | `management_store_par_reset.html`; UI | Store Operations / Replenishment |
| POST `/management/store-par-reset/non-sellable-items/create` — `store_par_reset_non_sellable_item_create` | Store par / AD | store, name | W global item/audit | 303→par reset; action | Admin/Inventory catalog overlap |
| POST `/management/store-par-reset/non-sellable-items/{item_id}/deactivate` — `store_par_reset_non_sellable_item_deactivate` | Store par / AD | item, store | W item active/audit | 303→par reset; action | Shared non-sellable catalog |
| POST `/management/store-par-reset/save` — `store_par_reset_save` | Store par / AD | store, dynamic levels/pars | W change/non-sellable pars + audit | 303→par reset; action | Cross-domain current state |
| POST `/management/store-par-reset/move-to-delivery` — `store_par_reset_move_to_delivery` | Store par / AD | store, selected dynamic lines | R current/par; W delivery queue/audit | 303→delivery; action | Do not migrate queue mid-flight |
| GET `/management/store-par-reset/load-delivery` — `store_par_reset_load_delivery_page` | Store par / AD | store, status flags | R queue/current/par | `management_store_par_reset_delivery.html`; UI | Store Operations / Delivery |
| POST `/management/store-par-reset/load-delivery/deliver` — `store_par_reset_deliver` | Store par / AD | store, bill-removal quantities | W current change inventory, submitted non-sellable take, clear queue, audit | 303→delivery; action | Cross-module atomic operation |
| POST `/management/store-par-reset/load-delivery/clear` — `store_par_reset_clear_delivery` | Store par / AD | store | Hard delete queue + audit | 303→delivery; action | Preserve queue-only cleanup |

## Ordering, mappings, purchase orders, and receiving

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/management/ordering-tool` — `ordering_tool_page` | Ordering / AD | optional vendor; order display/status fields | R vendors/orders/lines | `management_ordering_tool.html`; UI | Ordering Overview |
| GET `/management/ordering-tool/emergency-editor` — `ordering_tool_emergency_editor_page` | Emergency inventory / AD | draft ID | R vendors/draft/lines/stores/mappings | `management_ordering_emergency_editor.html`; UI | Ordering / Emergency Inventory |
| POST `/management/ordering-tool/emergency-editor/start-draft` — `ordering_tool_emergency_editor_start_draft` | Emergency inventory / AD | vendor | W draft | 303→editor; action | Preserve one draft command |
| POST `/management/ordering-tool/emergency-editor/{draft_id}/add-sku` — `ordering_tool_emergency_editor_add_sku` | Emergency inventory / AD | draft, lookup/SKU | R catalog/mapping; W draft line | 303→editor; action | Validate external variation mapping |
| POST `/management/ordering-tool/emergency-editor/{draft_id}/save` — `ordering_tool_emergency_editor_save` | Emergency inventory / AD | ID, dynamic store quantities | W line JSON/audit | 303→editor; action | JSON store keys need compatibility |
| POST `/management/ordering-tool/emergency-editor/{draft_id}/push` — `ordering_tool_emergency_editor_push` | Emergency inventory / AD | ID, quantities | W sync events/draft/audit; Square inventory writes | 303 or editor error template; action | Late cutover; partial success/idempotency risk |
| GET `/management/ordering-tool/mappings` — `ordering_tool_mappings_page` | SKU mappings / AD | vendor filter | R vendors/configs | `management_ordering_mappings.html`; UI | Admin / Product Mappings |
| GET `/management/ordering-tool/par-levels` — `ordering_tool_par_levels_page` | Par levels / AD | none | R vendors | `management_ordering_par_levels.html`; UI | Ordering / Par Levels |
| GET `/management/ordering-tool/pdf-templates` — `ordering_tool_pdf_templates_page` | Ordering docs / AD | none | R templates/vendors | `management_ordering_pdf_templates.html`; UI | Admin / Ordering Documents |
| POST `/management/ordering-tool/pdf-templates/save` — `ordering_tool_pdf_templates_save` | Ordering docs / AD | vendor IDs, generic flag, name, disclaimer | W templates/audit | 303→templates; action | Preserve generic/vendor uniqueness |
| POST `/management/ordering-tool/pdf-templates/{template_id}/edit` — `ordering_tool_pdf_templates_edit` | Ordering docs / AD | ID, name, disclaimer | W template/audit | 303→templates; action | Admin / Ordering Documents |
| GET `/management/ordering-tool/par-levels/{vendor_id}` — `ordering_tool_par_levels_vendor_page` | Par levels / AD | vendor, history lookback | R vendor/config/store/par + live Square | `management_ordering_par_levels_vendor.html`; UI | Ordering / Vendor Pars |
| POST `/management/ordering-tool/par-levels/{vendor_id}/save` — `ordering_tool_par_levels_vendor_save` | Par levels / AD | vendor, dynamic row keys/pars, lookback | W par levels/audit | 303→vendor pars; action | Preserve manual zero/lock semantics |
| POST `/management/ordering-tool/par-levels/{vendor_id}/prefill` — `ordering_tool_par_levels_vendor_prefill` | Par levels / AD | vendor, lookback | R live Square; W store pars/audit | 303→vendor pars; action | Derived write; fixture parity required |
| POST `/management/ordering-tool/mappings/upsert` — `ordering_tool_mappings_upsert` | SKU mappings / AD | SKU, vendor, Square variation, cost, pack, MOQ, default, active | W config/audit | 303→mappings; action | Admin / Product Mappings |
| POST `/management/ordering-tool/mappings/import` — `ordering_tool_mappings_import` | SKU mappings / AD | CSV file | W configs/audit | 303→mappings; action/import | Preserve CSV headers/upsert behavior |
| POST `/management/ordering-tool/mappings/bulk-save` — `ordering_tool_mappings_bulk_save` | SKU mappings / AD | dynamic row fields | W configs/audit | 303→mappings; action | Admin / Product Mappings |
| POST `/management/ordering-tool/mappings/auto-fill` — `ordering_tool_mappings_auto_fill` | SKU mappings / AD | vendor | R Square catalog; W configs/audit | 303→mappings; action/sync | Admin / Integrations |
| POST `/management/ordering-tool/vendors/sync` — `ordering_tool_sync_vendors` | Vendors / AD | none | R Square vendors; W vendors/audit | 303→ordering/par page; action/sync | Admin / Vendors |
| POST `/management/ordering-tool/generate` — `ordering_tool_generate` | PO generation / AD | dynamic vendor selections and math parameters | R Square/catalog/mapping/par/in-transit; W PO/lines/allocations/audit | 303→ordering; action | Ordering / Generate |
| POST `/management/ordering-tool/generate-full-stock` — `ordering_tool_generate_full_stock` | PO generation / AD | dynamic vendor selection/math | Same, includes zero/full-stock lines; W orders/audit | 303→ordering; action | Preserve include-zero semantics |
| GET `/management/ordering-tool/orders/{purchase_order_id}` — `ordering_tool_order_detail` | PO detail / AD | order ID | R PO/vendor/lines/allocations/mappings/sync | `management_ordering_order_detail.html`; UI | Ordering / Order Detail |
| GET `/management/ordering-tool/orders/{purchase_order_id}/pdf` — `ordering_tool_order_pdf_download` | PO export / AD | order ID | R PO/template/lines; W/regenerate PDF path/file | PDF `FileResponse`; export | Ordering / Documents; migrate stored files/redirect |
| POST `/management/ordering-tool/orders/{purchase_order_id}/save` — `ordering_tool_order_save` | PO editing / AD | order ID, dynamic line qty/remove/allocation/note fields | W PO/lines/allocations/audit | 303→detail; action | Preserve zero/remove rules |
| POST `/management/ordering-tool/orders/{purchase_order_id}/invoice` — `ordering_tool_order_invoice_save` | PO invoice / AD | payment status/date/amount/difference note | W PO/audit | 303→detail; action | Ordering / Invoice; text enum audit |
| POST `/management/ordering-tool/orders/{purchase_order_id}/add-line` — `ordering_tool_order_add_line` | PO editing / AD | order, SKU, initial qty | R catalog/mapping; W line/allocations/audit | 303→detail; action | Ordering / Order Detail |
| POST `/management/ordering-tool/orders/{purchase_order_id}/refresh-lines` — `ordering_tool_order_refresh_lines` | PO editing / AD | order | R Square catalog; W line labels/cost/GTIN/audit | 303→detail; action | Current catalog overwrites draft snapshot fields |
| POST `/management/ordering-tool/orders/{purchase_order_id}/submit` — `ordering_tool_order_submit` | PO workflow / AD | order, saved dynamic fields | W PO DRAFT→IN_TRANSIT, PDF, audit | 303→detail; action | Ordering / Submit; no email send |
| POST `/management/ordering-tool/orders/{purchase_order_id}/received-quantities` — `ordering_tool_order_received_quantities_save` | Receiving / AD | order, dynamic per-store received quantities | W allocations/line totals/audit | 303→detail; action | Ordering / Receiving |
| POST `/management/ordering-tool/orders/{purchase_order_id}/scan-barcode` — `ordering_tool_order_scan_barcode` | Receiving / AD | order, line/store/barcode/overage | R mapping; W allocation/line/audit | JSON; fetch/action | Preserve pack/GTIN/overage behavior |
| POST `/management/ordering-tool/orders/{purchase_order_id}/scan-barcode/cancel` — `ordering_tool_order_scan_barcode_cancel` | Receiving / AD | order, line/store | W allocation/line/audit | JSON; fetch/action | Ordering / Receiving |
| POST `/management/ordering-tool/orders/{purchase_order_id}/receive` — `ordering_tool_order_receive` | Receiving / AD | order, quantities | W sync events/PO/audit; Square inventory writes | 303→detail; action | Late Square-write cutover |
| POST `/management/ordering-tool/orders/{purchase_order_id}/receive-retry-failed` — `ordering_tool_order_receive_retry_failed` | Receiving / AD | order | R failed sync; W events/PO/audit; Square retry | 303→detail; action | Preserve deterministic idempotency |
| POST `/management/ordering-tool/orders/{purchase_order_id}/delete` — `ordering_tool_order_delete` | PO editing / AD | order | Hard delete draft and PDF; audit | 303→ordering; action | Draft-only route; retention decision |

## Chores, checklists, cash/change audits, forms, and employee logs

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/management/daily-chore-lists` — `daily_chore_lists_page` | Chore audit / MA | store, from/to | R sheets/entries/stores | `management_daily_chore_audit.html`; UI | Audits / Chores |
| GET `/management/daily-chore-tasks` — `daily_chore_tasks_page` | Chore admin / AD | status/error flags | R task template rows | `management_daily_chore_tasks.html`; UI | Admin / Store Tasks |
| POST `/management/daily-chore-tasks/add` — `daily_chore_tasks_add` | Chore admin / AD | section, prompt, section order | W tasks/audit | 303→task editor; action | Validate global/per-store propagation |
| POST `/management/daily-chore-tasks/reorder` — `daily_chore_tasks_reorder` | Chore admin / AD | task number/new number | W positions/audit | 303→task editor; action | Admin / Store Tasks |
| POST `/management/daily-chore-tasks/delete` — `daily_chore_tasks_delete` | Chore admin / AD | task number | W active/delete semantics + audit | 303→task editor; action | Preserve history references |
| GET `/management/daily-chore-lists/{sheet_id}` — `daily_chore_sheet_detail` | Chore audit / MA | sheet | R sheet/task/entries; W view audit | `management_daily_chore_detail.html`; UI | Audits / Chores |
| POST `/management/daily-chore-lists/{sheet_id}/delete` — `daily_chore_sheet_delete` | Chore audit / AD | sheet, return store/from/to | Hard delete draft + audit | 303→filtered list; action | Draft-only cleanup |
| GET `/management/opening-checklists` — `opening_checklists_page` | Opening audit / MA | store, from/to | R submissions/stores | `management_opening_checklist_audit.html`; UI | Audits / Opening |
| GET `/management/opening-checklists/{submission_id}` — `opening_checklists_detail` | Opening audit / MA | submission | R submission/items/answers; W view audit | `management_opening_checklist_detail.html`; UI | Audits / Opening |
| GET `/management/change-box-count` — `change_box_count_page` | Change-box audit / MA | store | R counts/stores | `management_change_box_count_audit.html`; UI | Audits / Change Box |
| GET `/management/change-box-count/{count_id}` — `change_box_count_detail` | Change-box audit / MA | count | R count/lines; W view audit | `management_change_box_count_detail.html`; UI | Audits / Change Box |
| POST `/management/change-box-count/{count_id}/delete` — `change_box_count_delete` | Change-box audit / AD | count | Hard delete count/lines + audit | 303→list; action | Retention decision |
| GET `/management/change-forms` — `change_forms_page` | Change forms / MA | store | R forms/stores | `management_change_forms.html`; UI | Customer & Forms / Change |
| GET `/management/change-forms/{submission_id}` — `change_form_detail` | Change forms / MA | submission | R form/lines; W view audit | `management_change_form_detail.html`; UI | Customer & Forms / Change |
| GET `/management/change-box-audit` — `change_box_audit_page` | Change audit / AD | store | R current inventory/settings/stores | `management_change_box_audit.html`; UI | Audits / Cash |
| GET `/management/exchange-return-forms` — `exchange_return_forms_page` | Exchanges / MA | store, from/to | R forms/stores | `management_exchange_return_forms.html`; UI | Customer & Forms / Exchanges |
| GET `/management/exchange-return-forms/{form_id}` — `exchange_return_form_detail` | Exchanges / MA | form | R form/store; W view audit | `management_exchange_return_form_detail.html`; UI | Customer & Forms / Exchanges |
| POST `/management/change-box-audit/{store_id}/submit` — `change_box_audit_submit` | Change audit / AD | store, auditor, target, denomination quantities | W audit/lines/current inventory/settings/audit log | 303→audit; action | Cash domain atomic cutover |
| GET `/management/master-safe-audit` — `master_safe_audit_page` | Master safe / AD | par-saved flag | R/W lazy safe inventory/settings/par | `management_master_safe_audit.html`; UI | Audits / Master Safe |
| POST `/management/master-safe-audit/par-levels/save` — `master_safe_par_levels_save` | Master safe / AD | dynamic denomination pars | W par/audit | 303→safe; action | Cash configuration |
| POST `/management/master-safe-audit/submit` — `master_safe_audit_submit` | Master safe / AD | auditor, target, denomination quantities | W audit/lines/current inventory/settings/audit log | 303→safe; action | Global safe current/history |
| GET `/management/non-sellable-stock-take` — `non_sellable_stock_take_page` | Non-sellable audit / MA | store | R takes/items/stores | `management_non_sellable_stock_take.html`; UI | Inventory / Non-sellable |
| GET `/management/non-sellable-stock-take/{stock_take_id}` — `non_sellable_stock_take_detail` | Non-sellable audit / MA | take | R take/lines; W view audit | `management_non_sellable_stock_take_detail.html`; UI | Inventory / Non-sellable |
| POST `/management/non-sellable-stock-take/{stock_take_id}/unlock` — `non_sellable_stock_take_unlock` | Non-sellable audit / MA | take | W status DRAFT/audit | 303→detail; action | Permission inconsistency decision |
| POST `/management/non-sellable-stock-take/items/create` — `non_sellable_item_create` | Non-sellable catalog / AD | name | W item/audit | 303→list; action | Admin / Inventory Catalog |
| POST `/management/non-sellable-stock-take/items/{item_id}/deactivate` — `non_sellable_item_deactivate` | Non-sellable catalog / AD | item | W active/audit | 303→list; action | Preserve history FK behavior |
| GET `/management/customer-requests` — `customer_requests_page` | Requests / MA | store, from/to | R submissions/lines/items/stores | `management_customer_requests.html`; UI | Customer & Forms / Requests |
| POST `/management/customer-requests/items/create` — `customer_requests_item_create` | Requests admin / MA | name | W item/audit | 303→requests; action | Broad permission; investigate |
| POST `/management/customer-requests/items/{item_id}/count` — `customer_requests_item_set_count` | Requests admin / MA | item, request count | W aggregate count/audit | 303→requests; action | Clarify curated vs derived count |
| GET `/management/employee-logs` — `employee_logs_page` | Employee logs / EL | employee, from/to, status flags | R employees/categories/entries; W lazy categories | `management_employee_logs.html`; UI | Audits / Employee Logs |
| POST `/management/employee-logs/entries` — `employee_log_entry_create` | Employee logs / EL | employee, category, note | W entry/audit | 303→logs; action | Audits / Employee Logs |
| POST `/management/employee-logs/employees/add` — `employee_logs_employee_add` | Employee admin / ELA | full name, visible-to-leads | W employee/audit | 303→logs; action | Admin / Employee Logs |
| POST `/management/employee-logs/employees/{employee_id}/save` — `employee_logs_employee_save` | Employee admin / ELA | employee, name, visibility, active | W employee/audit | 303→logs; action | Admin / Employee Logs |
| POST `/management/employee-logs/employees/{employee_id}/deactivate` — `employee_logs_employee_deactivate` | Employee admin / ELA | employee | W active/audit | 303→logs; action | Admin / Employee Logs |
| POST `/management/employee-logs/categories/add` — `employee_logs_category_add` | Employee admin / ELA | label | W category/audit | 303→logs; action | Admin / Employee Logs |
| POST `/management/employee-logs/categories/{category_id}/save` — `employee_logs_category_save` | Employee admin / ELA | category, label, active | W category/audit | 303→logs; action | Preserve entry label snapshot |
| POST `/management/employee-logs/categories/{category_id}/deactivate` — `employee_logs_category_deactivate` | Employee admin / ELA | category | W active/audit | 303→logs; action | Admin / Employee Logs |
| GET `/management/audit-queue` — `audit_queue_page` | Audit placeholder / MA | none | None | `management_placeholder.html`; UI, **placeholder** | Audits; candidate retirement only after usage evidence |

## Reports and exports

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/management/reports` — `reports_page` | Reports hub / MA | none | None | `management_reports.html`; UI | Reports hub; template hides most links except literal ADMIN |
| GET `/management/reports/master-safe-change-usage` — `master_safe_change_usage_report_page` | Cash report / AD | start/end, store | R change-form history/stores | `management_master_safe_change_usage_report.html`; UI | Reports / Cash |
| GET `/management/reports/count-square-sync` — `count_square_sync_report_page` | Sync report / RA | store, from/to, session, sync scope | R sync events/count/store | `management_count_square_sync_report.html`; UI | Audits / Integration Health; two hub entries |
| GET `/management/reports/recount-changes` — `recount_change_report_page` | Recount report / AD | store, from/to; derived line filters | R session/snapshot/entry history | `management_recount_change_report.html`; UI | Audits / Inventory |
| GET `/management/reports/cogs` — `reports_cogs_page` | COGS / MA | start/end | R stores/mappings + live Square | `management_cogs_report.html`; UI | Reports / Finance |
| GET `/management/reports/sales-transactions` — `reports_sales_transactions_page` | Sales / RA | Square location IDs, start/end | Live Square locations/orders | `management_sales_transactions_report.html`; UI | Reports / Sales |
| GET `/management/reports/gross-sales-by-store` — `reports_gross_sales_by_store_page` | Sales / RA | locations, start/end | Live Square locations/orders | `management_gross_sales_by_store_report.html`; UI | Reports / Sales |
| GET `/management/reports/sales-by-vendor` — `reports_sales_by_vendor_page` | Sales / RA | locations, start/end, vendor | R vendor mappings + live Square | `management_sales_by_vendor_report.html`; UI | Reports / Vendor |
| GET `/management/reports/employee-sales` — `reports_employee_sales_page` | Sales / RA | locations, start/end | Live Square orders/payments/team | `management_employee_sales_report.html`; UI | Reports / People |
| GET `/management/reports/gross-sales-by-store/export.csv` — `reports_gross_sales_by_store_export_csv` | Sales export / RA | locations, start/end | Live Square | CSV stream; export | Preserve filename/columns/timezone |
| GET `/management/reports/employee-sales/export.csv` — `reports_employee_sales_export_csv` | Sales export / RA | locations, start/end | Live Square | CSV stream; export | Preserve attribution/calculations |
| GET `/management/reports/sales-by-vendor/export.csv` — `reports_sales_by_vendor_export_csv` | Sales export / RA | locations, start/end, vendor | R mappings + live Square | CSV stream; export | Current mapping affects history |
| GET `/management/reports/sales-transactions/export.csv` — `reports_sales_transactions_export_csv` | Sales export / RA | locations, start/end | Live Square | CSV stream; export | Reports / Sales |
| GET `/management/reports/stock-value-on-hand` — `reports_stock_value_on_hand_page` | Inventory value / AD | optional store | R stores + live Square catalog/on-hand | `management_stock_value_on_hand.html`; UI | Reports / Inventory |
| GET `/management/reports/targeted-sku-demand` — `reports_targeted_sku_demand_page` | Demand / RA | query/search, variation IDs, lookback, target days, store, end date | R stores/vendors/mappings + live Square | `management_targeted_sku_demand.html`; UI | Reports / Inventory Demand |
| GET `/management/reports/targeted-sku-demand/export.csv` — `reports_targeted_sku_demand_export_csv` | Demand export / RA | same selection filters | Same live/local reads | CSV stream; export | Preserve summary/detail rows |
| GET `/management/reports/inventory-velocity` — `reports_inventory_velocity_page` | Inventory analytics / RA | days, end, store, target days and display filters | R stores/vendors/mappings + live Square | `management_inventory_velocity.html`; UI | Reports / Inventory |
| GET `/management/reports/stock-coverage-purchase` — `reports_stock_coverage_purchase_page` | Purchase analytics / RA | days, end, target months, top N, filters | R stores/vendors/mappings + live Square | `management_stock_coverage_purchase.html`; UI | Reports / Ordering Planning |
| GET `/management/reports/stock-coverage-purchase/export.csv` — `reports_stock_coverage_purchase_export_csv` | Purchase export / RA | same filters | Same live/local reads | CSV stream; export | Reports / Ordering Planning |
| POST `/management/reports/stock-coverage-purchase/create-order` — `reports_stock_coverage_purchase_create_order` | Report→order / RA | selected dynamic row/vendor/store values | R report refs; W PO/lines/allocations/audit | 303→PO detail/report error; action | Ordering / Generate; preserve exact snapshot |
| GET `/management/reports/inventory-velocity/export.csv` — `reports_inventory_velocity_export_csv` | Inventory export / RA | report filters | Same live/local reads | CSV stream; export | Reports / Inventory |
| GET `/management/reports/stock-value-on-hand/export.csv` — `reports_stock_value_on_hand_export_csv` | Value export / AD | store | R stores + live Square | CSV stream; export | Reports / Inventory |

## Users, access controls, sessions, groups, and system administration

| Method/path — function | Module / permission | Inputs | DB reads/writes | Response, class, status | V2 / cutover note |
|---|---|---|---|---|---|
| GET `/management/users` — `users_page` | Users / US | none | R management principals | `management_users.html`; UI | Admin / Users |
| POST `/management/users/create` — `create_user` | Users / US | username, password, role | W principal/audit | 303→users; action | Admin / Users |
| POST `/management/users/{target_principal_id}/status` — `set_user_status` | Users / US | principal, active | W principal/audit | 303→users; action | Preserve no-delete behavior |
| POST `/management/users/{target_principal_id}/password` — `set_user_password` | Users / US | principal, new password | W password/audit | 303→users; action | Admin / Users |
| GET `/management/access-controls` — `access_controls_page` | Access / US | saved flag | R principals/stores/role+principal overrides | `management_access_controls.html`; UI | Admin / Access |
| POST `/management/access-controls/roles/save` — `access_controls_save_roles` | Access / US | dynamic role+permission flags | W role overrides/audit | 303→access; action | Preserve principal>role>fallback precedence |
| GET `/management/access-controls/roles/{role}/categories` — `access_controls_role_categories_page` | Access/dashboard / US | role, saved | R categories/role visibility | `management_access_role_categories.html`; UI | Admin / Navigation Access |
| POST `/management/access-controls/roles/{role}/categories/save` — `access_controls_role_categories_save` | Access/dashboard / US | role, dynamic category flags | W role category access/audit | 303→role categories; action | Visibility, not backend auth |
| POST `/management/access-controls/principals/save` — `access_controls_save_principals` | Access / US | principal IDs, custom labels, dynamic overrides | W principal labels/overrides/audit | 303→access; action | Admin / Access |
| GET `/management/sessions` — `list_sessions` | Count history / MA | none | R sessions/stores/campaigns | `management_sessions.html`; UI | Inventory / Count History |
| POST `/management/sessions/delete` — `delete_sessions` | Count admin / AD | session IDs | Hard delete/cascade sessions + audit | 303→sessions; action | Retention/cascade audit before V2 |
| GET `/management/groups` — `groups_page` | Count config / GP | status flags | R campaigns/groups/stores/rotation/store logins | `management_groups.html`; UI | Admin / Count Configuration |
| GET `/management/groups/audit-count-groups` — `audit_count_groups_page` | Count config audit / GP | none | R groups/campaigns + live Square; W audit log | `management_group_coverage_audit.html`; UI | Audits / Configuration |
| POST `/management/groups/create` — `create_group` | Count config / GP | name, campaign IDs | W group/junction/audit | 303→groups; action | Admin / Count Configuration |
| POST `/management/stores/{store_id}/credentials` — `update_store_credentials` | Store credentials / GP | store, username, password | W store principal/audit | 303→groups; action | Move to Admin / Stores & Users |
| POST `/management/password/reset` — `reset_password` | Account / GP | current/new/confirm password | R/W own principal/audit | 303→groups; action | Shared account settings; permission label mismatch |
| POST `/management/groups/{group_id}/update` — `update_group` | Count config / GP | group, name, campaign IDs | W group/junction/audit | 303→groups; action | Admin / Count Configuration |
| POST `/management/groups/{group_id}/delete` — `delete_group` | Count config / GP | group | W soft deactivate/unassign/audit | 303→groups; action | Preserve historical FKs |
| POST `/management/groups/renumber` — `renumber_groups` | Count config / GP | dynamic positions | W positions/audit | 303→groups; action | Rotation order behavior |
| POST `/management/groups/sync-campaigns` — `sync_campaigns_from_square` | Campaign sync / GP | min items, deactivate missing | R Square catalog; W campaigns/audit | 303→groups; action/sync | Admin / Integrations |
| POST `/management/stores/{store_id}/set-next-group` — `set_next_group` | Rotation / GP | store, group | W rotation/audit | 303→groups; action | Admin / Store Count Rotation |
| GET `/management/sessions/{session_id}` — `view_session` | Count history / MA | session; push/recount status query fields | R session/snapshot/entries/sync; W view audit | detail template or 303 if store route needed; UI | Inventory / Count Detail; preserve redirect logic |
| POST `/management/sessions/{session_id}/push-to-square` — `push_session_to_square` | Count sync / RA | session | R count; W sync/audit; Square inventory | 303→detail; action | Late Square-write cutover; literal role |
| POST `/management/sessions/{session_id}/push-recount-to-square` — `push_session_recount_to_square` | Count sync / RA | session | R recount count; W sync/audit; Square inventory | 303→detail; action | Same destination, recount scope |
| POST `/management/sessions/{session_id}/force-recount` — `force_recount` | Recount / AD | session | W forced count/audit | 303→detail; action | Inventory / Count Actions |
| POST `/management/sessions/{session_id}/unlock` — `unlock` | Count recovery / MA | session | W status DRAFT/audit | 303→detail; action | Broad permission decision gate |
| GET `/management/sessions/{session_id}/export.csv` — `export_csv` | Count export / MA | session | R snapshot/entries; W export audit | CSV stream; export | Reports / Counts; GET side effect |

## Duplication, bypasses, legacy, and cutover notes

- Rotating counts and full admin store counts duplicate snapshot/draft/submit/export/Square-push concepts with separate schemas/services.
- Count Push Trend and Square Recount Push are duplicate hub entry points into one route with different `sync_scope`.
- Inventory velocity, stock coverage, targeted demand, ordering generation, and stock-value reports repeatedly fetch overlapping Square catalog/on-hand/sales data.
- Cash fetch routes return Python dict/list responses directly rather than using a dedicated API router/schema.
- Barcode scan/cancel and cash endpoints are API-like but use session cookies and form CSRF, not API authentication.
- Many routes bypass a shared service for response formatting, parsing, CSV construction, or direct query joins; all sales CSVs and the session CSV are router-built.
- Literal `Role.ADMIN` routes bypass the configurable capability system. See permission map for exact impact.
- `management_placeholder.html` is used only by Audit Queue. `/store/opening-checklist/submit` GET is an explicit legacy/compatibility redirect.
- Vendor contacts, receipt tables, PO email fields, and several PO status values have no route owner; this does not prove they are unused in production.
- Every active V1 user-facing route needs either a stable compatibility URL or an explicit redirect at cutover. Preserve record IDs/query filters for deep links. Draft/session/PO routes should remain pinned to the version that created the record until completion.
