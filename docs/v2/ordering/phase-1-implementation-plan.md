# Phase 1 V2 Ordering implementation plan

Status date: 2026-07-25. Implemented in the repository after explicit approval; not deployed or production-exposed. V1 remains canonical. The verified outcome is recorded in the [Phase 1 implementation record](./phase-1-implementation-record.md).

## Outcome

Deliver a principal-scoped, server-rendered, read-only V2 Ordering Intelligence dashboard that calculates deterministic store-level recommendations from current V1 reference/supply data and Square reads. Every result exposes inputs, formulas, warnings, freshness, actionability, and deterministic confidence. It creates no PO, changes no inventory, and performs no Square write.

## Explicit scope

Included:

- Approved policies `P1-POL-001` through `P1-POL-012` and approved confidence policy `P1-POL-016`.
- Store-isolated recommendations using trailing 28-day velocity with 7/56-day comparisons.
- Stockout-adjusted eligible days, new/zero-sales handling, confirmed status handling, V1 `IN_TRANSIT` supply, resolvable non-sellable quantity, three-state data freshness, and HIGH/MEDIUM/LOW confidence.
- Human-readable calculation evidence and stable reason codes.
- Principal-scoped feature exposure, effective authorization, captured Square fixtures, and automated policy/route tests.

Excluded:

- Preferred-vendor fallback (`P1-POL-013`), MOQ display (`P1-POL-014`), pack rounding (`P1-POL-015`), and maximum-inventory warning (`P1-POL-017`) because they remain unapproved.
- Exclusions, transfers, seasonality, recommendation decisions, overrides, PO drafts/approval/PDF, receiving, payment, COGS changes, background scheduling, persistence, and all Square writes.
- Any change to V1 formulas, routes, services, tables, or canonical ownership.

## Implemented dependency flow

```text
GET /v2/ordering
  -> V2 router: feature, capability, and store-scope checks
  -> read coordinator: V1-owned local facts + Square read gateway
  -> pure policy layer: normalize approved statuses/freshness/confidence evidence
  -> pure calculation engine: deterministic recommendation and explanation codes
  -> presentation mapper/template: display only
```

The calculation engine does not import FastAPI, SQLAlchemy sessions, templates, settings, clocks, or Square clients. The policy layer supplies explicit normalized inputs and an injected `as_of` time. Presentation cannot modify calculation or actionability.

## Affected services and modules

### Implemented files

| File | Responsibility |
|---|---|
| `app/services/v2_ordering_policy_service.py` | Immutable policy input/result types; zero/null/lock/status rules; freshness classifier; confidence classifier and stable reason codes |
| `app/services/v2_ordering_recommendation_service.py` | Pure velocity, eligible-day, stockout adjustment, store availability, incoming-supply, target/need, new/zero-sales calculations, and explanation evidence |
| `app/services/v2_ordering_data_coordinator.py` | Request-level coordinator; reads V1-owned local facts without mutation and builds normalized per-store inputs |
| `app/services/v2_ordering_square_gateway.py` | Read-only adapter for catalog, sales, inventory counts, and inventory-change evidence; returns source timestamps/completeness and supports fakes |
| `app/services/v2_ordering_normalization_service.py` | Immutable normalized Decimal/date/source inputs |
| `app/services/v2_ordering_view_model_service.py` | Presentation-only formatting and summary models |
| `app/routers/v2_ordering.py` | Thin GET-only dashboard route, feature/capability/store-scope enforcement, safe result mapping |

No new model, table, migration, repository write, task worker, or Square write client was added. The V2 gateway reuses the existing Square transport behind a strict read-endpoint allowlist and performs its own normalization without modifying V1 output contracts or callers.

### Existing files modified narrowly

| File | Implemented change |
|---|---|
| `app/routers/v2.py` | Remove the generic `/v2/ordering` placeholder handler so the native gated router owns the path; leave unrelated V2 pages unchanged |
| `app/main.py` | Include the new V2 Ordering router |
| `app/v2/navigation.py` | Add a native Ordering Intelligence destination behind `ordering_intelligence_v2`; retain separately labeled `ordering_v1_links_v2` bridge entries |
| `app/templates/v2/base.html` or page context only if required | Load Ordering stylesheet through established V2 asset convention without changing other modules |

The proposed feature key is `ordering_intelligence_v2`, absent from enabled-feature settings by default. Adding the code does not authorize global or production exposure.

## Routes and authorization

| Method and path | Capability / exposure | Behavior |
|---|---|---|
| GET `/v2/ordering` | Effective `management.admin` plus `ordering_intelligence_v2`; principal-scoped rollout | Render selected authorized store recommendations and evidence |

No POST, mutation, API write, export, calculation-run trigger, or background route is included. Store IDs from query parameters are intersected with server-resolved authorized stores. The route must not expose aggregate data from unauthorized stores. Missing or invalid store selection returns a safe validation state without a Square call.

## Templates and presentation

### Implemented files

- `app/templates/v2/ordering/dashboard.html`: store selector, freshness summary, recommendation table, empty/error states, and V1-canonical notice.
- Per-SKU calculation evidence is rendered in `dashboard.html` with no presentation-side policy calculation.
- `app/static/v2/ordering.css`: module-scoped responsive styles; no calculation logic or policy-derived JavaScript.

Required presentation rules:

- `FRESH`: show actionable label and complete evidence.
- `STALE DATA`: show complete calculations but an unmistakable informational-only label; no actionable presentation.
- `CRITICAL`: keep SKU visible, suppress actionable quantity, and name the failed/expired required source.
- Confidence is always displayed with its contributing data-quality reasons and never controls whether quantity is calculated or actionable.
- Show primary/comparison windows, eligible/stockout days, observed/adjusted velocity, store inventory, applied incoming V1 supply, applied or unapplied non-sellable quantity, raw target/need, source timestamps, and all assumptions.
- Do not show PO, approve, ignore, override, vendor-fallback, MOQ, pack, maximum, receive, payment, or Square-write actions.

## Deterministic policy details to freeze in tests

1. Use an injected `as_of` timestamp; no hidden system-clock reads in pure services.
2. Freshness boundaries: age `<=24h` is FRESH; `>24h and <=72h` is STALE; `>72h` or unavailable required data is CRITICAL.
3. STALE changes presentation/actionability only; it does not change any numeric calculation.
4. CRITICAL suppresses only the actionable quantity/presentation result, while retaining inputs and a blocking reason where safely available.
5. Confidence precedence: any LOW condition -> LOW; otherwise any MEDIUM condition -> MEDIUM; HIGH only when every HIGH requirement is satisfied.
6. Confidence never changes input normalization, formula branches, quantities, freshness class, or actionability.
7. Decimal arithmetic and explicit rounding remain deterministic. Unapproved MOQ/pack rounding is absent.
8. Store calculations never consume other-store inventory as available supply.

## Automated test plan

### New test files

| File | Coverage |
|---|---|
| `tests/test_v2_ordering_policy_service.py` | Every approved decision, freshness boundaries, confidence precedence/reasons, and proof confidence does not alter calculations |
| `tests/test_v2_ordering_recommendation_service.py` | 7/28/56 windows, stockout eligible days, new/zero-sales rules, store isolation, open supply, non-sellable evidence, exact deterministic outputs |
| `tests/test_v2_ordering_square_gateway.py` | Captured/fake Square reads, source timestamps, missing/partial/error normalization, and assertion that no write endpoint/method exists |
| `tests/test_v2_ordering_routes.py` | Disabled-by-default 404, principal exposure, effective `management.admin`, authorized store scope, fresh/stale/critical rendering, safe errors, and zero side effects |

### Existing tests modified or executed for regression evidence

- `tests/test_v2_shell.py`: replace assertions that no V2 Ordering router/service exists with native-feature-disabled/navigation-separation assertions; retain exact V1 bridge tests.
- `tests/test_purchase_order_math_service.py`, `tests/test_inventory_velocity_report_service.py`, and `tests/test_square_ordering_data_service.py`: run unchanged as V1 regression evidence.
- Full available suite and PostgreSQL integration suite when `TEST_POSTGRES_ADMIN_URL` is available.

Required decision coverage:

- `ORD-DEC-001/002/003`: zero, null inference, and lock semantics remain distinct.
- `ORD-DEC-009`: identical selected-store inputs produce identical results regardless of another store’s stock.
- `ORD-DEC-011/012`: fixed windows and stockout adjustment with zero/partial/all eligible days.
- `ORD-DEC-013/027`: new product, established zero sales with 13 versus 14 eligible days, missing history, and real zero demand.
- `ORD-DEC-014`: confirmed discontinued versus inactive/ambiguous status.
- `ORD-DEC-015`: positive/zero/stale `IN_TRANSIT` allocations and gross-to-net explanation.
- `ORD-DEC-016`: fresh resolved, stale, unresolved-identity, and absent non-sellable quantity.
- `ORD-DEC-018`: exactly 24h, over 24h, exactly 72h, over 72h, and unavailable required data.
- `ORD-DEC-019`: every HIGH/MEDIUM/LOW condition, precedence, stable reasons, and invariant numeric outputs across confidence labels.

## Implementation sequence after approval

1. Add pure policy types/classifiers and exhaustive unit tests.
2. Add pure recommendation calculations and decision-specific unit tests.
3. Add read-only Square gateway with captured fixtures and no write surface.
4. Add local read coordinator for V1-owned data, with query/no-mutation tests.
5. Add gated GET router and store-scope/security tests.
6. Add templates/styles and rendering tests for FRESH, STALE DATA, CRITICAL, and confidence explanations.
7. Update navigation/main wiring while preserving the V1 bridge.
8. Run targeted V1 regression, full available suite, PostgreSQL suite if configured, link validation, and `git diff --check`.

## Acceptance and stop conditions

Implementation is acceptable only if:

- identical normalized inputs and policy version produce identical numeric/evidence outputs;
- all approved decision and boundary tests pass;
- STALE results retain identical calculations but are informational only;
- CRITICAL results retain SKU visibility and exact blocking reasons without actionable quantity;
- confidence is derived only from approved evidence and cannot change calculations;
- no V1 route/service/model behavior changes;
- no Square write surface exists or is called;
- no PO, recommendation decision, or other operational record is written;
- the native feature is disabled by default and independently separable from the V1 bridge.

Stop and return to owner review if implementation evidence requires a policy for preferred-vendor fallback, MOQ, pack rounding, maximum inventory, exclusions, transfers, manual override, or any other deferred decision.
