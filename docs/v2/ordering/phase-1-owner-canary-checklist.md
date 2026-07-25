# Phase 1 Ordering owner-canary verification

Verification date: 2026-07-25. Status: **OWNER CANARY LIVE AND VERIFIED** for principal `6` only. Staff rollout, global exposure, and V1 cutover are not authorized.

## Verified release evidence

- PostgreSQL 16.12 full suite: 247 passed, 0 failed, 1 skipped. The sole skip is the optional real-R2 integration.
- Focused Phase 1 suite: 43 passed.
- Unchanged V1 Ordering regression selection: 39 passed.
- Repository migration head: `20260720_0006`, one head. A disposable database upgrades through the full chain to head, and Phase 1 changes no model or migration.
- `/v2/ordering` is GET-only. Disabled exposure returns 404; an exposed principal without effective `management.admin` returns 403.

## Production deployment evidence

- Approved and deployed commit: `55500f6a3b7fd65f04edced64915e056ac454752` (`Harden V2 release and add Ordering Phase 1`). Local `HEAD` and `origin/main` match and the working tree was clean at deployment verification.
- Final Render deployment: `dep-d9ig49brjlhs73euh650`, status `live`.
- Pre-deployment rollback commit: `676b648bf816f3e8cfe37b993e90f93c5c7cb3c4`.
- Production Alembic revision before and after deployment: `20260720_0006`; no migration or schema change occurred.
- `V2_ENABLED_FEATURES` remains unset. Final `V2_PRINCIPAL_FEATURES` is `6:daily_store_logs_v2,6:ordering_v1_links_v2,9:daily_store_logs_v2,6:staff_scheduling_v2,6:digital_signage_v2,6:ordering_intelligence_v2`.
- Owner principal `6` is the active individual ADMIN account `justinrawlinson`, with effective `management.admin=true`.
- The all-store owner request returned HTTP 200 with 3,296 recommendations in 112.894 seconds. An Andresen-only request returned HTTP 200 with 824 recommendations in 26.261 seconds. TD-026 remains accepted only for this read-only owner canary and blocks wider operational exposure without separate review.

## Executed exposure operation

- Before: `6:daily_store_logs_v2,6:ordering_v1_links_v2,9:daily_store_logs_v2,6:staff_scheduling_v2,6:digital_signage_v2`
- After: `6:daily_store_logs_v2,6:ordering_v1_links_v2,9:daily_store_logs_v2,6:staff_scheduling_v2,6:digital_signage_v2,6:ordering_intelligence_v2`
- Only `6:ordering_intelligence_v2` was appended. Existing order and entries were preserved, `ordering_v1_links_v2` was unchanged, and no STORE, LEAD, general-management, or global exposure was added.

## Read-only live verification checklist

Record timestamp, principal, store scope, observed SKU/vendor, outcome, and evidence for each check. Never create or alter production data to manufacture a case.

- [x] Owner navigation contains Ordering Intelligence and retains the existing V1 Ordering bridge.
- [x] Owner GET `/v2/ordering` succeeds with HTTP 200; the page contains no mutation controls.
- [x] Authenticated unexposed control principal `9` has no Ordering Intelligence navigation item and receives HTTP 404 at the direct route. The response is only `{"detail":"Not Found"}` and contains no recommendation data.
- [ ] Capability-negative exposure check not exercised. Exposure was not broadened to manufacture a 403 case; automated coverage remains the evidence for this dependency ordering.
- [x] Owner store selector contains only the four server-authorized active stores: Andresen, HWY 99, Longview, and SR 503.
- [x] Andresen-only selection produced 824 rows and every store cell was Andresen; no other store appeared as supply or result scope.
- [x] Successful Square catalog, inventory, completed-order, and inventory-change reads show source timestamps and availability evidence.
- [x] Each recommendation shows freshness and ACTIONABLE, INFORMATIONAL, or BLOCKED state. All-store counts were 152 actionable, 222 informational, and 2,922 blocked.
- [x] Explanation shows 7/28/56 windows, eligible/stockout days, observed/adjusted velocity, inventory, incoming supply, effective levels, unrounded calculated need, applied policies, inputs, and source evidence.
- [x] Confidence is HIGH/MEDIUM/LOW with stable reason codes and does not change the calculated quantity. Counts were 26 HIGH, 86 MEDIUM, and 3,184 LOW.
- [x] Warnings and blocking reasons are visible. A critical example displayed `INVENTORY_OLDER_THAN_72_HOURS`, remained visible, and suppressed its actionable quantity.
- [ ] A live Square failure was not induced. Naturally stale and critically old required data failed safely; automated coverage verifies unavailable-read blocking. TD-026 remains open and no cached fallback claim is made.
- [x] V1 Ordering returned HTTP 200 and its existing pages and operational actions remained available and unchanged.

## Negative-control verification

Principal `9` was requalified from current production state before use:

- Identity and scope: `sr503`, active `STORE`, assigned only to active store `9` (`SR 503`), with no principal permission override and no STORE-role permission override.
- Production legitimacy: the account has 748 successful authentication events from 2026-03-02 through 2026-07-25 and had an active production session at verification time. This is operational evidence, not a username-based assumption.
- Effective capability: `management.admin=false`.
- Effective V2 feature scope: `daily_store_logs_v2` only; `ordering_intelligence_v2` is neither global nor principal-exposed.
- Credential-free authenticated verification ran inside the deployed Render image with every database transaction forced read-only, an additional SQL write-statement rejection guard, and a fail-fast Square gateway guard.
- Existing authorized route `/v2/store-operations` returned HTTP 200, displayed Daily Store Log functionality, and remained scoped to SR 503.
- Ordering Intelligence navigation was absent. Direct GET `/v2/ordering` returned HTTP 404 with `{"detail":"Not Found"}`. No recommendation marker or data was present, and the Square gateway was not reached.
- No database write statement, Square call, configuration change, or external POST/PUT/PATCH/DELETE was performed. The verification harness emitted a Starlette `TestClient` deprecation warning only; it did not affect the result or production runtime.

The owner was rechecked through the same read-only boundary after the negative control: principal `6` remained active ADMIN with `management.admin=true`, retained `ordering_intelligence_v2`, displayed the navigation item, and received HTTP 200 from GET `/v2/ordering`.

## Natural production examples

Inspect only examples that already exist. Mark every unavailable case `NOT OBSERVED`; do not create, edit, receive, submit, discontinue, or otherwise mutate a record.

| Scenario | Required evidence | Result |
|---|---|---|
| Fresh data | Source timestamps within 24 hours; actionable only if no other blocker | OBSERVED: 176 fresh rows; 152 actionable. Sample `480370H` was fresh/actionable with quantity `1`. |
| Stale or blocked data | `STALE DATA` informational display, or critical source-specific block | OBSERVED: 679 stale rows and 2,441 critical rows. Sample `1348551` displayed quantity `2 (informational)`; sample `552042W` suppressed quantity and named the critical inventory-age block. |
| Null par | Demand inference warning and MEDIUM-or-lower confidence | OBSERVED: 1,844 rows disclosed approved demand inference. |
| Zero par | Numeric zero preserved; no inferred exclusion | OBSERVED: 342 current production par rows contain a numeric zero; zero values remained calculation inputs rather than exclusions. |
| Manual lock | Locked named input disclosed; no inferred exclusion | OBSERVED: 1,452 recommendations disclosed the manual lock without excluding the SKU. |
| New/insufficient history | No fabricated demand quantity without explicit manual target | OBSERVED: 680 recommendations disclosed new-product history limitations and blocked where required. |
| Established zero sales | Zero accepted only with fresh complete data and at least 14 eligible in-stock days | OBSERVED: fresh sample `480370H` had zero 28-day observed units, 23 eligible days, a manual target, and a deterministic actionable result. |
| Confirmed stockout adjustment | Observed/adjusted velocity and removed days shown | OBSERVED: 2,194 recommendations disclosed confirmed stockout adjustment; sample `480370H` showed 5 removed days. |
| Positive `IN_TRANSIT` | PO ID and quantity shown separately; age warning over 30 days | OBSERVED: 1,394 recommendations had positive incoming supply. Sample `5795998` at Andresen showed incoming `3`; read-only backing evidence identified PO `101`, quantity `3`, ordered 2026-07-20. The page shows the quantity but not the PO ID; the result object retains the PO ID. |
| Discontinued/deleted | Actionable quantity suppressed only for confirmed status | NOT OBSERVED; no production recommendation disclosed a confirmed discontinued status. |
| Resolved non-sellable | Fresh product-resolved quantity subtracted and disclosed | NOT OBSERVED. |
| Unresolved non-sellable | Inventory left unchanged with limitation disclosed | NOT OBSERVED. |

## Success, stop, and rollback criteria

Success requires every applicable checklist item to pass, no write or V1 regression, source-specific failure behavior, and owner acceptance of explanation quality. Stop immediately for unauthorized data, pooled store inventory, an actionable critical result, hidden Square failure, unexpected write, or V1 impact.

Rollback removes only `6:ordering_intelligence_v2`, preserves all other feature entries, redeploys/restarts through the approved path, and verifies navigation absence plus direct-route 404. The pre-checkpoint application commit remains the code rollback target. No rollback condition occurred. TD-026 is accepted only for this one-principal read-only canary and remains a blocker for broader exposure.
