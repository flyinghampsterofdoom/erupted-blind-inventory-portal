# Purchasing V3 — direct store receiving and partial shipments

> Update: The outstanding confirmation/browser checks are resolved by `purchasing-v3-final-receiving-ui-review.md`. The report below records the earlier implementation state.

2026-10-17. Implemented locally for owner review. No deployment, production data changes or new migrations.

The owner's clarified workflow is implemented as **Create PO → Vendor PDF → Count directly into stores → Submit to Stores → Receive Remaining Items when later shipments arrive**. The prior report's central-receiving/transfer gap is superseded by this clarification; no warehouse or transfer stage is required or implemented.

Local preview: http://127.0.0.1:8763/management/purchasing/orders/4/receiving . This is a synthetic 22-SKU PO. It has three independently recorded shipments, 180 total units received, and 400 units still open on other SKUs. Square inventory responses are mocked; external networking and financial writers remain blocked by the temporary preview launcher. The existing purchasing interface remains at `/management/purchasing/sessions/4`.

## What already existed and what changed

| Capability | Existing implementation verified | Targeted change |
| --- | --- | --- |
| Store recommendations and owner allocation edits | Existing V3 session service, saved allocation maps and quantity-sum validation | Preserved unchanged; no recommendation/forecast changes |
| Consolidated supplier PO and intended account | Existing PO conversion and attribution | Preserved unchanged |
| Vendor PDF | Existing supplier-facing consolidated quantities and pack annotations | Existing generator preserved; receipt timestamps no longer mark an already-issued PDF stale |
| Per-store receipts and partial deliveries | Existing receipt/receipt-line/destination event models, aggregate received totals and outstanding commitments | Reused; one owner submission records and posts the new shipment |
| Multiple shipments | Existing append-only events and request keys | Added visible Receive Remaining Items, blank new-count cells, previous/outstanding references and derived receiving statuses |
| Square posting and retry | Existing exact durable commands, idempotency keys, owner checks, advisory serialization and uncertain-outcome recovery | Added shipment-specific selection so a submission never posts another saved shipment |
| Quantity limits | Negative counts rejected; PO lock existed | Enforced cumulative supplier-line capacity across typed receipts, barcode scans, corrections and closures; competing sessions cannot consume the same remainder |
| Original order preservation | Historical receipt evidence retained | Prevent received lines being removed or their original order/intended plan rewritten; close shortages explicitly instead |
| Receiving UI | Legacy spreadsheet with separate Save Receiving and Push actions | Native V2 compact table with one Submit to Stores completion action and recovery only when needed |
| Incoming supply after actual split differs | Existing shared incoming definition subtracted receipts per destination | Cap remaining destination weights at the supplier line's actual unfilled balance using the existing integer allocation utility; no fictitious incoming units |

Sources: `app/services/purchase_order_admin_service.py` (`save_purchase_order_received_quantities`, `_validate_receipt_delta`, `submit_receiving_shipment`, `_prepare_receiving_operations`, `receive_purchase_order`, `receiving_workspace`, `close_receiving_shortages`); `app/routers/purchasing.py`; existing PO routes in `app/routers/management.py`; `app/services/purchasing_inventory_service.py`; existing receipt/PO/SquareSyncEvent models in `app/models.py`.

## Owner workflow

The existing placed-order editor links to the native receiving screen when Phase 2 is enabled. A draft must still be placed through the existing Submit (In Transit) action; creating a draft or generating its PDF does not alter stock.

The count table shows product/SKU, ordered quantity, previous receipts, outstanding quantity, dynamic store columns, New Received and Remaining. Store inputs begin blank, with intended / previously received values shown only as reference. They are not assumed received. Actual destination quantities may differ from the intended split, provided their cumulative supplier-line total does not exceed the unclosed original order balance. Explicit zeros are retained in the shipment audit payload and produce no stock commands.

Submit to Stores records the delivery, then invokes the existing authorized inventory posting workflow for that shipment only. The receipt is durable before HTTP begins. Each destination receives an exact delta command with its own durable Square idempotency key. A second submission with the same identity and values returns the original event and skips successful commands; reusing the identity for different values is rejected. After network interruption, a saved event is not recounted. Failed or uncertain commands remain visible in history and recover using the original command, key, quantity and timestamp.

Receive Remaining Items opens a fresh blank count on the same PO with cumulative previous receipts and outstanding values. It does not copy earlier counts into new input cells. More than two shipments are supported. Row locking refreshes stale ORM values before capacity validation, and the existing advisory lock prevents simultaneous posting of the same PO.

Draft counts persist immediately on the same browser/device and survive refresh or navigation. Other-tab changes block stale submission. Pagination renders at most 50 count rows; search retains counts outside the current page. Tab follows store-cell order. Invalid or over-balance values disable submission; server validation independently enforces the same limit. Clear unsubmitted count and Close with Shortage use explicit inline confirmation. Browser storage is draft protection, not an inventory authority.

## Statuses and compatibility

No new database status or schema is needed. User-facing states derive from ordered, received and explicitly closed quantities:

- Awaiting Receipt: no received quantities, open remainder.
- Partially Received: received quantities with open remainder.
- Fully Received: all ordered units received, no shortage closure.
- Closed with Shortage: no open remainder because the owner explicitly closed missing units.

Underlying existing lifecycle values remain IN_TRANSIT while quantities are outstanding, RECEIVED_SPLIT_PENDING when all quantities are accounted for but posting remains pending, and COMPLETED when no quantity or posting remains open. Inventory posting state is always displayed separately from physical count status. An explicitly closed shortage is never silently reopened by a later delivery.

Legacy aggregates remain historical evidence and are never converted into commands. Newly submitted shipments carry submission intent in the existing immutable request payload. Shipment-specific recovery accepts that intent or an existing durable command; older saved counts with no posting intent are not activated by this new route. Existing correction commands remain recoverable. The legacy saved-count tools remain an explicit compatibility link (`?legacy_receiving=1`) on the existing editor, with the new cumulative capacity protections. Their two-step controls are not the default Phase 2 owner workflow. Historical modern saved counts requiring deliberate resolution remain visible rather than silently posted.

The original PO quantities, intended store allocations and PDF are preserved after receiving begins. The existing editor now rejects quantity changes/removal of received lines; use explicit shortage closure. This is a deliberate compatibility change required to preserve the original order across shipments. Existing receiving corrections and append-only audit evidence remain intact.

## Verification evidence

Automated receiving tests in `tests/test_purchasing_shipments.py` cover:

- Full receipt across three shipments; 100-unit first SKU received as 90 + 5 + 5, second SKU initially zero then 80.
- Same-event duplicate submission without duplicate Square commands.
- Zero shipment audit evidence, explicit shortage closure and duplicate closure.
- Actual split differing from plan, capped incoming supply and unchanged intended allocations.
- Negative, fractional, boolean, duplicate-cell, invalid-destination and cumulative over-receipt rejection.
- Competing counts serialized against the same remaining balance.
- Barcode and correction capacity checks, including explicitly closed shortages.
- Uncertain and definitive Square failures, exact-command recovery, and no posting of older unrelated saved counts.
- Existing PDF bytes/path and original quantities retained across receipts.
- Owner permissions, CSRF, route validation and absence of actual-balance/ledger side effects.

Relevant PostgreSQL/V2 integration selection: **166 passed**, no skips, 4 warnings. Subsequent final receipt-integrity selection after the submission-intent refinement: **76 passed**, no skips, 4 warnings. Final focused barcode/incoming guards: **10 passed**, no skips, 3 warnings. These selections overlap and must not be summed as unique coverage. Final general regression: **981 passed, 444 skipped, 7 subtests passed**, 5 warnings. Of the skips, 443 require PostgreSQL-admin integration configuration and one requires private real-R2 access; none is counted as a pass. Log: `output/purchasing-receiving-shipments/general-regression.log`. The existing all-PostgreSQL scheduling suite was not rerun.

Browser verification on isolated PO 4:

1. Counted 25/20/25/20 directly into four stores; explicit zero for Citrus. New received 90, Blueberry remaining 10.
2. Reloaded the browser; the unsubmitted count was restored before submission.
3. Submitted once: four mock inventory commands and one receipt event.
4. Reopened Receive Remaining Items; entered five new units. Negative input disabled Submit.
5. Simulated a lost Square acknowledgement: 95 received remained durable, with an uncertain event visible. Retried that event using the same payload/key; cumulative receipts stayed 95.
6. Submitted a third shipment: five remaining Blueberry units plus 20 Citrus units per store. Blueberry cumulative 100; Citrus cumulative 80; three distinct receipt events.
7. Verified ten unique durable commands totaling 180 units; only the uncertain command had a second attempt. No financial activity or ledger entry was created.
8. Downloaded the vendor PDF again: SHA-256 remained identical after all three shipments.

Measured desktop count row height: **41 px** at 1440 × 1080. Inputs are compact and right aligned, table headers remain sticky, and horizontal scrolling remains available. Screenshots: `subsequent-shipment-count.jpg`, `three-shipment-history.jpg`, and `recoverable-store-update.jpg` in the evidence directory.

## Remaining limits for owner review

- All inventory transmissions were mocked. No live Square mutation or production integration was exercised. Production rollout requires separate authorization and the established inventory permission/configuration checks.
- Draft count recovery is local to the browser/device; submitted events and exact commands persist in PostgreSQL. It does not provide cross-device draft collaboration.
- Existing inconsistent legacy orders (for example, intended allocations that do not support the original order balance) require explicit review; this change does not rewrite historical plans or receipts.
- An earlier temporary in-app browser tab became blocked by a native confirmation dialog during the final count-clear check. New controls were changed to inline confirmations. Core shipment submission, subsequent counts, history and retry were browser-verified before that dialog. The last browser recheck of the inline clear/close confirmation controls remains unverified pending dismissal of the old dialog; API closure and draft protection tests pass. Screenshots show the verified shipment workflow before that final auxiliary-control refinement.
- Existing FastAPI lifecycle and SQLAlchemy circular metadata warnings remain; skips are not counted as passes.

## Changed files and preservation

Modified: `app/services/purchase_order_admin_service.py`, `app/services/purchasing_inventory_service.py`, `app/routers/purchasing.py`, `app/routers/management.py`, `app/templates/management_ordering_order_detail.html`, `app/static/v2/purchasing.css`, `tests/test_purchasing_receiving_extension.py`, and `tests/test_v2_ordering_data_coordinator.py` (read-only fixture now includes supplier-line balance fields).

Added: `app/templates/purchasing/receiving.html`, `app/static/v2/receiving.js`, `tests/test_purchasing_shipments.py`, this review report and local evidence artifacts. No additional service module or inventory authority was introduced.

Hash comparison confirms `app/models.py`, the purchasing math/session services, card-balance authority, COGS classification and Square request policy are unchanged from the start of this task. No new tables, migrations, replenishment calculations, payment-account rules or accounting workflows. Pre-existing worktree changes are preserved. The temporary preview launcher now records synthetic inventory responses while continuing to deny external network connections and actual financial writers.

Recommendation: owner review of the local direct-to-store workflow. Finish the auxiliary inline-confirmation browser recheck after dismissing the old test dialog. No production deployment is authorized or performed.
