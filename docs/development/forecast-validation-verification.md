# Forecast validation — verification receipt

October 22, 2026. Local implementation only. Production deployment and scheduling are unapproved and unactivated.

## Results

The purchasing, PO, new forecast-validation and Square-boundary regression run completed with **406 passed, 1 skipped** in 161.12 seconds. The only skipped check requires explicit hosted-test R2 credentials and an isolated object prefix; no production storage credentials were used. Final focused acceptance after the last source-evidence changes completed with **30 passed** in 7.28 seconds. These 30 checks are a subset of the suite, not 30 additional distinct regression tests.

Python 3.10 release-test environment, FastAPI 0.129.0, SQLAlchemy 2.0.54, PostgreSQL 16. Matching PostgreSQL 16 backup tools are required in PATH. The first broad run selected PostgreSQL 17 tools and failed an existing PDF restore test on `transaction_timeout`; the matching-tool rerun passed it. Remaining warnings are existing FastAPI lifespan, Starlette TestClient and SQLAlchemy cyclic-table warnings.

| Requirement | Evidence |
|---|---|
| Preserve purchasing calculations | No edits to existing purchasing services, models, migrations, settings values, templates, JS, or cron. Only configuration declarations and router registration integrate the package. |
| Reproduce the audit | Offline replay of all 360 frozen store/SKU pairs against the independent audit scripts; every A/B/C/D rate agrees, including explicit D insufficiency. |
| Current model parity | 1,500 deterministic cases compare shadow B against the production calculator with identical inputs, including age, lifecycle, availability, inventory, incoming, lead time, clipping and policy quantities. Exact integer thresholds/targets/needs agree; displayed velocity agrees at production precision. |
| Shared policy | Hand-checked constant demand, inclusive reorder threshold, fractional/negative inventory, cross-store pack rounding and missing evidence; same function for all models and B0. |
| Freeze before outcomes | Future source/cutoff rejection, backdated PROSPECTIVE refusal, completed-day training contract, next-Pacific-midnight evaluation and 28/56 maturity checks. |
| Immutable/recoverable evidence | Restart read equality; update/delete/truncate rejection; restricted runtime role cannot disable triggers; PostgreSQL custom dump/restore preserves full payload and trigger enforcement. |
| Idempotence | Eight concurrent publications yield one ID and one run. Identical retry is stable; changed evidence under an existing identity conflicts. |
| Failure and missing evidence | Timeout publishes no run; safe retry succeeds; failed attempts retained; missing history/inventory/verified exposure/whole outcome rows are explicit unknowns or abstentions. |
| Source integrity | Real disposable PostgreSQL read-only capture, fractional sales retained exactly, full source payload/version retention, historical absolute anchors and forward exposure, current-count reconciliation, repeated-pagination-cursor failure, and 40-request wrapper bound. |
| Outcome revisions | Mature evaluations append; repeated revision is idempotent; corrected analysis retains original; PARTIAL scores use only verified full available days; UNKNOWN never becomes zero demand. |
| Owner authorization | Configured Owner succeeds; other ADMIN and manager identities denied; disabled flag returns 404; POST returns 405. Existing active-principal verification is reused. |
| Business isolation | Identical row-content fingerprints across **all 199 business tables** before/after forecast and evaluation on a separate evidence database. Capture issues SELECT/SHOW only and source transactions are read-only. This is local disposable-data proof, not a production database extraction. |
| Ordinary page performance | 30 measured requests after five warmups: baseline median **3.06 ms**, with new route **2.82 ms**; **2 SQL queries/request** in both; **zero** evidence/source/forecast calls. Small timing differences are noise, not a speedup claim. |
| Dashboard cost | Final local mean **29.5 ms** over ten Owner requests, with zero source/forecast calls. PostgreSQL local fixtures and small frozen inputs; production large-payload performance is not inferred. |
| Browser QA | Model table, incomplete exposure subtotal, B-versus-B0 effect, reliability and SKU investigation visibly inspected; three saved screenshots use explicitly SYNTHETIC examples. |

Offline audit reproduction returned:

| Quantity | Result |
|---|---:|
| A Raw84 exposure | $10,674.00 |
| B Current exposure | $15,411.00 |
| C Conservative exposure | $11,626.50 |
| B with availability flags disabled | $9,367.50 |
| B availability-adjustment effect | **$6,043.50** |
| D Verified store-pair abstentions | **22** |

These are replayed audit calculations, not new orders or new live recommendations. D has no implicit raw fallback; unresolved stores prevent a definitive complete SKU exposure. Reds PO #139 was never updated or regenerated.

## Reproduction

From the implementation worktree, using a disposable local PostgreSQL URL and matching client binaries:

```sh
PATH=/opt/homebrew/opt/postgresql@16/bin:$PATH \
P1_TEST_DATABASE_URL=postgresql+psycopg://localhost/erupted_purchasing_p1_test \
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://localhost/postgres \
python -m pytest tests/test_purchasing*.py tests/test_purchase_order*.py \
  tests/test_forecast_validation*.py tests/test_square_write_boundary.py \
  tests/test_square_cost_workflow_boundaries.py -q -rs

PYTHONPATH=. python scripts/verify_forecast_validation_audit.py \
  /Users/justinrawlinson/Developer/Erupted/output/stockout-demand-audit-20261022
```

No test uses real Square network access. PostgreSQL fixture databases and role names are disposable and cleaned up. Backup/restore proof uses those local databases only.

## Practical limits and deployment recommendation

There are no actual prospective business outcomes in the synthetic preview. Prediction intervals are explicitly uncalibrated; no model is declared superior. Existing historical evidence is not relabeled prospective. The collector fails on a scope that cannot fit its request/time limit, rather than silently returning a subset. Owner cost/financing decisions require source reconciliation; corresponding costs remain unknown. Managed backup, retention, failure alert delivery, durable cadence deduplication and production-scale pilot measurements must be commissioned before scheduling.

Deploy **disabled only**, after code review and Justin's explicit deployment approval. Provisioning, one manual capture, Owner review, then scheduling are distinct later approvals. Both feature flags default false. Rollback disables the new component and retains its separate evidence database; the business schema and purchasing calculations require no reversal. See `forecast-validation-foundation.md` for the architecture, full definitions, source contract, cadence cost ceiling, role separation, retention, activation sequence and proposed future promotion standards.
