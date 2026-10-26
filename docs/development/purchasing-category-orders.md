# Category purchase-order drafts

Production baseline: `df191239cf48ba46e04c50cf06eed354201f6326`, confirmed with read-only Render GETs on October 26, 2026. Production schema remains `20261026_0051`; this change has no migration or cron configuration change.

An inactive item-type button filters its existing supplier table. Reclicking the selected button opens Create Purchase Orders, Review Existing Orders when applicable, and Cancel. The menu explicitly states that creation covers all suppliers' saved quantities for that category, irrespective of search, pagination, store, supplier, or review filters. All Item Types covers all remaining categories. Unsaved edits must be saved first.

The category endpoint locks the purchasing session and requires its current saved version. It validates every affected supplier before using the existing PO review and draft writer. Every product preserves its supplier, quantity, purchase cost and complete store-allocation map. Creation records only DRAFT orders, allocations, session commitments and existing audit events; it does not submit, email, generate supplier transmission, receive inventory, or initialize financial records.

Each product's existing decisions JSON retains its PO identity, committed quantity, allocations and intended account. This is an immutable purchasing decision: subsequent category actions and legacy supplier finalization exclude it. Draft edits, transfers, removal, discard and recommendation refresh cannot unlock that commitment. New purchasing decisions use the existing fresh-recommendation workflow; cancellation or edits within an existing PO do not silently recommit its original session quantities.

Supplier groups stay open while other products remain uncommitted. Category status counts committed and pending positive quantities independently. Original converted supplier groups remain recognized without backfilling historical data. Existing order detail, history deletion protection and financing initialization resolve every split draft's original Purchasing context. Financial calculations and financial writers are unchanged. Intended account changes while a group is open apply to remaining decisions; existing category drafts retain their own account snapshots. The existing completed-group account action remains explicit and rejects submitted POs.

## Qualification

`tests/test_purchasing_category_orders.py` uses disposable PostgreSQL and synthetic suppliers/products across Disposable, Freebase and Salt. It verifies category isolation, two-supplier grouping, four-store allocation preservation, independent and partial creation, all-type completion, repeats and stale versions, overlapping legacy finalization, concurrent duplicate prevention, atomic category failure, frozen edits and refresh, existing converted groups, account snapshots, history deletion protection, CSRF and unknown categories. Financial, inventory, supplier configuration and original PO records are compared before and after draft generation.

Browser acceptance uses the real local FastAPI routes and JavaScript with synthetic PostgreSQL data and mocked Square inventory. Desktop 1440×1000 and mobile 390×844 checks cover filter clicks, selected-button menus, keyboard dismissal, Cancel, existing-order links, disabled repeat creation, three successive category actions, and viewport-contained mobile dropdowns. Screenshots and test logs are retained outside Git under `output/purchasing-category-orders-20261026`.

The complete repository release test matrix is required in addition to targeted acceptance. Exact final-commit GitHub CI, candidate-bound advisory artifact preflight, source identity and protected release checks still apply; local tests alone are not deployment authorization evidence.

## Deployment and rollback boundary

Do not deploy this source with the currently installed advisory archive: its manifest is bound to the previous exact web candidate. Follow `docs/development/advisory-artifact-preflight.md` and the protected release coordinator. Preserve the frozen projection bytes, installer, execution flags, cron revision and schedule.

A rollback after any category draft has been created must retain per-product commitment recognition, exclusion from legacy finalization, and split-order context/history protections. The unmodified previous web application does not understand partially committed groups and is therefore not a safe rollback. Prepare and qualify a compatibility rollback before activation. Do not remove commitments, restore business tables, or bypass the release checks to obtain a rollback.
