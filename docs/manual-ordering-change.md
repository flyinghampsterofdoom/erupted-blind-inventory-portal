# Manual Ordering Discovery / Addition

Implemented on `codex/ordering-blind-count-reliability`; no deployment or live Square write performed during validation.

The PO editor searches the read-only Square catalog by unordered partial name, variation, SKU and GTIN tokens. Results use actual variation IDs, including SKU-less products. A process-local 60-second catalog cache avoids downloading the catalog on each keystroke; unavailable catalog reads produce an error, not synthetic identity.

`ordering.manage` controls PO discovery, generation, editing and receiving. ADMIN and legacy MANAGER roles receive the default; an explicit permission override can grant or deny designated accounts. STORE and LEAD do not receive it by default. Mapping, par, PDF-template administration and emergency inventory tools retain their existing admin permission. Access Controls exposes the new permission; the V1 navigation exposes Ordering for an authorized account. Existing broad Admin Actions overrides do not substitute for an explicit override of the new separate permission.

Manual additions bypass vendor eligibility and lifecycle recommendation exclusions. They retain catalog identity, name, variation, SKU/GTIN where present, PO vendor, explicit purchase cost, quantity and allocations. Local mapped cost may prefill for confirmation; unmapped cost must be entered. Blank/negative/nonfinite/overprecision values fail; deliberate zero is valid. Additions do not create mappings or change default vendors, pars, lifecycle or financial assignments. Standard generation remains restricted to configured mappings and now excludes No Future Reorder and Archived lifecycle records. Existing full-stock alternate-mapping rules remain intact.

Initial quantity goes to the first active store in the existing store-name order and can be reallocated in the PO editor. Duplicate active variations are rejected. Removed lines without receipt history can be restored only at their saved historical cost; unknown cost or receipt-history corrections require the existing authorized correction process. Historical cost is not overwritten.

## Financial compatibility

No payment, funding or consignment calculation code changed. Existing submit/receipt hooks still call `initialize_new_order_if_configured`; it uses saved PO costs and independently configured financial vendor classification. Manual addition does not create that classification, a funding assignment, or a consignment sales attribution relationship.

Unmapped purchases remain PO/receipt evidence and are receivable through the established inventory adjustment workflow. Mapping-scoped reports, including `v2_order_payments_service.inventory_snapshot`, can omit the unmapped product from that vendor's inventory summary. Consignment sales attribution remains governed by its independent effective-dated assignments. Existing payment records may not automatically recalculate when an already-placed PO is edited; this behavior is preserved. These limitations are documented rather than changing accounting semantics.

## Verification

34 focused ordering, receiving and vendor-reassignment tests passed, including name/token search, explicit cost, SKU-less identity, mapped/non-primary/unmapped receiving, lifecycle exclusion, restoration without cost mutation, and explicit purchasing permission. Square receipt HTTP calls were mocked. No migration is needed for this change.
