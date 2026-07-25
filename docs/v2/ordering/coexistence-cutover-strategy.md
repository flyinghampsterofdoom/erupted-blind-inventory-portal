# V1/V2 coexistence and cutover strategy

Status: proposed. V1 is canonical now. Coexistence must be single-writer by aggregate and workflow.

## Ownership by phase

| Phase | V1 ownership | V2 ownership | Collision prevention |
|---|---|---|---|
| 0–1 | All operational ordering, receipt, payment, Square writes | Read snapshots and recommendations only | Read-only V1 adapter; no V2 operational foreign writes |
| 2 | V1 behavior unchanged | V2 merchandising decisions affect only V2 recommendations | Separate tables; no synchronization into V1 pars/mappings |
| 3 | V1 POs remain canonical operational POs | V2 drafts only | Distinct IDs/tables and protected source/recommendation key |
| 4 | Existing V1 POs remain V1-owned | Approved V2 snapshots/PDFs for explicitly enrolled flow | Source-system marker and uniqueness; no automatic V1 copy |
| 5–6 | V1 receives/pays V1-owned orders | V2 receives/pays only V2-owned approved orders | Immutable `order_owner`; routes reject wrong-owner aggregate |
| 7 | Existing V1 Square flow until separately retired | V2 gateway only for enrolled V2 receipts/targets | Inventory-command business key, location/SKU scope, owner gate |

## Data reuse decisions

- **Safe read through adapters:** stores/location links, vendors, SKU mappings, pars, open V1 supply, and historical V1 PO/allocations. Freshness and ambiguity must be surfaced.
- **Do not directly extend as V2 write ownership initially:** V1 `purchase_orders`, lines, allocations, payment fields, and `square_sync_events`.
- **Verify before any reuse:** dormant receipt tables, deployed uniqueness constraints, SKU/variation duplicates, production statuses, and orphan rows.
- **New V2 ownership:** recommendation evidence/decisions, drafts/snapshots, receipt events, finance allocations, and future inventory commands.

## Duplicate prevention

- PO approval uniqueness covers V2 draft version/source selection/vendor/store-cycle business key. UI warnings alone are insufficient.
- Each order has one owner (`V1` or `V2`) and one receiving system. Receipt submission has a stable client operation ID and server payload digest.
- Each inventory target derives a durable command key from immutable receipt/allocation identity. V1 and V2 gateway scopes cannot overlap during canary.
- Before a V2 approval, query active V1 and V2 orders for the same vendor/SKU/store/order cycle and require explicit conflict resolution.

## Cutover evidence gates

Before each ownership change require: PostgreSQL migration verification, deterministic calculation tests, production-data profile, capability/store-scope tests, audit completeness, canary success metrics, rollback rehearsal, owner sign-off, and zero unresolved high-severity reconciliation events. Square writes additionally require sandbox evidence, outcome-unknown drill, operational monitoring, per-location limits, and explicit principal approval.

## Rollback

Feature exposure can be disabled without deleting V2 data. V2-created snapshots, receipts, payments, and commands remain read-only evidence. Never copy them into V1 as an emergency rollback unless a separately reviewed reconciliation tool exists. An aggregate already owned by V2 remains V2-owned through resolution; new work may return to V1 only for new, non-overlapping orders.

## History

Historical V1 orders do not require destructive migration for early phases. Provide a read-only projection labeled V1, preserving original semantics and missing fields. Migrate only if a later capability needs normalized history and reconciliation proves counts, costs, statuses, payments, and Square outcomes. Never fabricate absent receipt/payment events.

## Canonical cutover

V2 becomes canonical one bounded workflow at a time: recommendations, then decisions, then selected new POs, receiving for those POs, finance for those POs, and finally inventory writes. A global flag flip is not an acceptable ownership transfer.
