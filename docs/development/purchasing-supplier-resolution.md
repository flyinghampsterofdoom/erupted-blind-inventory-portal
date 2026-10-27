# Purchasing supplier resolution and explicit draft recovery

Purchasing supplier identity is independent of sales COGS and financing accounts.
The resolver uses distinct active vendor identities, active variation mappings,
and the catalog's vendor candidates, in this order:

1. One explicitly designated default supplier, consistent with catalog candidates.
2. With no valid explicit defaults, exactly one active catalog supplier.
3. Otherwise Needs supplier. Conflicting or inconsistent defaults fail closed.

Inactive vendors and mappings cannot establish a default. Multiple default rows
for the same supplier do not manufacture multiple supplier identities; cost
ambiguities remain subject to the existing cost validator. No candidate ordering,
Square write, cost rewrite, financing inference or schema migration is added.

## Existing drafts

The owner can open **Review supplier recovery** from an active purchasing draft.
Unsaved browser edits must first be saved through the existing draft action guard.
Preview uses the most recent saved product evidence and current mappings; it does
not contact Square, run purchasing jobs, or recalculate demand. The owner selects
products, reviews the assignments, and explicitly confirms. Filling missing costs
from displayed current configuration is a separate opt-in; nonnull costs always
remain unchanged, even if current configuration differs.

Recovery excludes removed/ineligible products, already selected suppliers, manual
supplier decisions, ordered/discarded destination groups, any product commitment,
and any product/store quantity present in a linked session PO. It conservatively
protects the whole product rather than splitting a partial commitment.

Apply takes the same session lock as draft/PO operations, rechecks a preview token
and draft version, and validates every selected product before applying changes.
It reuses existing draft batch receipts for idempotency and emits a
`PURCHASING_SUPPLIERS_RECOVERED` audit event with before/after suppliers and costs,
saved quantities, allocations and retained financing account IDs. Exact request
retries replay once; reused keys with different decisions fail. GET never applies
recovery, refresh never applies recovery, and no PO/payment is created.

Historical accepted/proposed evidence and review flags remain unchanged. Recovery
does not acknowledge demand or uncertain evidence. Existing financing decisions
are retained per line. A new supplier group inherits only a unanimous saved account
choice; an existing group's account is never overwritten. Conflicting account
choices require the existing explicit account confirmation before finalization.

## Qualification and rollback contract

Use an exact clean source commit based on the current web/cron revision. Run the
full purchasing/PO/consignment suite, exact-SHA expanded CI, native web and cron
builds, and recovery on an isolated restored read-only production snapshot. Verify
all 172 Big Wholesale mappings/costs and the latest saved draft's 104 lines,
70 positive lines, and 413 saved units without treating those as fresh demand.
Fingerprint all original tables: only session rows/groups/version, recovery batch
receipts and audit events may change in the isolated recovery rehearsal. PO 129,
all product/store commitments, financial and inventory tables must be identical.
The existing production runtime must read the recovered clone unchanged, without
migration, downgrade, reconciliation or backup restoration.

Production recovery is not a deployment side effect. After separate release
authorization, the owner must still explicitly review recovery in each saved draft.
A code rollback preserves recovered decisions and existing orders; newly generated
recommendations under the old runtime would again have the old attribution defect.
Keep cron scheduling/entrypoints, scheduling business code, validation controls,
forecast projection bytes, historical November 1 evidence and current guarantees
unchanged. Do not trigger purchasing, forecast, scheduling or cron jobs to qualify.
