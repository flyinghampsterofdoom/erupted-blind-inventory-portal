# V1-to-V2 Ordering parity ledger

Canonical owner: **V1 canonical**. This ledger is planning evidence, not implementation or cutover approval.

Legend:

- Initial parity: required before V2 can own the capability.
- V1 link: may remain an unchanged V1 destination during an additive navigation stage.
- Enhancement: useful only after parity.
- Removal: never without separate explicit owner approval.

| Capability | V1 | Initial parity | V1 link | Enhancement | Removal candidate | Risk | Test requirement | Data owner |
|---|---:|---:|---:|---:|---:|---|---|---|
| Management dashboard entry | Yes | Yes | Yes | No | No | Low | Permission/navigation matrix | V1 |
| Vendor list and active state | Yes | Yes | Yes | Later | No | Medium | Square fixtures; missing/deactivation cases | Square cached locally |
| Vendor sync | Yes | Yes for writer parity | Yes | Scheduling later | No | High | Pagination/failure/deactivation | Square→V1 |
| Vendor/SKU assignment sync | Yes | Yes | Yes | Reconciliation later | No | High | duplicate/missing/default-vendor fixtures | Mixed |
| Mapping single edit | Yes | Yes | Yes | Better validation later | No | High | constraints and audit | V1 |
| Mapping bulk edit | Yes | Yes | Yes | Later | No | High | partial invalid rows/atomicity | V1 |
| Mapping CSV import | Yes | Yes | Yes | Preview/download later | No | Medium | exact headers, line errors, booleans | V1 |
| Variation-ID auto-fill | Yes | Yes | Yes | Drift report later | No | Medium | duplicate SKU/missing catalog | Square→V1 |
| GTIN storage/use | Yes | Yes | Yes | Multiple barcode aliases later | No | High | zero padding and mapping precedence | Square/V1 cache |
| Unit cost | Yes | Yes | Yes | Cost history later | No | High | Square/manual precedence and zero | Mixed |
| Pack size and MOQ | Yes | Yes | Yes | Vendor unit model later | No | High | rounding, short shipment, scans | V1 |
| Default vendor uniqueness | Yes | Yes | Yes | Conflict UI later | No | High | partial unique behavior | V1 |
| Global/vendor math defaults | Yes | Yes | Yes | Admin UI later | No | Medium | precedence and validation | V1 |
| Live Square sales history | Yes | Yes | No writer | No | No | High | pagination/date/location fixture parity | Square |
| Live Square on-hand | Yes | Yes | No writer | No | No | High | location/variation/negative/missing | Square |
| Open in-transit subtraction | Yes | Yes | No | No | No | High | status and allocation aggregation | V1 |
| Dynamic suggestion math | Yes | Yes | No | Explainability later | No | High | golden recommendation fixtures | V1 derived from Square |
| Confidence score/state | Yes | Yes | No | Model revision later | No | Medium | sparse/volatile/no-sale histories | V1 derived |
| Manual store level/par | Yes | Yes | Yes | Bulk tools later | No | High | null/zero/global/store precedence | V1 |
| Best-guess manual prefill | Yes | Yes | Yes | Preview later | No | Medium | only-fill-missing behavior | V1 derived |
| Standard generation | Yes | Yes | Yes until cutover | No | No | High | end-to-end snapshot fixtures | V1 |
| Full-stock generation | Yes | Yes | Yes until cutover | No | No | High | include zero/non-default mappings | V1 |
| One draft per selected vendor | Yes | Yes | No | Multi-vendor batch later | No | Medium | selected-vendor grouping | V1 |
| Stock Coverage→draft | Yes | Yes | Yes | Later | No | High | report-to-snapshot parity | Square/V1 |
| Draft resume | Yes | Yes | Yes | Draft queue later | No | Medium | persistence and permissions | V1 |
| Add/restore mapped line | Yes | Yes | Yes | Search improvements later | No | Medium | duplicate SKU/variation fallback | V1 |
| Catalog refresh of DRAFT | Yes | Yes | Yes | Diff preview later | No | High | precise overwritten fields | Square→V1 snapshot |
| Store split editing | Yes | Yes | Yes | Better balancing later | No | High | sum/zero/missing-store behavior | V1 |
| Remove/zero line behavior | Yes | Yes | Yes | Soft-delete history later | No | High | received/unreceived edge cases | V1 |
| Continued IN_TRANSIT edits | Yes | Yes | Yes | Owner decision later | No | High | PDF/idempotency implications | V1 |
| Submit DRAFT→IN_TRANSIT | Yes | Yes | Yes until cutover | No | No | High | atomic PO/PDF/audit test | V1 |
| Vendor PDF | Yes | Yes | Yes | Storage/versioning later | No | High | layout/content/filename/golden PDF | V1/filesystem |
| Generic/vendor disclaimer | Yes | Yes | Yes | Multiple layouts later | No | Medium | precedence and pagination | V1 |
| Manual external vendor send | Yes, outside app | Document boundary | Yes | Email/portal later | No | Medium | operational acceptance | External manual process |
| Invoice PAID/UNPAID | Yes | Yes | Yes | Accounting integration later | No | Medium | amount/date/difference validation | V1 |
| Manual received quantities | Yes | Yes | Yes | Later | No | High | per-store totals and status gating | V1 |
| Barcode scan | Yes | Yes | Yes | Hardware UX later | No | High | SKU/variation/GTIN/pack/overage | V1 |
| Cancel scan | Yes | Yes | Yes | Later | No | High | pack decrement defect decision | V1 |
| Unexpected barcode line | Yes | Yes | Yes | Review queue later | No | High | synthetic identity and cleanup | V1 |
| Square receive push | Yes | Yes for writer cutover | Yes | Worker later | No | Critical | idempotency, partial failure, retry | V1 command→Square |
| Failed-only retry | Yes | Yes | Yes | Automated retry later | No | Critical | deterministic event reuse | V1 |
| SENT_TO_STORES terminal behavior | Yes | Yes | Yes | Close/receive model later | No | High | current actual transition only | V1 |
| Emergency draft | Yes | Later parity | Yes | No | No | Critical | JSON/store mapping and permissions | V1 |
| Emergency Square physical count | Yes | Later parity | Yes | Safer reconciliation later | No | Critical | partial-success replay | V1 command→Square |
| Combined order table | Yes | Yes | Yes | Separate views later | No | Medium | all statuses/emergency sort/limit | V1 |
| Dedicated Current Orders | No | No | No | Post-parity | N/A | Medium | define semantics first | Undecided |
| Dedicated Order History | No | No | No | Post-parity | N/A | Medium | define archive/filters first | Undecided |
| Dedicated Order Payments | No | No | No | Post-parity | N/A | Medium | define scope/state first | Undecided |
| Formal receipt records | Schema only | Investigate | No | Possible later | No | High | production-row discovery | Unknown |
| COMPLETED/CANCELLED transitions | Enum only | Preserve values; do not invent | No | Owner decision later | No | High | production distinct values | Unknown |
| Audit events | Partial | Yes | N/A | Structured diffs later | No | High | action/actor/metadata parity | V1 |
| Historical immutable output | No | Preserve current behavior first | N/A | Strong enhancement | N/A | High | artifact version tests | Undecided |

## Parity requirements versus post-parity enhancements

Initial parity must reproduce current authorization, Square ownership, math, mappings, manual overrides, PO snapshot fields, editing rules, PDF content, invoice validation, receiving, idempotency, audit actions, and failure behavior.

Post-parity enhancements include dedicated Current/History/Payments pages, scheduled sync, cost history, immutable PDF versions, structured audit diffs, vendor email/portal delivery, automated retry, optimistic concurrency, and a completed/cancelled lifecycle. None may be smuggled into parity work without a separate product decision.
