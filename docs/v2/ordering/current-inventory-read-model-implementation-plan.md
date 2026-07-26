# Ordering current-inventory read-model focused implementation plan

Status date: 2026-07-25. Status: **IMPLEMENTED AND LOCALLY VERIFIED; DEPLOYMENT NOT AUTHORIZED**.

This plan implements only the design in [Ordering current-inventory read-model design](./current-inventory-read-model-design.md). It does not include Stagnant Inventory, scheduled refresh, lifecycle automation, numeric range filters, recommendation snapshots, Touchscreen integration, V1 changes, staff exposure, deployment, or production refresh.

## Phase 0 — Policy gate

1. Owner approved the recommended freshness thresholds, stale display, stale sorting/filtering, partial-store aggregation, and freshness clock in `ORD-DEC-037` on 2026-07-25.
2. The design and test contract use the exact approved behavior.
3. This gate is complete; deployment remains separately unauthorized.

## Phase 1 — Additive persistence foundation

Affected files:

- additive Alembic revision `20260725_0009` after `20260725_0008`;
- `app/models.py`;
- new `app/services/v2_ordering_inventory_repository.py`;
- migration/schema tests.

Work:

1. Add `ordering_inventory_refresh_runs` and `ordering_current_inventory` exactly within the approved data contract.
2. Add checks, composite keys, foreign keys, correlation uniqueness, and lookup indexes.
3. Implement repository methods for expected variation/store population, refresh-run creation/outcome, batched valid-pair upsert, and bounded workspace reads.
4. Preserve missing/failed pairs and last-valid data; never synthesize zero.
5. Verify upgrade/downgrade on disposable PostgreSQL 16 and upgrade from `20260725_0008`.

## Phase 2 — Read-only Square refresh

Affected files:

- `app/services/v2_ordering_square_gateway.py`;
- new `app/services/v2_ordering_inventory_refresh_service.py`;
- `app/routers/v2_ordering.py`;
- `app/templates/v2/ordering/lifecycle_products.html`;
- focused gateway/service/route tests.

Work:

1. Add the canonical `/v2/inventory/counts/batch-retrieve` endpoint to the read-only allowlist without changing existing V1 or recommendation endpoint behavior.
2. Add a raw inventory-count method that:
   - chunks at no more than 1,000 variation IDs;
   - requests all active Square locations per chunk;
   - exhausts pagination;
   - returns only explicit expected pairs;
   - keeps the newest duplicate pair by `calculated_at`;
   - reports request/page counts and elapsed time;
   - never calls inside a product loop.
3. Implement the owner-only refresh service and PostgreSQL overlap lock.
4. Add CSRF-protected `POST /v2/ordering/products/inventory/refresh` with existing feature, `management.admin`, and `ordering.lifecycle.manage` gates.
5. Write one redacted audit event and one refresh-run outcome; commit valid local changes only after normalization and validation.
6. Show refresh result, coverage, request count, as-of time, and generic failure state in the lifecycle workspace.

## Phase 3 — Local workspace integration

Affected files:

- `app/services/v2_ordering_lifecycle_repository.py`;
- `app/routers/v2_ordering.py`;
- `app/templates/v2/ordering/lifecycle_products.html`;
- `app/static/v2/ordering.css`;
- lifecycle workspace tests.

Work:

1. Replace the placeholder-only row state with the batched local inventory projection.
2. Keep pre-first-refresh `Inventory unavailable` distinct from post-refresh `Unknown`.
3. Produce a total only when every required active-store pair is policy-eligible.
4. Display per-store quantity/freshness/source time/refresh time and the company coverage state.
5. Preserve sortable ascending/descending behavior under the approved stale-use policy.
6. Add `Any inventory`, `Positive inventory`, `Zero inventory`, and `Unknown inventory` filters using only policy-eligible aggregate states.
7. Keep Archived Products inventory-visible and lifecycle behavior unchanged.
8. Enforce a nine-query repository budget independent of product count.

## Phase 4 — Verification and review checkpoint

Automated coverage:

- complete all-store refresh;
- partial refresh and exact missing-pair evidence;
- failed refresh preserving every last-valid row;
- explicit zero versus absent pair;
- active-store isolation and store/location mismatch;
- missing and duplicate active-store Square location configuration;
- aggregate total and fractional/negative source preservation;
- every approved fresh/stale/critical boundary;
- stale display and approved sorting/filter behavior;
- bulk chunking at 1,000 IDs and pagination;
- no per-product Square call;
- concurrent refresh rejection;
- authorization, owner-only feature exposure, and CSRF;
- audit redaction and correlation;
- lifecycle GET zero Square, zero Touchscreen, zero write, and nine-query ceiling;
- no Square/V1/Touchscreen write path;
- Product Lifecycle and Archived Products rendering;
- V1 Ordering regression;
- migration schema/constraint/upgrade/downgrade verification.

Required verification commands/checks:

1. full PostgreSQL-backed suite with `TEST_POSTGRES_ADMIN_URL`;
2. complete available test suite;
3. focused lifecycle, gateway, refresh-service, route, authorization, CSRF, and V1 Ordering tests;
4. Python and Jinja compilation;
5. Markdown local-link validation;
6. `git diff --check`;
7. development instrumentation for Square request count, refresh duration, expected/covered/missing pairs, workspace query count, workspace response time, and response size;
8. before/after local workspace measurements with no production Square refresh.

## Review return

Before deployment approval, return:

- implementation and migration summary;
- exact files changed;
- PostgreSQL and full-suite counts;
- Square endpoint/chunk/page/request evidence;
- complete/partial/failed preservation results;
- zero-versus-missing and store-isolation evidence;
- workspace query/time/response measurements;
- confirmation of zero lifecycle GET Square/Touchscreen/write behavior;
- V1 regression result;
- rollback impact;
- new risks or debt;
- readiness classification.

No commit, push, production migration, production refresh, deployment, configuration change, or exposure broadening is included in this plan.
