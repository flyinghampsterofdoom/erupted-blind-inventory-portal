# V2 test verification

Verification date: 2026-07-25
Repository state: Phase 1 Ordering pre-deployment checkpoint working tree based on `676b648`
Command: `.venv/bin/python -m pytest -q -rs`

## Result

| Result | Count |
|---|---:|
| Passed | 247 |
| Failed | 0 |
| Skipped | 1 |
| Warnings | 2 |

The full suite ran against local Homebrew PostgreSQL 16.12 through a loopback administrator URL. All 59 previously skipped PostgreSQL cases ran and passed. Coverage was not artificially increased and skipped tests were not converted into mocks.

The focused Phase 1 plus relevant shell suite passed 43 tests. A separate unchanged V1 Ordering regression selection passed 39 tests.

## Skip classification

| Requirement | Skipped | Affected areas | How to enable |
|---|---:|---|---|
| Real private R2 integration | 1 | Digital Signage object upload/read integration | Configure isolated R2 test credentials and set `RUN_REAL_R2_TESTS=1` |

The PostgreSQL tests create and destroy isolated test databases; they must not point at an operational database. The R2 test requires environment configuration and an external Cloudflare R2 service. No test was skipped solely because of an ordinary unit-test failure.

## External and environment coverage boundary

- The PostgreSQL suite created UUID-named disposable databases only and removed them after each fixture. No operational database was used.
- A separate disposable database upgraded through the full chain to `20260720_0006`; `alembic current` reported the single head. No Phase 1 model or migration changed.
- R2 credentials were not configured in the audited environment.
- Square was configured read-only. The suite uses local fakes/fixtures for covered Square service behavior and did not perform real Square calls.
- Phase 1 Ordering tests cover allowlisted Square reads, partial read failure, exact freshness boundaries, data-quality confidence, calculation metadata, store isolation, native/bridge exposure separation, and a no-write coordinator/gateway contract.
- The two warnings are two frames of the same pre-existing FastAPI deprecation: application registration at `app/main.py` uses `@app.on_event('startup')`, and FastAPI delegates that registration through `applications.py`. Phase 1 did not introduce the handler. It remains acceptable for a narrow canary because it still executes schema validation; replacing the application lifecycle is outside this checkpoint. See TD-015 in the [technical debt register](./v2-technical-debt-register.md).

## Release implication

The PostgreSQL result is healthy but does not authorize deployment. The real R2 test is not required for the Ordering-only canary because Ordering has no R2 dependency. Production environment, target-schema, exact owner-principal exposure, and live read-only checks remain deployment gates.
