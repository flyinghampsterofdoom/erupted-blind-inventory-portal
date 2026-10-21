# Purchasing V3 UI redesign — local owner review

Date: 2026-10-17. Status: implemented and verified locally; awaiting owner review. No deployment or production changes.

Preview: http://127.0.0.1:8763/management/purchasing/sessions/4 . Purchasing home supports Generate Order and fresh drafts. The isolated preview uses 508 synthetic catalog SKUs, 506 recommendations, three suppliers, four stores, mocked Square reads, and blocked external mutations and financial writers. Existing login and CSRF remain active. Preview credentials are supplied separately, not stored in this report.

## Design and implementation

Inspected the actual local V2 overview screen and reused `v2/base.html`, central navigation, destination registry, typography, buttons, cards, and existing color/spacing tokens. Purchasing appears under Inventory with the existing Phase 2 feature flag and owner permission boundary. Screenshots include the inspected V2 reference.

Replaced oversized product cards with collapsible supplier tables. Rows measured 41 px; 16 complete rows are visible at 1440 × 1080. Each row displays product/SKU, configured active store needs, total calculated need, editable order quantity, unit cost, supplier, and remove. Expand a row for calculation evidence and intended allocations. Configured costs remain authoritative; provisional owner costs can be edited. Supplier headers show product count, total units and estimated cost, with intended payment account and Create PO controls.

Search supports name/SKU; filters support supplier and store need; sorting supports quantity, cost and calculated need. Each expanded supplier loads a bounded 20/50/100-row page. Complete supplier totals are computed over the saved draft, separately from filtered counts. Collapsed groups do not render product components. Sticky headers and horizontal table scrolling were verified. At 390 × 844 the filter drawer collapses, and the page does not overflow.

Calculated store needs, total need, order quantities and final intended allocations remain separate. Manual allocation edits save atomically when their sum matches the order quantity. Existing automatic allocation and recommendation calculations are reused. Active store columns come from configuration; historical allocation destinations remain available even when inactive. A newly configured store has unknown evidence until supported by an approved recommendation snapshot; this UI change does not expand the engine's existing approved store scope.

Inline saves use a serialized queue, server version checks and row locks. Saving, saved and error states are visible. Pending edits are immediately preserved locally using an owner/session-specific key and survive refresh or a new tab. Concurrent versions produce a conflict with saved-versus-pending values and explicit reviewed reapplication; no silent overwrite or automatic replay. Missing acknowledgement after a committed write also requires comparison before reapplication. Blank quantities remain invalid and never silently become zero. Recommendation refresh preserves owner decisions; changed recommendations require explicit acceptance. PO creation verifies the current draft version.

## Verification

| Requirement | Result / evidence |
| --- | --- |
| Actual V2 comparison | PASS — live overview inspected; shared shell and navigation reused |
| At least 500 SKUs | PASS — automated 500-SKU PostgreSQL fixture; browser preview 508 SKUs |
| Four-store calculations | PASS — automated expected needs 59/128/197/266, total 650; browser shows all four columns |
| Inline persistence | PASS — reload/new-tab recovery; failed request, rapid delayed saves, stale-tab conflict, lost acknowledgement, blank input |
| Supplier totals | PASS — supplier move changes both groups' count, units and cost |
| Refresh preserves decisions | PASS — quantity, supplier and manual allocations preserved |
| Final allocation accuracy | PASS — quantity 17, allocations 2/3/4/8, sum 17 |
| PO creation and PDF | PASS — actual preview draft PO 2 has quantity 17, allocations 2/3/4/8 and intended account 1; PDF download succeeds |
| Desktop and smaller viewport | PASS — 16 visible 41 px rows; mobile horizontal scrolling, no page overflow; sticky header verified after 840 px scrolling |
| Permissions and CSRF | PASS — owner/feature guards and invalid field/type checks; preview financial/receiving mutation routes return 403 |
| Production isolation | PASS — local preview funding activity, ledger, Square sync event and receipt counts all zero |

General regression: **972 passed, 444 skipped, 7 subtests passed**, 5 warnings. The 444 skips comprise 443 requiring PostgreSQL-admin integration configuration and one private real-R2 dependency. They are not counted as passes.

Separately, the relevant PostgreSQL integration selection passed **124 tests, no skips**, 4 warnings: purchasing UI, Phase 2, V2 shell, V2 primary, and receiving extension. Counts overlap the general run and must not be added as unique coverage. Two additional UI cases were subsequently included in the final general run. The complete all-PostgreSQL suite was not rerun for this UI task; the previously documented unrelated scheduling incompatibility is outside this evidence. JavaScript syntax, Python compilation and diff whitespace checks pass.

Reproducible test coverage: `tests/test_purchasing_ui.py` covers bounded paging, all-store calculations, filters/sort, quantity/supplier changes, allocation validation, version conflicts, refresh, conversion, dynamic columns, review/zero-quantity views, navigation and permissions. Existing `tests/test_purchasing_phase2.py` continues to exercise HTTP purchasing and PDF behavior. Browser fault injection is confined to the temporary local launcher.

## Preservation and changed files

No new engine, balance authority, model or migration. Hash comparison against the pre-UI working-tree snapshot confirms the math, inventory, PO administration, card balance and classification services are unchanged. The original purchasing session service is byte-for-byte preserved before the appended read-only `table_workspace` presentation function. Existing decisions, Square authority, incoming PO treatment, COGS classification, audits, financial protections and receiving logic remain in their existing services.

Modified: `app/routers/purchasing.py`; `app/v2/navigation.py`; `app/v2/workspaces.py`; `app/templates/purchasing/home.html`; `app/templates/purchasing/layout.html`; `app/templates/purchasing/session.html`; `app/services/purchasing_session_service.py`; `tests/test_purchasing_phase2.py`.

Added: `app/static/v2/purchasing.css`; `app/static/v2/purchasing.js`; `tests/test_purchasing_ui.py`; this review report. Removed obsolete card partial: `app/templates/purchasing/product.html`. Pre-existing Phase 1/2 changes in this worktree are preserved.

## Evidence and remaining limits

Artifacts are in `output/purchasing-ui-redesign/`: `verification.json`, `change-preservation-manifest.json`, `general-regression.log`, `postgres-integration.log`, and six screenshots: `desktop-table.jpg`, `mobile-table.jpg`, `mobile-inline-controls.jpg`, `store-allocation-detail.jpg`, `concurrent-edit-review.jpg`, `v2-reference.jpg`.

The local timings in verification.json are single observations, not production performance benchmarks. Paging avoids rendering thousands of product rows, but catalog-scale production load and cross-browser testing remain deployment prerequisites. Pending local recovery depends on browser storage; confirmed drafts persist in the database. Owner review should walk through Generate Order, supplier substitution, allocations and PO creation with this isolated data. Production rollout requires separate explicit authorization and the existing Phase 2 cutover checks. No Phase 3 work was performed.

Recommendation: ready for local owner UI acceptance review. No deployment authorized or performed.
