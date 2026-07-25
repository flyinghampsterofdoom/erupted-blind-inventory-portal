# V2 Ordering discovery risk register

Status date: 2026-07-22. This register covers migration and architecture risks; intentional deferred implementation debt belongs in the authoritative V2 technical-debt register.

| ID | Risk | Evidence/status | Impact | Mitigation / gate | Owner decision? |
|---|---|---|---|---|---|
| ORD-R01 | Timeout after successful Square write can cause uncertain or duplicate inventory | Confirmed architecture gap | Critical inventory drift | Durable command, deterministic key, outcome-unknown state, remote reconciliation before retry | No |
| ORD-R02 | Emergency/count paths use fresh keys or may replay successful targets | Confirmed | Critical inventory drift | Keep outside V2; audit before future unified gateway | No |
| ORD-R03 | `SQUARE_READ_ONLY` does not protect all ordering/count writers | Confirmed | Unintentional production write | Central future gateway and explicit send capability; independently validate existing controls before cutover | Yes |
| ORD-R04 | Receiving spans mutable local allocation and remote Square without atomicity | Confirmed | High reconciliation failure | Local immutable receipts first; later command ledger | No |
| ORD-R05 | Pack scan cancel subtracts one rather than pack quantity | Confirmed defect | Incorrect receipt quantity | Preserve V1 now; log as debt and cover before V2 receiving parity | No |
| ORD-R06 | PO PDF depends on mutable mappings/template | Confirmed | Historical commercial record changes | Approved immutable snapshot, renderer version, durable hash/storage | Yes |
| ORD-R07 | Payment is one mutable PO field set | Confirmed | Cannot represent real remittance/reversal | Payment events and allocation model after accounting approval | Yes |
| ORD-R08 | COGS uses current preferred cost for historical sales | Confirmed | Historical reports mutate/misstate | Approve recognition/valuation; snapshot cost evidence and periods | Yes |
| ORD-R09 | SKU text and duplicate identities cross Square/local tables | Confirmed | Wrong mapping/vendor/order | Durable product/variation identity; production profile and exception queue | Yes |
| ORD-R10 | Zero/null par and lock/exclusion semantics are ambiguous | Confirmed/unresolved | Silent under/over-order | Resolve decision register; explicit decision model | Yes |
| ORD-R11 | V1 and V2 could create duplicate POs/receipts during coexistence | Proposed migration hazard | High operational duplication | Source owner, DB uniqueness, one workflow owner per order, conflict query | Yes |
| ORD-R12 | Cross-store aggregation may assume transferable inventory | Confirmed/inferred policy gap | Store stockouts hidden by remote stock | Per-store calculation and approved transfer states | Yes |
| ORD-R13 | Dormant receipt tables may contain production data | Unresolved until profile | Migration/data loss | Read-only production measurement before schema choice | No |
| ORD-R14 | Effective V1 authorization is broad and inconsistent by report | Confirmed | Financial/store data overexposure | Capability and store scope matrix; route/security tests | Yes |
| ORD-R15 | Missing/stale Square data can look like zero demand or no recommendation | Confirmed behavior risk | Under-order | Preserve missingness, freshness and blocked/low-confidence explanations | Yes |
| ORD-R16 | Recommendation formulas differ between generation and reports | Confirmed | User distrust/inconsistent decisions | One versioned deterministic engine and evidence snapshot | Yes |
| ORD-R17 | No optimistic concurrency on current operational rows | Confirmed | Lost edits/double approval | V2 row version plus DB uniqueness/idempotency | No |
| ORD-R18 | Historical V1 records cannot be normalized without invented facts | Confirmed model gaps | False audit/accounting history | Read-only labeled projection; migrate only evidenced values | Yes |

## Highest-risk gate

No Square write milestone should start until R01–R04 have executable reconciliation tests and a rehearsed runbook. No finance milestone should start until R07–R08 policy decisions are approved.
