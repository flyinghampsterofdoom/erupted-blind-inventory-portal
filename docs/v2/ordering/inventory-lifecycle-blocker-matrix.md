# Inventory lifecycle implementation blocker matrix

Status date: 2026-07-25. Status: **FOCUSED PHASE 1/2 IMPLEMENTED LOCALLY — VERIFICATION GATE OPEN**. This matrix records current evidence and does not authorize deployment or exposure.

Source: [owner decision packet](./inventory-lifecycle-owner-decision-packet.md). All 15 decision marks are cleared by the recorded owner dispositions. A mark now identifies which approved rule governs a phase; later phases also inherit applicable earlier-phase rules.

| Packet decision | Disposition | Register decision(s) | P1 lifecycle model | P2 bulk controls | P3 Square filtering | P4 workspace UX | P5 remeasurement | P6 Stagnant Inventory |
|---|---|---|:---:|:---:|:---:|:---:|:---:|:---:|
| IL-01 Identity/scope | Approved | ORD-DEC-028 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| IL-02 Restore/fallback | Approved | ORD-DEC-030 | ✓ | ✓ |  |  |  |  |
| IL-03 No Future Reorder output | Approved | ORD-DEC-029 | ✓ |  |  | ✓ |  | ✓ |
| IL-04 Archive eligibility/control/remote effect | Approved | ORD-DEC-030, 031 | ✓ | ✓ | ✓ |  | ✓ |  |
| IL-05 Notes | Approved | ORD-DEC-030 |  | ✓ |  |  |  |  |
| IL-06 Atomic bulk conflict | Approved | ORD-DEC-030 |  | ✓ |  |  |  |  |
| IL-07 Valuation basis | Approved with modification | ORD-DEC-033 |  |  |  |  |  | ✓ |
| IL-08 Last-sale evidence | Approved | ORD-DEC-034 |  |  |  |  |  | ✓ |
| IL-09 Sell-through velocity | Approved | ORD-DEC-032 |  |  |  |  |  | ✓ |
| IL-10 Inclusion/store/unknown inventory | Approved | ORD-DEC-032 |  |  |  |  |  | ✓ |
| IL-11 Queues/default sort/archived view | Approved with modification | ORD-DEC-035 |  |  |  | ✓ |  |  |
| IL-12 Select-all scope | Approved | ORD-DEC-035 |  | ✓ |  | ✓ |  |  |
| IL-13 Limits and endpoint-specific filtering | Approved | ORD-DEC-031, 035 |  | ✓ | ✓ | ✓ | ✓ |  |
| IL-14 Automatic archive | Approved deferred boundary | ORD-DEC-036 |  |  |  |  |  |  |
| IL-15 Shelf-location ownership | Approved with modification | ORD-DEC-032 |  |  |  |  |  |  |
| **Governing decision count** |  |  | **4** | **7** | **3** | **5** | **3** | **6** |

## Phase gates

| Phase | Approved governing decisions | Remaining non-policy dependency | Readiness |
|---|---|---|---|
| P1 | IL-01–IL-04 | No implementation-review dependency remains; deployment is separately gated | **IMPLEMENTED AND VERIFIED** |
| P2 | IL-01, IL-02, IL-04–IL-06, IL-12, IL-13 | No implementation-review dependency remains; owner capability grant is separately gated | **IMPLEMENTED AND VERIFIED** |
| P3 | IL-01, IL-04, IL-13 | Instrumented filtering and empty-lifecycle parity are verified | **IMPLEMENTED AND VERIFIED** |
| P4 | IL-01, IL-03, IL-11–IL-13 | Outside the focused Phase 1/2 scope; broad usefulness still depends on acceptable Square/read-model performance | **POLICY READY — DEFERRED** |
| P5 | IL-01, IL-04, IL-13 | Before baseline is retained; benefit measurement follows real owner archiving | **DEFERRED TO OWNER CANARY** |
| P6 | IL-01, IL-03, IL-07–IL-10 | Durable long-horizon Square read model (TD-026) and trustworthy recent purchase-cost evidence (TD-028) | **TECHNICALLY BLOCKED** |

Owner-policy-blocked phases: **0 of 6**. Technically/source-data-blocked phases: **1 of 6 (P6)**. P4 is deliberately deferred rather than policy-blocked; P5 is ordered after P3 rather than independently blocked.

IL-14 and IL-15 do not block P1–P6 because automatic archive remains disabled and shelf location is outside Ordering. Automatic archive requires separate implementation and activation approval after the read-model, monitoring, dry-run, audit, rollback, and recovery contracts exist.

## Readiness by requested delivery area

| Area | Readiness | Reason |
|---|---|---|
| Lifecycle foundation | **IMPLEMENTED AND VERIFIED** | Identity, transitions, restore fallback, capability, notes, audit, atomicity, constraints, concurrency, and rollback passed PostgreSQL 16 verification |
| Ordering integration | **IMPLEMENTED AND VERIFIED** | Archive filtering, No Future Reorder no-quantity policy, Active/empty-lifecycle parity, badges, metadata, and diagnostics are verified |
| Workspace UX | **POLICY READY — DEFERRED** | Queues, sorting, select-visible, page choices, limits, and badges are approved; full workspace is outside the focused Phase 1/2 scope |
| TD-026/read-model | **NOT READY / TECHNICALLY OPEN** | Durable snapshots, long-horizon last-sale coverage, cache/failure/observability contracts, and broader rollout work remain unimplemented |
| Stagnant Inventory | **BLOCKED** | Requires TD-026 durable last-sale evidence and TD-028 trustworthy recent-purchase-cost evidence |
| Automatic archive | **DISABLED / DEFERRED** | Explicitly outside the foundation and requires separate approval plus fresh all-store evidence, dry-run, audit, monitoring, rollback, and recovery |
