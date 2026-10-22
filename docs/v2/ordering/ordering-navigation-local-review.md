# Ordering navigation and PDF visibility — local acceptance

Date: October 21, 2026 (America/Los_Angeles).
Branch: `codex/ordering-navigation-20261021`, based on `fc30db1`.
Scope confirmed by owner: the two improvements in the chat request. Batch saving,
vendor-transfer repairs, and uncertain-evidence workflow changes from the larger
attachment are outside this change.

## Findings

The established PDF renderer is `_generate_purchase_order_pdf` in
`app/services/purchase_order_admin_service.py`. Submission creates the saved
vendor document; the existing `/management/ordering-tool/orders/{id}/pdf` route
retrieves it through `ensure_current_purchase_order_pdf`. Purchasing exposed only
an “Open PO” link for converted groups, and order review exposed a small
“Download Vendor PDF” text link only when a PDF reference existed. A draft PO
without a generated document had no explanation. This was a discoverability gap,
not a missing renderer. Historical document preservation must remain intact.

Square's catalog supplies category IDs and a reporting category. Inventory
counting and campaign code already use those classifications. The local
`ordering_catalog_identity` table has identity fields, but no product-category
column; purchasing's saved evidence discarded the category metadata. Square's
`product_type` field is not used to invent retail groupings.

## Implementation

- Prominent **Print / Export PDF** links in converted supplier review and existing
  PO review, pointing to the established download route. Ineligible orders show
  a disabled action and a visible explanation of creation/submission eligibility.
  No generation, storage, download, PDF template, or submission code was changed.
- Carry Square's reporting category ID through the read gateway. New evidence
  snapshots retain the established classification and category label. Prefer the
  reporting category; otherwise preserve the category memberships. Multiple
  memberships without a reporting category, missing categories, and unresolved
  labels are explicitly identified rather than silently reclassified. IDs, not
  names, distinguish categories with identical labels.
- Older drafts resolve their missing classification through a bulk, read-only
  catalog lookup cached for five minutes per connection. Failed lookups have a
  short retry cooldown and display an availability explanation. They never
  backfill or rewrite old evidence, orders, inventory, or history.
- Supplier tables show category navigation with full matching counts, expandable
  category headings, and the existing bounded 20/50/100-row pagination. A slim
  classification projection avoids loading all recommendation evidence; category
  counts span every matching page. No classification lookup is needed for the
  summary-only workspace. Existing PO eligibility is loaded in one bulk query.
- Search spans categories, clears a prior category selection, exposes matching
  categories, and restores pre-search collapse choices when cleared. Category
  navigation uses the existing save/flush and recovery behavior; no save or
  validation safeguards changed. Collapse/expand is entirely local.
- Desktop controls wrap; mobile category navigation scrolls horizontally and
  retains the existing horizontally scrollable store-allocation table. Supplier
  totals, quantities, and PDF eligibility remain visible above the table.

## Validation

All database tests use newly created, migrated, disposable local PostgreSQL
fixtures with synthetic inventory and mocked Square. No production data was read
or modified, and nothing was deployed.

Final focused suite: **93 passed, 1 skipped**, 42.20 seconds. The skipped test
requires isolated hosted R2 credentials. This includes purchasing UI, purchasing
Phase 2, category navigation, the Square read gateway, PDF preservation and the
permanent Square write boundary. JavaScript empty-table/pagination/retry checks,
syntax validation, and `git diff --check` also passed.

The 500-product acceptance case verifies full category counts, category-specific
pagination, search and empty results, saved allocations, legacy draft projection,
reporting-category precedence, missing/ambiguous categories, duplicate category
names, and bulk lookup reuse. A representative workspace request used 18 SQL
statements, took 276.2 ms locally, and performed zero writes. These are local
fixture measurements, not production latency predictions. Exact table comparisons
before/after navigation/search cover session decisions, sessions, purchase orders,
PO lines, vendor configuration, lifecycle inventory metadata, and audit history.

Browser acceptance used the actual local routes, templates, CSS, JavaScript,
and a disposable 500-product database:

1. Change Andresen allocation on SKU1 from 59 to 7; total updates to 598.
2. Collapse Devices and navigate to that item type; allocation remains 7.
3. Search for a product in E-Liquid from the Devices view; the other category is
   shown automatically.
4. Collapse Devices, search, and clear search; Devices remains collapsed.
5. Reload after saving; Andresen remains 7 and total remains 598.
6. Collapse Devices after all requests settle; **0 network requests** result.
7. Verify desktop (1440×1000) and mobile (390×844) layouts.
8. Download using the visible PDF action. The downloaded bytes match the existing
   renderer output exactly: SHA-256
   `32627ae4ced3b0058a320e9e0d160a0f3238852d8da827d6479e1c853f176a25`.
9. Open that PDF in macOS Preview, invoke Print, verify a one-page US Letter print
   preview, then Cancel. No physical print was submitted.

Evidence directory:
`/Users/justinrawlinson/Developer/Erupted/output/ordering-navigation-20261021/`

- `final-tests.log`, `js-regression.log`: regression evidence.
- `desktop-overview.png`, `desktop.png`, `mobile.png`, `search-desktop.png`:
  screenshots of actual local UI with synthetic records.
- `native-print-preview.png`, `pdf-print-preview.png`: native printing and rendered
  document evidence.
- `browser-requests.json`: local browser HTTP request audit.
- `preview.py`: disposable preview setup for reproduction; authentication overrides
  and mocked services exist only in this external local evidence script.

## Deployment and rollback

Deployment is not authorized. Review the isolated branch, then integrate it with
whatever application revision is current at authorization time, rerun the focused
checks, and deploy application code/static assets together. No schema migration or
production-data repair is required. Smoke-test a saved draft, category navigation,
search, quantities, and an existing PO PDF without submitting a new production PO.

Rollback restores the previous application/static-asset release together. The new
snapshot classification field is additive JSON that older code ignores. Preserve
all existing database records and PDF storage; no data rollback or regeneration
is needed.
