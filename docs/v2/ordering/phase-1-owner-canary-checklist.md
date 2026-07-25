# Phase 1 Ordering owner-canary preparation

Preparation date: 2026-07-25. Status: checkpoint preparation only. Deployment and production feature exposure are not authorized.

## Verified release evidence

- PostgreSQL 16.12 full suite: 247 passed, 0 failed, 1 skipped. The sole skip is the optional real-R2 integration.
- Focused Phase 1 suite: 43 passed.
- Unchanged V1 Ordering regression selection: 39 passed.
- Repository migration head: `20260720_0006`, one head. A disposable database upgrades through the full chain to head, and Phase 1 changes no model or migration.
- `/v2/ordering` is GET-only. Disabled exposure returns 404; an exposed principal without effective `management.admin` returns 403.

## Exact proposed exposure operation

The production owner principal ID and current production feature strings were not available in the repository or local environment and must not be inferred from test fixture principal ID `4`.

At the deployment approval checkpoint:

1. Read and retain the complete current `V2_PRINCIPAL_FEATURES` value.
2. Verify the selected owner is an active individual principal with effective `management.admin`, and record the database principal ID.
3. Append exactly one entry, `<verified_owner_principal_id>:ordering_intelligence_v2`, preserving every existing comma-separated entry. If that exact pair already exists, make no duplicate.
4. Leave `V2_ENABLED_FEATURES` unchanged and confirm it does not contain `ordering_intelligence_v2`.
5. Do not add the key for any STORE, LEAD, shared, general-management, or other principal.
6. Do not change `ordering_v1_links_v2` or any V1 authorization/configuration.

The literal before/after production value must be recorded and approved before configuration mutation. Until the owner ID and current value are verified, exposure is blocked.

## Read-only live verification checklist

Record timestamp, principal, store scope, observed SKU/vendor, outcome, and evidence for each check. Never create or alter production data to manufacture a case.

- [ ] Owner navigation contains Ordering Intelligence and retains the existing V1 Ordering bridge.
- [ ] Owner GET `/v2/ordering` succeeds; the page contains no mutation controls.
- [ ] An authenticated unexposed control principal has no navigation item and receives 404 at the direct route.
- [ ] If an approved capability-negative test principal exists, exposure alone still receives 403; otherwise record not tested and do not broaden exposure to create one.
- [ ] Store selector lists only server-authorized active stores.
- [ ] Single-store selection does not consume another store's inventory as supply.
- [ ] Successful Square catalog, inventory, completed-order, and inventory-change reads show source evidence.
- [ ] Each recommendation shows freshness timestamp/state and ACTIONABLE, INFORMATIONAL, or BLOCKED state.
- [ ] Explanation shows 7/28/56 windows, eligible/stockout days, observed/adjusted velocity, inventory, incoming supply, effective levels, unrounded calculated need, applied policies, inputs, and source evidence.
- [ ] Confidence is HIGH/MEDIUM/LOW with stable reason codes and does not change the calculated quantity.
- [ ] Warnings and blocking reasons are visible; a CRITICAL required source never displays an actionable quantity.
- [ ] A V2 Square-read failure remains visible, uses no invented/cached value, and does not affect V1 Ordering.
- [ ] V1 Ordering pages and operational actions remain available and unchanged.

## Natural production examples

Inspect only examples that already exist. Mark every unavailable case `NOT OBSERVED`; do not create, edit, receive, submit, discontinue, or otherwise mutate a record.

| Scenario | Required evidence | Result |
|---|---|---|
| Fresh data | Source timestamps within 24 hours; actionable only if no other blocker | Pending canary |
| Stale or blocked data | `STALE DATA` informational display, or critical source-specific block | Pending canary |
| Null par | Demand inference warning and MEDIUM-or-lower confidence | Pending canary |
| Zero par | Numeric zero preserved; no inferred exclusion | Pending canary |
| Manual lock | Locked named input disclosed; no inferred exclusion | Pending canary |
| New/insufficient history | No fabricated demand quantity without explicit manual target | Pending canary |
| Established zero sales | Zero accepted only with fresh complete data and at least 14 eligible in-stock days | Pending canary |
| Confirmed stockout adjustment | Observed/adjusted velocity and removed days shown | Pending canary |
| Positive `IN_TRANSIT` | PO ID and quantity shown separately; age warning over 30 days | Pending canary |
| Discontinued/deleted | Actionable quantity suppressed only for confirmed status | Pending canary |
| Unresolved non-sellable | Inventory left unchanged with limitation disclosed | Pending canary |

## Success, stop, and rollback criteria

Success requires every applicable checklist item to pass, no write or V1 regression, source-specific failure behavior, and owner acceptance of explanation quality. Stop immediately for unauthorized data, pooled store inventory, an actionable critical result, hidden Square failure, unexpected write, or V1 impact.

Rollback removes only `<verified_owner_principal_id>:ordering_intelligence_v2`, preserves all other feature entries, redeploys/restarts through the approved path, and verifies navigation absence plus direct-route 404. The pre-checkpoint application commit remains the code rollback target. TD-026 is accepted only for this one-principal read-only canary and remains a blocker for broader exposure.
