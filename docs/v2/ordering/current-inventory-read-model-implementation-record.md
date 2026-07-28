# Ordering current-inventory read-model implementation record

Status date: 2026-07-25. Classification: **READY FOR MINOR OWNER UX PATCH DEPLOYMENT**. Commit `5a8558077581ae59d83e102e1aad6b8ef1040411` and schema `20260725_0009` are live for owner principal `6`. The accepted first refresh covered `3288/3296` store/variation pairs; every omission remains visibly Unknown and no partial total is presented. A local, undeployed minor UX patch adds portal-local timestamp presentation and a distinct trusted-negative state without changing schema, persistence, freshness, lifecycle, Square, V1, Touchscreen, or exposure behavior.

## Implemented scope

Revision `20260725_0009` adds an Ordering-owned last-valid current-inventory model and immutable refresh-run evidence. An explicit owner-only, CSRF-protected refresh route uses a count-only Square gateway, bulk chunks of at most 1,000 variation IDs, pagination, one PostgreSQL advisory transaction lock, strict expected-pair coverage, generic failure evidence, and one correlated audit event. Only explicit parseable `IN_STOCK` store/variation pairs are persisted; omission is never converted to zero.

Product Lifecycle and Archived Products remain local-only GET surfaces. They project the latest inventory evidence in three additional bounded queries, display a trusted company total only when every active store is Fresh, show per-store last-valid evidence and blocking reasons, and support Any, Positive, Zero, Negative, Unknown, and Stale review filters. A trusted negative total remains signed, receives a prominent warning, sorts numerically, exposes its contributing stores, and remains eligible for normal owner-controlled lifecycle actions. Persisted UTC retrieval/source timestamps render in portal-local time with `PST`/`PDT`; freshness calculations remain UTC-based.

The implementation has no recommendation, purchase-order, lifecycle-transition, V1, Touchscreen, scheduled-refresh, worker, or Square-write path.

## Migration

`20260725_0009` follows `20260725_0008` and creates:

- `ordering_inventory_refresh_runs`: immutable outcome, coverage, timing, request-count, sanitized error, actor, and correlation evidence;
- `ordering_current_inventory`: one last-valid row per Square variation and store, including exact numeric quantity, location identity, Square source time, successful retrieval time, and source refresh run.

Checks enforce supported outcomes/freshness labels, non-negative evidence counts, exact coverage arithmetic, valid outcome shapes, and time ordering. The migration performs no backfill and changes no existing table. Empty upgrade, prior-head upgrade, downgrade to `0008`, and re-upgrade passed on disposable PostgreSQL 16.12. Operational rollback retains populated evidence and rolls application code back to `0008`; the downgrade is for disposable verification only.

## Freshness and failure evidence

| Case | Verified behavior |
|---|---|
| Exactly 24 hours | Fresh; eligible for trusted aggregate, numeric sort, and Positive/Zero classification |
| More than 24 through exactly 72 hours | Stale; last-valid value remains visible with age, but is excluded from trusted aggregate and numeric Positive/Zero use |
| More than 72 hours | Critical/Unknown operational result; labeled last-known value remains visible |
| Complete refresh | Every expected active-store pair is covered; explicit zero remains zero |
| Partial refresh | Only returned pairs update; omitted pairs retain prior quantity and run provenance; aggregate is Unknown |
| Failed refresh | A failed run and redacted audit evidence are written; no current row changes; aggregate is Unknown |
| Missing/mismatched location | The affected store blocks the total and displays the exact configuration reason |
| Overlap | Advisory lock rejects the second refresh before any Square request |

Square `calculated_at` remains separate evidence. Effective age is always computed from the successful Ordering retrieval timestamp.

## Access and data-boundary evidence

- `POST /v2/ordering/products/inventory/refresh` requires `ordering_intelligence_v2`, effective `management.admin`, explicit `ordering.lifecycle.manage`, and a valid CSRF token.
- Product Lifecycle GET uses nine local bounded queries for any product count. A production-shaped PostgreSQL test confirms no INSERT, UPDATE, or DELETE and no query referencing a `touchscreen_*` table.
- The lifecycle repository imports no Square or Touchscreen service. Square is called only by the explicit refresh POST.
- The new gateway allowlists only the canonical read operation `/v2/inventory/counts/batch-retrieve`; it exposes no write/adjustment method.
- One 1,001-variation fixture required two chunks and three Square requests because the first chunk had a second page. No Square call occurs inside a product or variation loop.
- Existing V1 Ordering regression selection passed unchanged.

## Verification results

| Check | Result |
|---|---|
| Complete PostgreSQL 16.12 suite | 295 passed, 0 failed, 1 skipped, 2 warnings |
| Focused inventory/lifecycle/migration/routes/gateway selection | 38 passed |
| Unchanged V1 Ordering selection | 39 passed |
| Optional real R2 | 1 skipped; isolated credentials and explicit opt-in unavailable, unrelated to Ordering |
| Warning review | Two frames of pre-existing FastAPI `on_event('startup')` deprecation, tracked by TD-015 |
| Migration | Empty, `0008`→`0009`, `0009`→`0008`, constraints, and re-upgrade passed |
| Python/Jinja/Markdown/diff checks | Passed |

Minor UX patch verification updated the complete PostgreSQL result to 295 passed, zero failed, one optional real-R2 skip, and the same two pre-existing FastAPI deprecation warnings. The focused lifecycle/current-inventory/migration/route/gateway selection passed 38 tests; the unchanged V1 Ordering selection passed 39. All 98 Jinja templates compiled, all 102 Markdown documents decoded with every local link resolving, Python compilation passed, and `git diff --check` passed.

The real Product Lifecycle template was rendered with a signed `-3` aggregate, a `NEGATIVE INVENTORY` badge, signed `-4` and `1` per-store evidence, a per-store negative warning, `PDT` retrieval and Square-source labels, and an enabled lifecycle selection control. Freshness assertions use the original UTC instant and remain unchanged after presentation-time timezone conversion.

## Local performance evidence

The same disposable PostgreSQL 16.12 host and production-shaped population were used before and after: 824 mapped variations, 820 named identities, four intentionally unnamed identities, two active stores, page size 50, and no production Square request. The after case included 1,648 explicit Fresh inventory rows.

| Metric | `0008` placeholder baseline | `0009` current-inventory implementation |
|---|---:|---:|
| Bounded repository queries | 6 | 9 |
| Default workspace projection | 58 ms | 98 ms |
| Default workspace projection + template render | 69 ms | 150 ms |
| Rendered response bytes, 50 rows | 63,146 | 104,003 |
| GET-time Square requests | 0 | 0 |
| GET-time Touchscreen reads | 0 | 0 |
| GET-time writes | 0 | 0 |

These are local diagnostic samples, not latency service-level guarantees. The added cost is bounded local retrieval and per-store disclosure rendering; query count does not grow with product count. Runtime structured logs retain workspace projection/render/response metrics and refresh expected/covered/missing pairs, Square request count, and duration.

## Changed paths for this focused implementation

Runtime and migration:

- `app/models.py`
- `app/routers/v2_ordering.py`
- `app/schema_contract.py`
- `app/services/v2_ordering_inventory_refresh_service.py`
- `app/services/v2_ordering_inventory_repository.py`
- `app/services/v2_ordering_lifecycle_repository.py`
- `app/services/v2_ordering_square_gateway.py`
- `app/static/v2/ordering.css`
- `app/templates/v2/ordering/lifecycle_products.html`
- `migrations/versions/20260725_0009_ordering_current_inventory.py`

Verification:

- `tests/test_schema_migration_postgres.py`
- `tests/test_v2_digital_signage.py` (schema-head expectation only)
- `tests/test_v2_ordering_inventory_refresh.py`
- `tests/test_v2_ordering_lifecycle_postgres.py`
- `tests/test_v2_ordering_lifecycle_workspace.py`
- `tests/test_v2_ordering_routes.py`
- `tests/test_v2_ordering_square_gateway.py`

Documentation was reconciled across the Ordering design/plan/decision/workspace/index, schema baseline, deployment/checklist, roadmap, test verification, readiness report, parity ledger, module ancestor references, and technical-debt register. Previously captured Product Lifecycle responsive evidence files remain separate owner-canary evidence and are not current-inventory implementation artifacts.

## Risks and technical debt

- TD-026 remains for broad Ordering read architecture. This checkpoint adds only owner-triggered current counts; it does not add durable sales history, retry/backoff/circuit-breaking, scheduled refresh, or Stagnant Inventory evidence.
- TD-029 records the accepted production coverage gaps plus the still-deferred scheduled refresh and broader inventory-state/store-relevance workflow work.
- A synchronous owner refresh can take multiple Square pages. The route prevents overlap and records duration/request counts but deliberately adds no worker or concurrency.
- Pure Square network duration is not persisted separately from the broader read window.

## Readiness

**READY FOR MINOR OWNER UX PATCH DEPLOYMENT**

The minor UX patch requires implementation review and separate deployment approval. It does not authorize a commit, push, deployment, migration, production refresh, configuration/permission/exposure change, or staff rollout.
