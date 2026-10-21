# V2 test verification

Verification date: 2026-07-25
Repository state: owner current-inventory canary at `20260725_0009`; local minor timestamp/negative-state UX patch undeployed and uncommitted
Command: `PYTHONPATH=. TEST_POSTGRES_ADMIN_URL=<isolated PostgreSQL 16 administrator URL> .venv/bin/pytest -q`

## Result

| Result | Count |
|---|---:|
| Passed | 295 |
| Failed | 0 |
| Skipped | 1 |
| Warnings | 2 |

The complete suite ran against loopback-only Homebrew PostgreSQL 16.12 using an isolated administrator URL. Every PostgreSQL-dependent test ran and passed, including the additive current-inventory migration, prior-head upgrade/downgrade, refresh persistence, and production-sized lifecycle workspace case. Coverage was not artificially increased and skipped tests were not converted into mocks.

The focused current-inventory/lifecycle/PostgreSQL selection passed 38 tests. A separate unchanged V1 purchase-order, receiving, generation, math, Square data, and mapping regression selection passed 39 tests. New coverage renders the real lifecycle template and verifies portal-local `PDT` labeling, UTC freshness invariance, a signed negative aggregate, mutually exclusive Negative filtering, signed numeric ordering, negative per-store evidence, and continued owner-controlled lifecycle selection.

## Skip classification

| Requirement | Skipped | Affected areas | How to enable |
|---|---:|---|---|
| Real private R2 integration | 1 | Digital Signage object upload/read integration | Configure isolated R2 test credentials and set `RUN_REAL_R2_TESTS=1` |

The PostgreSQL tests create and destroy isolated test databases; they must not point at an operational database. The R2 test requires environment configuration and an external Cloudflare R2 service. No test was skipped solely because of an ordinary unit-test failure.

## External and environment coverage boundary

- PostgreSQL tests created UUID-named disposable databases only and removed every database they created. The two pre-existing preview databases were left unchanged.
- Alembic reports one repository head, `20260725_0009`. Empty upgrade, prior-head upgrade from `20260725_0008`, downgrade back to `20260725_0008`, older-chain downgrade/re-upgrade, inventory/catalog/lifecycle constraint failure, and re-upgrade behavior passed.
- R2 credentials were not configured in the audited environment.
- Square was configured read-only. The suite uses local fakes/fixtures for covered Square service behavior and did not perform real Square calls.
- Ordering tests cover allowlisted Square reads, 1,000-ID current-count chunking and pagination, explicit zero versus omitted pairs, complete/partial/failed refresh behavior, last-valid preservation, exact 24/72-hour freshness boundaries, data-quality confidence, calculation metadata, store isolation, native/bridge exposure separation, explicit lifecycle transitions, sparse Active behavior, archived pre-filtering, No Future Reorder no-quantity behavior, capability denial, CSRF route dependencies, audit metadata, a nine-query workspace ceiling, and a production-shaped 824-mapping/1,648-store-pair dataset with empty touchscreen caches.
- The two warnings are two frames of the same pre-existing FastAPI deprecation: application registration at `app/main.py` uses `@app.on_event('startup')`, and FastAPI delegates that registration through `applications.py`. Phase 1 did not introduce the handler. It remains acceptable for a narrow canary because it still executes schema validation; replacing the application lifecycle is outside this checkpoint. See TD-015 in the [technical debt register](./v2-technical-debt-register.md).

## Release implication

The minor owner UX patch is ready for deployment review but does not authorize a commit, push, deployment, migration, Square refresh, configuration change, permission change, or broader exposure. The real R2 test is not required for Ordering because Ordering has no R2 dependency. Production remains on schema `20260725_0009`; this patch requires no schema change.
