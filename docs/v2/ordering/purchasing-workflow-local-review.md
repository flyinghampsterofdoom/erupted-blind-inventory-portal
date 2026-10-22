# Purchasing workflow corrections — local review

Date: October 21, 2026 (America/Los_Angeles).
Branch: `codex/purchasing-workflow-20261021`, based on the approved deployed
navigation release `771a289`. These workflow corrections are **local only** and
require separate deployment approval.

## Root causes

1. **Per-edit persistence:** the JavaScript staged each keystroke briefly, then
   automatically pumped separate PATCH requests. Each decision committed
   separately. The service's completion check repeatedly loaded the entire saved
   product/evidence collection. Network round trips and repeated full-session
   reads accumulated while the owner entered many quantities.
2. **Supplier changes:** a supplier-only update recalculated an automatic quantity
   using the destination supplier's pack multiple. The existing test encoded a
   280-to-282 change. Supplier movement, quantity changes, and confirmation also
   settled through separate queued saves. A destination with missing cost evidence
   could move a product into review instead of keeping it visible in Purchasing.
3. **Uncertain-evidence rejection:** PO creation requires confirmation when source
   evidence is uncertain OR the purchase cost is not configured. The presentation
   previously omitted nonconfigured-but-positive costs from its review filter.
   Confirmations were tied only to revision numbers: unchanged refreshes discarded
   their validity, while some relevant quantity/cost changes did not. Native form
   submission displayed the API's HTTPException JSON directly.
4. **Saving versus finalization:** automatic saves and native Create PO actions
   obscured the difference between unfinished purchasing decisions and creating a
   purchase order. This also made failed validation interrupt the interface.

A read-only examination of the verified production backup for the reported
session 4 / supplier 36 found **64 positive-quantity products, 17 with uncertain
source evidence, 26 with nonconfigured costs, 26 supplier substitutions, and zero
confirmations for the current revision**. These counts overlap. The validation
was legitimate; the interface failed to identify the affected decisions clearly.
No production record was changed to investigate this incident.

## Implemented behavior

- All quantity, supplier, removal/restore, review and account decisions are staged
  locally. Inputs and totals respond immediately. The prominent **Save Draft**
  action shows the number of unsaved products/account decisions. Browsing,
  searching and changing categories never invoke persistence.
- Save Draft sends one atomic batch with an expected session version and a stable
  request identity. A single locked transaction validates the entire batch and
  commits once. A database receipt, unique to session/request, makes an interrupted
  response safe to retry. A changed payload cannot reuse a committed identity.
- The service bulk-loads affected rows and cost context and performs completion
  checks without loading all saved recommendation evidence. A batch advances the
  externally visible session version once. Per-product audit history is retained.
- Failed saves retain the full local draft and exact retry identity. Conflicts
  show saved versus pending quantities/allocations and require an explicit
  reviewed reapply. Browser recovery and cross-tab save confirmation retain these
  guarantees. Navigation with unsaved work invokes the browser warning.
- Supplier transfers move the working row immediately and preserve its selected
  quantity and store allocations. Save commits assignment and quantities together.
  Destination configured cost is validated at save time. A retained cost with no
  destination configuration stays provisional and requires explicit review.
  Source financing decisions remain attached until explicitly reconfirmed for
  the destination group. No canonical Square vendor or local vendor mapping is
  changed.
- **Review / Finalize PO** opens an inline review of the supplier's entire saved
  draft, including products outside current filters. Affected products show
  quantities, costs, store allocations, reasons, explicit review confirmations,
  and a link to edit the product. A separate visible Review items action exposes
  the review filter. Native validation failures render this interface as HTML;
  JSON errors are consumed by JavaScript and displayed inline.
- Confirmations bind to the selected quantity, allocations, supplier, cost and
  relevant underlying evidence. Unchanged evidence retains confirmation; changed
  evidence or decisions require review again. Confirmation controls pin the cost
  shown to the owner, so a changed configured cost cannot be silently confirmed.
- Save Draft accepts unfinished decisions without creating a PO or PDF.
  **Finalize / Create PO** is a separate explicit action. It creates the existing
  DRAFT purchase order; its established Review/Submit operation generates the
  existing vendor PDF. No additional PDF renderer or automatic submission exists.
- Existing owner/CSRF/version boundaries, active supplier checks, configured-cost
  authority, store allocation validation, frozen financing after submission,
  historical accounting, and Square catalog/vendor read-only policy remain intact.

## Local validation and evidence

Evidence directory:
`/Users/justinrawlinson/Developer/Erupted/output/purchasing-workflow-20261021/`.

All mutations in acceptance testing target newly created disposable PostgreSQL
fixtures containing synthetic products. Production-backup migration rehearsal
uses a separate local clone and changes only schema metadata plus the new empty
receipt table. Existing clone business rows are compared byte for byte.

Browser acceptance demonstrates:

- 30 quantity edits plus three supplier transfers, with zero POST/PATCH requests
  before Save Draft; one batch request persists them together.
- Pending and saved quantities survive searching, category/supplier navigation and
  reload. Three moved products retain their store allocations.
- A simulated 503 before execution leaves the database unchanged and all edits
  recoverable. Retry commits once.
- A simulated lost response AFTER commit is recovered when reopening the page;
  the same request is replayed without adding a receipt or applying updates twice.
- Concurrent tabs retain the losing edit, display a saved-versus-pending comparison,
  and permit overwrite only after explicit reviewed reapply.
- Inline provisional-cost confirmation is saved separately; finalization remains
  disabled until the draft and review decisions are saved.
- Saving leaves PO count unchanged. Explicit finalization creates a DRAFT PO with
  the selected store allocations and quantities intact, in the existing PO editor.
- Desktop and 390-pixel mobile review layouts use the existing shell and no modal.

Key evidence files: `regressions.log/xml`, `js-draft-tests.log`,
`js-table-tests.log`, `browser-requests.json`, `state-*.json`,
`unsaved-transfers-desktop.png`, `concurrent-edit-review.png`,
`failed-save-retained.png`, `actionable-review-desktop.png`,
`ready-to-finalize-desktop.png`, and `finalization-mobile.png`.

Final validation: **446 passed, 1 skipped** in the broad purchasing, accounting,
PDF, schema, permission and scheduling regression suite. The skip requires
hosted-test R2 credentials. After strengthening the confirmation fingerprint,
the final targeted suite passed **67 tests**; the unchanged legacy-form
confirmation regression and portable benchmark also passed separately. Both
JavaScript suites, JavaScript syntax and `git diff --check` pass.

The 30-edit / three-transfer HTTP acceptance case used **24 SQL statements,
74.7 ms, one commit and one version increment**, with no purchase order created.
The portable comparative benchmark used the previous committed service:
**3,494.3 ms / 420 SQL statements / 30 commits** before versus
**103.7 ms / 19 SQL statements / one commit** after. The existing PDF submission
and generation path took **14.9 ms** in a separate synthetic test; repeated
exports were byte-identical. These local measurements are not production latency
predictions. The financial initializer was mocked for the isolated submission
check; unchanged financing semantics were covered by the accounting regressions.

The comparative persistence benchmark uses the actual previous service on the
same synthetic 500-product fixture: 30 individual transactions versus one batch.
It measures service/database time, not WAN latency or browser paint. Exact final
regression totals and timing evidence are recorded in the accompanying logs.

## Migration and rollout proposal — not executed

The workflow introduces **schema 48**, adding only `purchasing_draft_batches` for
atomic save receipts. No historical purchasing rows are backfilled or rewritten.
A local clone rehearsal preserved all **197 pre-existing non-version tables**
exactly and created an empty receipt table.

Current deployed web and cron binaries support schema 47 only. Compatibility
builds have therefore been prepared locally:

- Web rollback/bridge: `3c343f3`, based on deployed `771a289`, with only schema 48
  added to the allowed revision set.
- Cron bridge: `6834a96`, based on unchanged cron `8073b0f`, with the same single
  compatibility change. No scheduling behavior or cadence changes.

Both compatibility builds passed read-only checks on schemas 47 and 48. The new
workflow passed the same checks on 48. See `migration-rollback-acceptance.json`.
These builds are not deployed or pushed as part of the approved navigation release.

After separate owner approval, recheck the current production revision/config,
create and restore-verify a fresh backup, run the final regression gates, deploy
compatible web and cron bridges without changing cron cadence or triggering an
extra scheduling run, apply the additive migration, and deploy the reviewed
workflow commit. Application code and assets must move together. Verify runtime
identity, preserved historical rows, and read-only production behavior. Do not
create test purchasing records or submit production POs as a smoke test.

Rollback deploys compatible web bridge `3c343f3`, retaining schema 48 and all save
receipts, orders, audit evidence and PDF objects. Keep the compatible cron build.
**Do not downgrade/drop the receipt table or restore an older purchasing database
to roll back application code.** The unmodified schema-47 binaries are not suitable
for startup after this migration.

The existing missing-original PDF storage issue recorded in the navigation
release report remains separate. These workflow corrections do not recreate or
replace historical PDFs.
