# V2 Ordering Phase 1 implementation record

Implementation date: 2026-07-25. Status: implemented in the repository; not deployed or production-exposed. V1 remains canonical.

## Implementation summary

Phase 1 adds a GET-only `/v2/ordering` owner-preview dashboard behind the independently disabled `ordering_intelligence_v2` feature key and effective `management.admin`. The system reads current V1-owned mappings, pars, ordering settings, and positive `IN_TRANSIT` allocations plus four allowlisted Square read endpoints. It performs deterministic store-isolated recommendations and renders all inputs, calculations, applied policies, data sources, freshness, actionability, confidence reasons, warnings, and blocking reasons.

No model, table, migration, POST route, recommendation persistence, PO creation, inventory mutation, Square write, background job, environment default, or V1 service behavior was added or changed.

## Layered architecture delivered

| Layer | File | Contract |
|---|---|---|
| Square gateway | `app/services/v2_ordering_square_gateway.py` | Allowlists catalog, inventory-count, completed-order, and inventory-change reads; normalizes partial failures and source timestamps; exposes no write method |
| V1 data coordinator | `app/services/v2_ordering_data_coordinator.py` | Executes SELECT-only queries over V1-owned facts and combines them with the gateway; no `add`, `flush`, `commit`, or delete surface |
| Normalization | `app/services/v2_ordering_normalization_service.py` | Produces immutable normalized Decimal/date/source inputs and stable ordering |
| Policy engine | `app/services/v2_ordering_policy_service.py` | Implements exact 24/72-hour freshness boundaries and deterministic HIGH/MEDIUM/LOW confidence precedence |
| Recommendation engine | `app/services/v2_ordering_recommendation_service.py` | Pure approved calculation rules with injected `as_of`, stable policy version/reason codes, and complete evidence |
| View models | `app/services/v2_ordering_view_model_service.py` | Formats results for display without formula or actionability decisions |
| Route/presentation | `app/routers/v2_ordering.py`, `app/templates/v2/ordering/dashboard.html`, `app/static/v2/ordering.css` | Feature/capability/store-scope checks and read-only server-rendered evidence |

## Files created

- `app/routers/v2_ordering.py`
- `app/services/v2_ordering_data_coordinator.py`
- `app/services/v2_ordering_normalization_service.py`
- `app/services/v2_ordering_policy_service.py`
- `app/services/v2_ordering_recommendation_service.py`
- `app/services/v2_ordering_square_gateway.py`
- `app/services/v2_ordering_view_model_service.py`
- `app/templates/v2/ordering/dashboard.html`
- `app/static/v2/ordering.css`
- `tests/test_v2_ordering_data_coordinator.py`
- `tests/test_v2_ordering_policy_service.py`
- `tests/test_v2_ordering_recommendation_service.py`
- `tests/test_v2_ordering_routes.py`
- `tests/test_v2_ordering_square_gateway.py`

## Existing runtime files modified

- `app/main.py`: includes the native router.
- `app/routers/v2.py`: removes only the generic `/v2/ordering` placeholder so one gated native GET owns the path.
- `app/v2/navigation.py`: adds the independently gated native child and preserves every `ordering_v1_links_v2` bridge destination.
- `tests/test_v2_shell.py`: replaces the obsolete no-native-module assertion with native/bridge separation and principal-scope coverage.

## Test results

| Suite | Result |
|---|---:|
| Phase 1 focused policy, engine, gateway, coordinator, route, navigation, and rendering contracts | 43 passed |
| Focused V1 Ordering/math/generation/velocity/Square/mapping/receiving regression | 39 passed |
| Full default-environment suite | 188 passed, 0 failed, 60 skipped, 2 warnings |
| Full PostgreSQL 16.12 checkpoint suite | 247 passed, 0 failed, 1 optional R2 skip, 2 warnings |

At implementation review, 59 cases required `TEST_POSTGRES_ADMIN_URL`; all subsequently passed against disposable PostgreSQL 16.12 databases. The remaining skip requires explicit real-R2 configuration and is outside Ordering scope. The warnings are the pre-existing FastAPI `on_event('startup')` deprecation. No real Square call or write was executed. Template compilation and empty-dashboard rendering passed.

## V1 parity result

- All focused V1 Ordering tests passed unchanged.
- An explicit parity test proves Phase 1 equals `compute_line_recommendation` for the same 28-day demand, pack size 1, MOQ 0, store inventory, incoming quantity, and dynamic par conditions when no approved difference applies.
- Existing V1 bridge route destinations, authorization dependencies, and feature key remain unchanged and tested.
- The V1 ordering math, generation, receiving, reports, Square services, models, and templates were not edited.

## Approved deviations from V1

| Difference | Justification |
|---|---|
| Primary trailing 28 days with 7/56 comparisons instead of V1 configurable/full-window generation | `ORD-DEC-011` |
| Confirmed zero-stock days excluded from eligible demand days | `ORD-DEC-012` |
| Each store calculated independently; other-store inventory never offsets need | `ORD-DEC-009` |
| Zero, null, and manual lock remain distinct and explained | `ORD-DEC-001` through `003` |
| New products and unsupported established zero-sales results do not receive fabricated quantities | `ORD-DEC-013` and `027` |
| Only positive V1 `IN_TRANSIT` allocations count as supply; orders over 30 days are flagged | `ORD-DEC-015` |
| Only fresh product-resolved non-sellable quantity may be applied | `ORD-DEC-016`; current schema provides no such SKU link, so none is silently subtracted |
| Fresh/stale/critical actionability and deterministic confidence are displayed | `ORD-DEC-018` and `019`; confidence never alters calculations |
| MOQ and pack rounding are absent from Phase 1 output | Explicitly unapproved optional policies `P1-POL-014/015` remain disabled; the dashboard labels its quantity as unrounded calculated need |

## Technical debt and operational limitations

- **TD-026:** Phase 1 uses synchronous request-time Square reads through the existing transport. It has no persisted last-known-good cache, module timeout budget, 429 backoff, metrics, or circuit breaker. An unavailable required source therefore becomes CRITICAL rather than using cached stale data.
- Existing **TD-021** remains: durable product identity is incomplete. Phase 1 is intentionally limited to active default-vendor mappings with a Square variation ID.
- Current non-sellable tables have no SKU/variation link, so the approved subtraction rule is implemented but does not fabricate a match.
- A real PostgreSQL-backed authenticated route test remains part of pre-canary verification under TD-001 even though this slice adds no schema.

## Rollback impact assessment

Immediate operational rollback is removal of `ordering_intelligence_v2` from the affected principal/global exposure value. That hides native navigation and makes direct route access return 404 while leaving `ordering_v1_links_v2` and all V1 routes unchanged.

Code rollback removes the native router include/navigation child and restores the generic placeholder handler. There is no database, migration, Square, file-storage, audit-record, or user-created V2 data rollback because Phase 1 writes nothing. No reconciliation is required.

## Review decision

Implementation review is approved. Deployment and production feature exposure remain unapproved. PostgreSQL-backed verification has passed; before canary approval, validate the deployed schema/environment, verify the exact individual owner principal and current exposure string, and review the [owner-canary checklist](./phase-1-owner-canary-checklist.md) plus TD-026 latency/failure expectations.
