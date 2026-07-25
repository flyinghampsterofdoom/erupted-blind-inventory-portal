# Purchase-order PDF audit

## Confirmed generation behavior

- `purchase_order_admin_service._generate_purchase_order_pdf` uses ReportLab canvas and US Letter pages.
- Submit generates a PDF after setting the PO to IN_TRANSIT in the same database transaction, but filesystem output is not transactional.
- GET download calls `ensure_current_purchase_order_pdf`; missing files or `updated_at > submitted_at` regenerate from current database/configuration and flush a new `pdf_path`.
- Files live at `generated/purchase_orders/purchase_order_{id}_{UTC timestamp}.pdf`; browser filename is `purchase-order-{id}.pdf`.
- Vendor-specific active template wins, then generic active template, then built-in header. Template name does not select a layout; only legal disclaimer changes output.
- Lines are current nonremoved PO lines, sorted by item/variation. Columns are Item, Variation, QTY. Quantities are individual units with approximate ceiling pack display when current mapping pack size >1.
- PDF omits unit cost, price, extended total, store allocation, payment, receiving, address/contact, and vendor submission status.
- Long labels are truncated by character count. Alternating rows and a footer disclaimer are paginated by fixed layout rules.

## Snapshot and historical behavior

PO lines snapshot identifiers, labels, costs, prices, ordered quantities, confidence, and par fields, but remain editable after submit. PDF regeneration also looks up **current** vendor template and **current** mapping pack size. Therefore the file is not a durable approved-order snapshot and historical reproduction is not guaranteed.

The referenced file can change after an IN_TRANSIT edit. Old replaced files are not systematically deleted. The current referenced file is best-effort deleted when a DRAFT or IN_TRANSIT PO is hard-deleted. There is no file hash, version, immutable artifact row, object storage, backup/retention contract, or proof of what was actually sent to a vendor. `vendor_contacts` and email fields exist but no sender uses them.

## Permissions, failures, and tests

- Download and template administration both use broad `management.admin`.
- Missing PO/empty order/missing ReportLab/missing file produce errors; GET may create a file/path.
- No golden PDF, byte/content, pagination, disclaimer, permissions, storage, regeneration, or historical-reproducibility test exists.

## V2 requirement

Yes: V2 needs an immutable approved PO snapshot and artifact version. Approval should freeze vendor identity/contact snapshot, lines, units, pack/case interpretation, costs/prices as appropriate, allocations, terms, template version, totals/rounding policy, actor/time, and a content hash. Regeneration should reproduce an artifact from that snapshot or create a clearly linked replacement version; it must never silently rewrite the approved artifact.
