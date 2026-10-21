# Purchasing V3 — final receiving UI refinements

2026-10-17. Local implementation and verification only. No deployment, production changes or migrations.

## Delivered

1. **Receiving action state.** Submit to Stores is the primary action during an active count. Receive Remaining Items is hidden while counting or resuming a saved draft. It appears after successful partial submission and is absent for fully received or closed POs. Opening the count retains the existing local draft identity; refresh restores entered values without creating another receipt.
2. **Compact history.** One compact summary row per event shows its date, actual event quantity, PO-wide Outstanding After and posting status. Details default to collapsed. Row clicks or accessible disclosure buttons expand the existing store-level audit details independently.
3. **Confirmation verification completed.** Inline confirmations now have an explicit Cancel action and Escape dismissal. Clear affects only the current owner/PO browser draft and checks for newer edits from another tab. Shortage closure requires its explicit confirmation. No native JavaScript confirmation dialog is used by these controls.

## Historical quantity accuracy

The earlier records did not store a PO-wide outstanding snapshot. Those records show **Not recorded**, rather than a number reconstructed from current balances or one SKU's remainder. They are preserved unchanged.

New directly submitted shipments capture `po_outstanding_after` in the existing immutable event payload while holding the existing PO lock. Corrections and closures capture their event-time balance as well. Retries reuse the original snapshot. No new table, migration, quantity rule, posting mechanism or inventory authority was introduced.

For the two-SKU regression PO (100 + 80 units), the three events received **90, 5 and 85** units. Their recorded PO-wide outstanding quantities are **90, 85 and 0**. The first event's value is 90, not the first SKU's ten missing units. The final 85-unit event contains five Blueberry units plus 80 Citrus units. This is the existing three-shipment scenario rerun with the new summary metadata; original PO 4's three events remain unchanged.

## Browser acceptance checks

| Check | Result |
| --- | --- |
| Active/resumed count hides Receive Remaining Items | PASS; draft value restored after reload, Submit remains visible |
| Partial submission offers Receive Remaining Items | PASS |
| Fully received / explicitly closed PO offers no additional receiving | PASS; closed PO submission also rejected with HTTP 400 |
| Cancel draft clear | PASS; entered value and receiving history unchanged |
| Escape dismisses confirmation | PASS; entered value unchanged |
| Confirm clear affects only intended unsent draft | PASS; another PO's value survived reload |
| Cancel shortage closure | PASS; Submit remained available and no receipt was created |
| Confirm shortage closure | PASS; 180 units explicitly closed, zero inventory commands |
| Three independent shipment records | PASS; ten unique mocked inventory commands, correct totals |
| Multiple history details expand independently | PASS; collapsing one left the other open |
| No stale dialog blocks interaction | PASS; old temporary dialog/tab gone, subsequent count/submit/clear/close/history interactions worked |
| Original history and vendor PDF | PASS; old PO 4 payloads unchanged; PO 5 PDF hash unchanged after receiving |

## Regression evidence

Relevant regression: **87 passed, no skips**, 4 existing warnings, covering purchasing shipments, receiving extension, Phase 2, purchasing UI, and the existing PO receiving service. Log: `output/purchasing-receiving-final-ui/regression.log`. Coverage includes direct counts, draft/route behavior, totals, partial shipments, exact Square retries, duplicate submissions, concurrency limits, immutable receipts, original allocations and PDF preservation. Existing 500-SKU purchasing regression coverage is retained. JavaScript syntax and diff checks pass.

The three-shipment scenario was also completed in the actual local browser on synthetic PO 5. Quantity calculations, receiving updates, audit and PDF behavior remain unchanged apart from the additional event-time summary metadata. The closed test PO is 6; the editable/resumed count screenshot uses the original synthetic PO 4.

## Review links and evidence

- Active receiving: http://127.0.0.1:8763/management/purchasing/orders/4/receiving
- Compact completed history: http://127.0.0.1:8763/management/purchasing/orders/5/receiving
- Screenshots and verification JSON: `output/purchasing-receiving-final-ui/`.
- Changed implementation files: `app/static/v2/receiving.js`, `app/static/v2/purchasing.css`, `app/templates/purchasing/receiving.html`, and receiving presentation/event snapshot metadata in `app/services/purchase_order_admin_service.py`.
- Extended existing assertions in `tests/test_purchasing_shipments.py`; no new service or business workflow.

No remaining browser blocker. Square responses remain mocked, and production integration was not exercised. Events without recorded outstanding snapshots remain explicitly labeled Not recorded. Ready for local owner acceptance review; deployment remains unauthorized.
