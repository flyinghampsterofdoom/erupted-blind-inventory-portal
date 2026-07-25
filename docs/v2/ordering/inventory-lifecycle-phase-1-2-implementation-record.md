# Inventory Lifecycle Phase 1/2 implementation record

Status date: 2026-07-25. Classification: **READY FOR OWNER LIFECYCLE CANARY APPROVAL**. PostgreSQL 16 verification and implementation review preparation are complete. No deployment, push, feature exposure, capability grant, production schema change, V1 change, Square write, or purchase-order write occurred.

## Implemented scope

Phase 1 adds the additive `ordering_product_lifecycle` table and ORM model, a batched repository, explicit transition service, row-version conflict handling, atomic batches of at most 250 products, per-product V2 audit events with shared correlation IDs, and the no-fallback `ordering.lifecycle.manage` capability. Owner-gated management pages provide active/No Future Reorder controls and a recoverable Archived Products view. Missing rows remain implicitly `ACTIVE`; GET requests never create lifecycle rows.

Phase 2 adds one batched lifecycle lookup to the Ordering coordinator. `ARCHIVED` variation IDs are removed before variation-scoped Square inventory-count and inventory-change requests and never become recommendation candidates. `NO_FUTURE_REORDER` products continue through catalog, inventory, sales, stockout, normalization, freshness, confidence, warning, and descriptive metric processing, but reorder targets and purchase quantities are not calculated. `ACTIVE` products retain the existing policy set and calculation outputs.

Instrumentation now records active, archived, and No Future Reorder variation counts; inventory-count and inventory-change variation IDs; inventory-change pages and returned changes; endpoint and total Square request counts/timing; local database, lifecycle lookup, Square, calculation, template, and request duration; store/recommendation counts; and rendered response bytes. Logs contain counts and timings only, not Square payloads or credentials.

## Migration

Revision `20260725_0007` follows `20260720_0006` and creates only `ordering_product_lifecycle`. It performs no backfill and no lifecycle inference. Checks enforce supported statuses, trusted pre-archive values, positive versions, bounded snapshots/notes, and actor/timestamp evidence for Archived and No Future Reorder states. The status index supports lifecycle partitions.

The migration has a disposable-database downgrade for development verification. Operational rollback must not downgrade a populated production table: hide the management surface, revoke the separately granted capability, and roll application code back while retaining lifecycle rows and audit history.

## Verification evidence

| Check | Result |
|---|---|
| Full PostgreSQL-backed suite | 264 passed, 0 failed, 1 skipped, 2 warnings on loopback-only Homebrew PostgreSQL 16.12 |
| Ordering-focused suite | 41 passed; lifecycle transition, sparse default, optimistic conflict, archived pre-filter, NFR no-quantity, instrumentation, route, and Active parity coverage included |
| Unchanged V1 Ordering regressions | 28 passed |
| Migration verification | Empty upgrade, prior-head `20260720_0006` to `20260725_0007`, constraints, downgrade/re-upgrade, audit, row locking, stale conflict, and atomic rollback passed in disposable databases |
| Real R2 | One test skipped; unrelated to Ordering and credentials/opt-in are unavailable |
| Python compilation | Passed |
| Warning review | Two pre-existing FastAPI `on_event('startup')` deprecation warnings; TD-015, not introduced here |

All 60 PostgreSQL-dependent tests ran using UUID-named disposable databases and passed. The suite removed every database it created; only the two pre-existing preview databases remained afterward. One test requiring explicitly enabled isolated real-R2 credentials stayed skipped.

## Active parity

The Active lifecycle path retains the prior `ordering-phase1-2026-07-25` policy version and prior applied-policy tuple. Direct equality fixtures verify sparse implicit Active and explicit `ACTIVE` inputs send the same Square variation population and produce identical recommendations. Existing V1 raw-math parity, null/zero par, manual lock, freshness, confidence, store isolation, incoming supply, discontinued, and zero-sales tests continue to pass.

Lifecycle adds `lifecycle_status=ACTIVE` to explanation metadata and an accessible ACTIVE badge. This is explanatory metadata, not a calculation deviation. No Active quantity, target, warning, freshness, confidence, actionability, or store scope changed.

## Performance evidence

The approved pre-change production diagnostic remains the valid baseline:

| Metric | Single store | All stores |
|---|---:|---:|
| Total request | 24.795 s | 95.910 s |
| Recommendations | 824 | 3,296 |
| Square requests | 39 | 122 |
| Inventory-change time/calls | 19.555 s / 16 | 82.227 s / 68 |
| Database queries/time | 12 / 0.039 s | 12 / 0.032 s |
| Response bytes | 2,640,766 | 10,468,646 |

### Empty-lifecycle parity

An empty lifecycle table intentionally resolves every mapped variation to `ACTIVE`. Automated parity evidence verifies the same recommendation population, the same Square variation set, and no recommendation difference between sparse implicit Active and explicit `ACTIVE`. Migration alone is therefore not expected to reduce Square pagination or materially improve latency, and no such improvement is an implementation-readiness requirement.

### Post-owner-classification measurement

Performance benefit is measured only after the migration is deployed, lifecycle management is exposed to the reverified owner alone, and the owner archives a meaningful set of real irrelevant products. Do not create synthetic production lifecycle records or claim an improvement percentage before that classification exists.

Repeat the exact single-store and all-store scopes and record active, No Future Reorder, and archived counts; inventory-count and inventory-change submitted variation IDs; inventory-change pages/calls and returned changes; Square request count and elapsed time; total request time; recommendation count; and response bytes.

## Changed-path classification

Every one of the original 46 changed or untracked paths was reviewed. This verification pass adds the required `docs/v2/v2-canary-deployment-guide.md` reconciliation, producing 47 checkpoint paths. No unrelated or unexpected path is present.

| Classification | Paths |
|---|---|
| Required runtime implementation | `app/models.py`; `app/routers/v2_ordering.py`; `app/schema_contract.py`; `app/services/access_control_service.py`; `app/services/v2_ordering_data_coordinator.py`; `app/services/v2_ordering_normalization_service.py`; `app/services/v2_ordering_recommendation_service.py`; `app/services/v2_ordering_square_gateway.py`; `app/services/v2_ordering_view_model_service.py`; `app/services/v2_ordering_lifecycle_repository.py`; `app/services/v2_ordering_lifecycle_service.py`; `app/templates/v2/ordering/dashboard.html`; `app/templates/v2/ordering/lifecycle_products.html` |
| Required migration | `migrations/versions/20260725_0007_ordering_product_lifecycle.py` |
| Required test coverage | `tests/test_schema_migration_postgres.py`; `tests/test_v2_digital_signage.py` (head assertion only); `tests/test_v2_ordering_data_coordinator.py`; `tests/test_v2_ordering_lifecycle.py`; `tests/test_v2_ordering_lifecycle_postgres.py`; `tests/test_v2_ordering_recommendation_service.py`; `tests/test_v2_ordering_routes.py`; `tests/test_v2_ordering_square_gateway.py` |
| Required documentation | `docs/v2/README.md`; `docs/v2/daily-store-log-migration-and-schema.md`; `docs/v2/digital-signage.md`; `docs/v2/ordering-permission-matrix.md`; `docs/v2/ordering/README.md`; `docs/v2/ordering/business-rule-decision-register.md`; `docs/v2/render-production-v1-compatibility-profile.md`; `docs/v2/staff-scheduling-v2-foundation.md`; `docs/v2/touchscreen-flavor-finder.md`; `docs/v2/v1-v2-feature-parity-ledger.md`; `docs/v2/v2-canary-deployment-guide.md`; `docs/v2/v2-deployment-and-rollback-plan.md`; `docs/v2/v2-feature-exposure-and-cutover.md`; `docs/v2/v2-production-release-checklist.md`; `docs/v2/v2-recommended-sequence.md`; `docs/v2/v2-release-readiness-report.md`; `docs/v2/v2-schema-baseline-and-environment.md`; `docs/v2/v2-technical-debt-register.md`; `docs/v2/v2-test-verification.md`; `docs/v2/ordering/inventory-lifecycle-and-stagnant-inventory-design.md`; `docs/v2/ordering/inventory-lifecycle-blocker-matrix.md`; `docs/v2/ordering/inventory-lifecycle-owner-decision-packet.md`; `docs/v2/ordering/inventory-lifecycle-phase-1-2-implementation-plan.md`; this implementation record |
| Pre-existing owner-canary checklist change | `docs/v2/ordering/phase-1-owner-canary-checklist.md`; preserved separately and not represented as lifecycle implementation work |

## Proposed owner-only canary controls

Reverify that production principal `6` is still the legitimate active owner and retains effective `management.admin` plus the existing principal-scoped `ordering_intelligence_v2` feature. After approved migration deployment, add only a principal permission override allowing `ordering.lifecycle.manage` for that verified owner. Do not add a role override, do not change global `V2_ENABLED_FEATURES`, and do not add STORE, LEAD, staff, or general-management exposure. Preserve the existing `V2_PRINCIPAL_FEATURES` string because the Ordering feature is already owner-scoped. Rollback removes or denies only that principal capability while retaining lifecycle rows and audit history.

## Known risk and technical debt

TD-026 remains: live Ordering Intelligence reads still lack durable snapshots, a last-known-good read model, request budgets, backoff, and circuit breaking. The lifecycle management workspace no longer calls Square during render: it uses active default-vendor mappings, the existing local touchscreen catalog/inventory cache, lifecycle snapshots, and lifecycle audits. Missing cache coverage is labeled as unknown and falls back to lifecycle snapshot/SKU text. Category filtering remains unavailable until a supported local category source exists (TD-029). TD-006 now records target-environment verification as a deployment gate; local migration verification is complete.

## Readiness

The implementation is **READY FOR OWNER LIFECYCLE CANARY APPROVAL**. This does not authorize deployment, production migration, capability assignment, or exposure changes. Reduced post-archive timing is deliberately deferred until the owner has archived real products; it is a canary measurement, not an implementation-review prerequisite.
