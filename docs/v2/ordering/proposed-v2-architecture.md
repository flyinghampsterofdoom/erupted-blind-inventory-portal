# Proposed V2 Ordering architecture

Status: proposed only. This design follows existing V2 capability, store-scope, result, CSRF, audit, and owner-preview conventions. It creates no runtime exposure.

## Boundaries

| Layer | Proposed modules | Responsibility |
|---|---|---|
| Routers | ordering intelligence, merchandising controls, purchase orders, receiving, payments, ordering administration | Thin HTTP validation, capability/store-scope checks, CSRF on mutations, result rendering |
| Services | recommendation, merchandising decision, PO lifecycle, snapshot/PDF, receiving, payment allocation, COGS reporting, reconciliation | One bounded workflow per service; no oversized all-domain service |
| Repositories | product/read-model, V1 supply adapter, recommendation, decision, PO, receipt, payment, reporting | Explicit query and transaction boundaries; V1 adapter is read-only |
| Integrations | Square read gateway; future Square inventory-command gateway; PDF renderer/storage | Isolate external schemas, timeouts, fixtures, idempotency, and failure translation |
| Jobs | recommendation-run computation; stale-data refresh; future reconciliation | Scheduled computation creates versioned evidence, never orders automatically |

## Proposed domain ownership

- `RecommendationRun` and `Recommendation`: immutable input/version evidence and calculated output.
- `MerchandisingDecision`: status, exclusion, preferred vendor, override, reason, scope, effective dates.
- `PurchaseOrderDraft`: editable human selection with row version.
- `PurchaseOrderSnapshot`: immutable approved commercial terms; replacement links instead of mutation.
- `Receipt` and `ReceiptLine`: append-only submissions/dispositions linked to snapshot lines.
- `InventoryCommand`: separately approved intent and durable Square outcome/reconciliation state.
- `VendorInvoice`, `Payment`, `PaymentAllocation`, `FundingAccountReference`: accounting boundary.
- `CostAllocation`/report snapshot: versioned reporting evidence, pending accounting-policy approval.

These are conceptual entities, not a migration proposal.

## Request and authority contracts

- Every route requires a named capability; role labels alone are insufficient.
- Store-scoped reads and mutations resolve authorized store IDs server-side. Client-submitted scope is intersected, never trusted.
- Vendor price, COGS, payment, Square approval, and reconciliation are separately grantable capabilities.
- Every mutation requires CSRF, typed validation, a stable result/error code, actor/time/request correlation, before/after or event payload, and a reason where policy requires it.
- Feature exposure uses a new proposed V2 Ordering capability key only after implementation approval. Existing `ordering_v1_links_v2` remains a bridge and must not imply V2 capability.

## Concurrency and state changes

Editable aggregates carry an integer row version. Mutation commands provide expected version; mismatch returns a conflict result and no write. Approval uses a uniqueness constraint/business key so one draft version can produce only one approved snapshot. State changes are validated transitions and append an audit event in the same PostgreSQL transaction.

## Idempotency

Local POST commands accept a server-bound operation token scoped to actor, aggregate, command, and intended version. Receipt submission, PO approval, payment allocation, and future inventory commands have durable uniqueness keys. A repeated identical command returns its prior result; a conflicting payload returns a conflict. Future remote write keys are derived from immutable command IDs, never fresh UUIDs on retry.

## Square boundaries

The read gateway normalizes catalog, inventory, order, vendor, and location data; records source timestamp and freshness; and supports captured fixtures. Read failures reduce confidence or block a recommendation rather than silently produce zero demand.

The write gateway does not exist in early phases. Its later contract is prepare -> inspect/dry-run -> independently approve -> send -> record success/failure/outcome-unknown -> reconcile. Only that gateway may call Square inventory writes.

## PDF, receiving, payment, and reporting

- PDF generation consumes only an immutable approved snapshot and a renderer version. The output is hashed, durably stored, and reproducible.
- Receiving consumes approved/placed snapshot lines and records local physical facts before any inventory command exists.
- Payment accepts invoice/payment facts and explicit allocations; it cannot mutate PO commercial history.
- Reporting reads versioned facts and states its cost/recognition basis. Operational estimates are labeled as estimates, not accounting truth.

## Error contract

Use established V2 structured results: stable code, safe message, field errors where applicable, correlation identifier, and retryability. Never expose Square secrets or raw sensitive payloads. External outcome-unknown is a distinct non-retryable-until-reconciled result.
