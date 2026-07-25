# V2 test verification

Verification date: 2026-07-25
Repository state: local Inventory Lifecycle Phase 1 / Ordering Integration Phase 2 implementation; undeployed and uncommitted
Command: `PYTHONPATH=. .venv/bin/pytest -q -rs`

## Result

| Result | Count |
|---|---:|
| Passed | 264 |
| Failed | 0 |
| Skipped | 1 |
| Warnings | 2 |

The complete suite ran against loopback-only Homebrew PostgreSQL 16.12 using an isolated administrator URL. All 60 PostgreSQL-dependent tests ran and passed. Coverage was not artificially increased and skipped tests were not converted into mocks.

The focused Ordering/lifecycle selection passed 41 tests. A separate unchanged V1 purchase-order, receiving, generation, math, Square data, and mapping regression selection passed 28 tests.

## Skip classification

| Requirement | Skipped | Affected areas | How to enable |
|---|---:|---|---|
| Real private R2 integration | 1 | Digital Signage object upload/read integration | Configure isolated R2 test credentials and set `RUN_REAL_R2_TESTS=1` |

The PostgreSQL tests create and destroy isolated test databases; they must not point at an operational database. The R2 test requires environment configuration and an external Cloudflare R2 service. No test was skipped solely because of an ordinary unit-test failure.

## External and environment coverage boundary

- PostgreSQL tests created UUID-named disposable databases only and removed every database they created. The two pre-existing preview databases were left unchanged.
- Alembic reports one repository head, `20260725_0007`. Empty upgrade, prior-head upgrade from `20260720_0006`, constraint failure, downgrade, and re-upgrade behavior passed.
- R2 credentials were not configured in the audited environment.
- Square was configured read-only. The suite uses local fakes/fixtures for covered Square service behavior and did not perform real Square calls.
- Ordering tests cover allowlisted Square reads, partial read failure, exact freshness boundaries, data-quality confidence, calculation metadata, store isolation, native/bridge exposure separation, no-write coordinator/gateway contracts, explicit lifecycle transitions, sparse Active behavior, archived pre-filtering, No Future Reorder no-quantity behavior, capability denial, CSRF route dependency, audit metadata, and instrumentation.
- The two warnings are two frames of the same pre-existing FastAPI deprecation: application registration at `app/main.py` uses `@app.on_event('startup')`, and FastAPI delegates that registration through `applications.py`. Phase 1 did not introduce the handler. It remains acceptable for a narrow canary because it still executes schema validation; replacing the application lifecycle is outside this checkpoint. See TD-015 in the [technical debt register](./v2-technical-debt-register.md).

## Release implication

The lifecycle checkpoint is ready for owner lifecycle canary approval but does not authorize deployment. Empty-lifecycle parity is verified and is not expected to improve latency; performance benefit is measured after the owner archives real products. The real R2 test is not required for Ordering because Ordering has no R2 dependency. Target schema, exact owner-only capability assignment, and live checks remain separate deployment gates.
