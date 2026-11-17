# Purchasing V4 native PDF — validation and deployment review

Implemented on deployed baseline 029509fe018434f2705a2117c127934d5ad89f68. No deployment was performed.

V4 now exposes Print / PDF in the worksheet toolbar and on recorded purchase orders in the existing Purchases workflow. A vendor selector opens a document preview with Print, Download PDF and Preview PDF. Drafts wait for normal pending saves and request the exact saved worksheet revision; issued POs use their authorized snapshot. Search, paging and collapsed groups do not truncate a vendor document. Zero quantities are excluded. Incomplete drafts and vendors without positive quantities are identified in the selector.

The implementation reuses V4's existing ReportLab engine and layout primitives and preserves legacy company/contact details, SKU/vendor SKU, pack conversions, unit and extended costs, and vendor-specific/generic legal disclaimers. It includes date, PO number, total units and total cost. Direct-order aggregate half-up rounding follows V4; approved quotation line totals and freight/tax/discount are taken from the saved financial snapshot, preserving the existing commitment. Unknown costs remain Unknown.

Document GET routes are owner-scoped and run in a database-enforced repeatable-read read-only transaction. They do not invoke purchasing generation, commitment, receiving, inventory, email, original-artifact replacement or status transitions. Issued email artifacts and their worker-safe projections remain unchanged. No production dependency changed.

Dependency fix: pypdf 6.21.4 was missing from both the pinned Python environment and the declared test requirements. Added it only to requirements-test.in and the hash-pinned requirements-test.txt. Its required typing-extensions dependency was already pinned. The local run reused the matching bundled pure-Python package after checking its RECORD hashes; the shared Python installation was untouched. The launcher was qualified for exactly the two test-requirement paths; inherited write confinement, native quotas, hooks and regression supervision were preserved.

Validation passed:
- 8 PDF/layout and persisted V4 HTTP tests: required fields, exact costs/rounding, quotation components, unknown costs, zero exclusion, multiple vendors, repeated headers, extreme long labels, manual quantity edits, stale revisions, authentication, inline/attachment responses and database fingerprints.
- 12 canonical production-authorization/legacy parity tests: real disposable PostgreSQL, exact authorization, duplicate/replay behavior, owner permissions, financial/receiving compatibility and matching legacy document semantics.
- 6 existing canonical receiving-worksheet tests plus 1 local acceptance test against frozen actual production V4 records.
- 2 PDF browser/build checks, 12 existing vendor submission checks, and 4 existing worksheet navigation checks. Strict TypeScript and the production frontend build passed. Chromium tested the actual chooser, asynchronous saves, new tabs, native-print action, PDF links, mobile preview and row pagination in browser print output.
- Native Safari preview and native Print dialog were checked. Safari initially split HTML table rows; print-specific grid rows fixed this and the multipage preview was rechecked. No physical print job was sent.
- Poppler rendered the actual V4 PDF for visual inspection. Long names, costs, rows, page footer and totals were readable.
- git diff --check passed.

Actual production data acceptance used SELECT-only SSH/database reads, not a production purchasing action. The source transaction was database-enforced read-only; protected purchasing/inventory fingerprints were unchanged.
- Coastal Clouds PO #142: 71 lines, 550 units, $4,345.00, 5 native PDF pages. Compared against the existing legacy renderer over an in-memory mirror of the same captured order and actual vendor legal terms. Product, variation, identifier, pack/unit quantities, prices, total and disclaimer content were preserved.
- Streamline saved draft: 11 positive manually adjusted rows, 440 units, 13 zero rows excluded; 1 PDF page.
- Fresh Farms: 55 unresolved quantity rows; correctly rejected as an incomplete draft.
- BIG Wholesale: 1 unresolved quantity row; correctly rejected as an incomplete draft.
Live snapshots and PDF examples are local review artifacts and are excluded from the source commit.

Deployment preparation: source and generated frontend assets are prepared as one local commit on codex/ava-erupted-v4-pdf-review-20261116. The exact commit ID is recorded in the accompanying local validation manifest and final chat response. No application configuration, migration, purchasing calculation, PAR/LEVEL, commitment, receiving behavior or inventory balance is changed by this feature. Release transport/hosted deployment must target the validated commit; an authenticated post-deployment smoke check remains a release step.

Limitations: incomplete drafts require the existing quantity/pack review before they can be printed. Very long single rows that exceed a PDF page continue without dropping text. Native browser print and the downloadable PDF may use different page counts. Safari was checked natively; Chrome compatibility was exercised through Chromium. No hosted PDF route has been smoke-tested because this revision has not been deployed.

Storage: at the owner's explicit direction in this chat, the hard-cap policy review was deferred while bounded PDF work continued. No other project's source, database, backup, device symbols or retained records were deleted. The current numeric accounting remains reported separately; this work does not claim machine-wide hard enforcement of legacy writers.
