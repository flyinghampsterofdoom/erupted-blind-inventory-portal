# Purchasing forecast validation foundation — local implementation

October 22, 2026. Branch `codex/forecast-validation-20261022`, based on production commit `1f487a7f0765f15695cd5d305afb469d522a7d90`. **Not deployed. No production collection, schedule, order, setting, vendor association, inventory, or saved decision was changed. Reds PO #139 was not modified or regenerated.** The existing audit files were replayed offline.

## Architecture and boundaries

The new `app/forecast_validation` package calculates four shadow models from one frozen evidence snapshot. It imports no purchasing calculation or order-writing service. Demand calculation (`demand.py`), shared inventory policy (`policy.py`), source capture (`capture.py`, `exposure.py`), atomic publication (`runner.py`), and mature-outcome scoring (`evaluation.py`) are separate. Exact rational arithmetic is retained through integer policy decisions.

The web application adds four disabled configuration fields and registers a separate GET-only router. Existing purchasing services, templates, JavaScript, models, Alembic migrations, settings and cron scripts are unchanged. Ordinary purchasing requests never open evidence storage, collect sources, or calculate shadow forecasts. The dashboard reads recorded results; it has no purchasing actions or mutation endpoints.

Evidence uses a **separate PostgreSQL database**, not extra business tables. SQLite is permitted only for local/test use. Store construction refuses the configured business database identity, and production refuses SQLite. Main business schema remains revision `20261021_0048`.

The optional source worker requires an explicit separately provisioned SELECT-only business connection and enforces read-only repeatable-read transactions, 10-second SQL statements and 2-second lock waits. It never creates business rows. Square catalog, inventory and inventory-history requests use the existing read gateway and centralized Square request policy. There are no Square mutations, inventory writes, financial postings, PO generation, emails, or employee-facing recommendations.

The two read routes are `/management/forecast-validation` and `/management/forecast-validation/evidence/{run_id}`. They use existing purchasing Owner verification: the authenticated identity must equal the configured active Owner principal; an ADMIN role alone is insufficient. Disabled routes return 404. Responses use `Cache-Control: no-store`; source payloads are escaped. Access is independent of the employee purchasing workspace.

## Frozen definitions

Model identity and version: `audit-20261022-v1`. Labels follow Justin's new request: **C Conservative, D Verified**; the earlier audit used these letters in the opposite order.

| Model | Exact definition |
|---|---|
| A — Raw84 | Sum observed gross units / 84 completed calendar days. |
| B — Current Production | Production 56-day long window inside 84 days, spike clipping, availability exclusions and 2× observed cap, sustained trend weights/cap, new-product trimming/shrinkage, recent-zero and verified aged-slow rules. Same lifecycle and missing-evidence abstentions. No formula tuning. |
| C — Conservative Adjustment | Raw84 unless at least 28 verified positive-exposure days, at least 7 positive-sales days among them, and no more than 10% unknown/partial days. If qualified, `raw + 0.5 × max(0, min(verified, 1.5 × raw) − raw)`. Maximum uplift 25%. |
| D — Verified Exposure | Units on verified full positive-stock calendar days / those days, requiring at least 14 days. Otherwise **abstain**, with no implicit fallback. |

B0 is a separate control: B with all availability classifications unknown. It retains B's 56-day, clipping, trend and zero logic. It is not Raw84. The control has its own shared-policy outputs.

Verified exposure is the audit's **calendar-day proxy**, not proof of historical selling hours. A forward inventory ledger begins only at a historical absolute IN_STOCK physical count. Physical-count days, negative balances, missing anchors and inconsistent ledgers are unknown. Intraday transitions are PARTIAL. The forward ledger must reconcile to the observed current count; it is never repaired using that count. The normalized business inventory table does not preserve absolute physical-count quantities, so the collector separately retains bounded raw Square inventory events. Missing raw anchors yield explicit abstention.

Every rate flows through one independent fixed policy: reorder level `ceil(rate × max(28, lead_days))`; target `ceil(rate × max(56, lead_days))`; position = live on-hand + qualified incoming. Trigger is inclusive `position <= level`; need = `ceil(max(target-position, 0))`. Sum four store needs, then round once to the SKU pack multiple. Unknown position/demand/pack or blocked lifecycle abstains. Missing cost permits known quantity but makes cost unknown. Any unresolved store makes the SKU quantity unresolved; a subtotal is never labeled complete.

No manual par settings are changed or applied to the common comparison policy. The policy is deliberately fixed 4/8 even if the existing application defaults differ. Lead-time extensions are frozen from at least three distinct observed supplier POs, using the existing median definition. Forecast outputs include exact rate, demand28/56, parameters, reasons and confidence. Prediction intervals are explicitly **uncalibrated**, not manufactured from a point estimate.

## Evidence and revisions

`fv_runs` atomically stores the entire source snapshot, all four models, B0, all store policies and packed SKU exposures in one immutable payload. It includes timestamps, cutoff, four store/location mappings, variation/product/SKU identities, 84 raw daily quantities, separate B and independent availability arrays, source coverage, on-hand/count timestamps, incoming commitments, local costs, pack rules, lifecycle, lead inputs, raw source records with IDs/versions/known/occurred times, model/policy versions, parameters, limitations and observational receipts/order commitments. Hashes verify retained payloads; they do not replace source records.

A unique `(scope, normalized UTC origin)` prevents duplicate runs. Identical retries return the existing ID; changed inputs under the same identity raise a conflict. Publication is atomic across all models. A process crash after commit can be retried without duplicate forecasts; an attempt log may be absent if the process dies after commit. A new forecast has a new origin, never an overwritten historical row.

`fv_evaluations` uniquely identifies `(run, horizon, source revision)`, retains raw outcome quantities/states/source records, cases, metrics, coverage, observational events, and an optional validated `supersedes_id`. Same-revision changed evidence conflicts; a later correction gets a new explicit revision. All revisions remain visible. No “latest truth” silently replaces the original score. A compact immutable summary supports historical trend reads without loading every outcome payload.

`fv_attempts` appends completion, explicit abstention/incomplete status and failure information. Command failure exits nonzero. Provider process logs remain necessary for database outages or hard process termination, when no database failure row can be written.

PostgreSQL triggers reject UPDATE, DELETE and TRUNCATE on all three evidence tables. SQLite rejects UPDATE/DELETE. Application roles must not own tables or have DDL, trigger-management, DELETE, UPDATE or TRUNCATE privileges. These controls protect against ordinary application mutation, not a malicious database superuser. Database backups and operator audit trails remain necessary.

Suggested retention is **540 days from capture**, with expiry metadata and a quarterly Owner/DBA archival review. The application deliberately has no purge or trigger-disable endpoint. Before production activation, approve an encrypted retention job/policy covering primary rows, evaluation revisions, exports and backups, including bounded backup expiry; a DBA-controlled archival/purge is a separate audited operation. Do not run a destructive expiry process as the forecast writer. Store no customer identities or payment credentials. Source order/line IDs and business quantities are retained only for reconciliation.

## Prospective and scoring rules

Training uses the 84 completed Pacific calendar days preceding the origin's date. Publication must occur no more than 15 minutes after an origin labeled PROSPECTIVE; old imports must be explicitly RETROSPECTIVE or SYNTHETIC. All source records must have occurred and been known by cutoff, and cutoff cannot exceed origin. Every source row preserves full payload/version. Store/SKU scope is identical across the four models, including explicit unknown rows.

Evaluation begins **next Pacific midnight**. Both observed time and source cutoff must be after all 28 or 56 days conclude, and cannot be in the future. This deliberately leaves the origin's partial day out. There is no hindsight recalculation of the frozen prediction.

CONTINUOUS requires complete sales and every outcome day verified fully in stock. PARTIAL requires complete sales and no unknown availability but some non-full days; only verified IN_STOCK days are scored, with at least 7 such days. UNKNOWN rows never enter definitive accuracy metrics. Stockout/partial-day sales are not zero demand truth. The demand-rate comparison over verified available days assumes time-homogeneous exposure; partial-period results remain secondary because availability can select demand.

All four rates must exist for a store/SKU to enter the common comparison. Store/category/vendor/SKU stratification remains separated by cohort. For each model, retain MAE, WAPE, signed bias, cost MAE/WAPE/bias, over/under frequency, sample count, actual-unit denominator, cost-known count, abstentions and availability coverage. Zero actual denominator means WAPE is undefined. Fixed origin costs weight all models; actual future price changes do not create a spurious model advantage. All-model comparisons can be selective when D abstains, so show the omitted population and never infer performance for unsupported SKUs.

Scores remain separated by origin, horizon and revision; overlapping weekly origins and the 28/56 windows are not independent trials. The foundation displays sample sizes and uncertainty limitations and does not compute a winner. Prospective multi-origin confidence intervals and a full inventory simulation are separate later analysis. Actual receipts, corrections, closures, supplier commitments, normalized inventory deltas and raw transfer/adjustment states are observational evidence, distinct from hypothetical policy outputs. No causal or realized-savings claim follows from these comparisons.

## Source adapter limits

Local sales come from immutable consignment source facts plus current line corrections, independent of financial attribution. Missing/stale/failed certified coverage is incomplete rather than zero. Current inventory comes only from Square counts. Missing inventory is unknown. Source observations use an isolated read-only database snapshot, followed by a single published origin after external reads; the read span and source timestamps are retained rather than claiming a simultaneous global transaction across Square and PostgreSQL.

The automatic source adapter uses unique canonical vendor/configuration evidence. Missing/ambiguous mappings and pack rules abstain. Historical cost fallback and owner financing/current-cost decisions are not silently approximated: applicable owner decisions are retained and their costs are unknown until their source basis is reconciled. This can produce less cost coverage than the live purchasing workspace and is a **manual activation review item**, not permission to change those decisions. Category IDs are retained; category labels/substitutions and historical opening hours are not inferred. Stock age defaults unknown.

The bounded outcome collector uses the retained 84-day capture window; collect a 56-day outcome within 28 days of maturity. Older unsupported periods fail explicitly and require an independently certified evidence file. Deleted/inactive variations absent from a fresh source capture remain missing, not zero. Late source corrections require explicit new revisions. No historical evidence is reclassified as prospective.

## Cadence and operational cost — proposal only

Existing scheduling uses `scripts/release/cron.sh` → `scripts/run_schedule_automation.py` with its own release gate. Do not add forecast work to that job. Prefer a **separate Render cron service** with dedicated read/append credentials after manual commissioning. Render documents single-active-run behavior, UTC schedules and active-time billing with a $1/month minimum per cron service: [Render Cron Jobs](https://render.com/docs/cronjobs), checked October 22, 2026. Database storage/backups are an additional cost; no paid resource has been provisioned.

Propose weekly capture on Tuesday at 11:30 UTC (03:30 PDT / 02:30 PST), after existing overnight source refresh is confirmed complete. Score mature 28/56 outcomes on that same weekly run, then explicitly append any late-source revision at the following review. No daily poll is needed initially. A designated origin every four weeks supplies non-overlapping 28-day primary cohorts; every eight weeks supplies non-overlapping 56-day cohorts. Other weekly origins are diagnostics. Missing/stale coverage produces a visible incomplete run or failure, never a refresh of business data from this worker.

Current manual commands cap source requests at **40 per capture**, each at at most 15 seconds, aggregate API deadline 180 seconds, process capture hard limit 240 seconds, maximum 1,500 variations / 6,000 store pairs, and forecasting loop budget 30 seconds. Repeated cursors fail. There are **no internal HTTP retries**: an operator may retry once after reviewing the cause, reusing the frozen input file and origin when publishing. A changed snapshot is a new origin. Before scheduling, use an external durable execution manifest keyed by cadence/scope to avoid re-capturing a new origin when the same scheduled slot is retried. No scheduler implementation or production schedule is enabled in this foundation.

At steady state, one capture plus two newly mature outcomes means at most **120 Square reads/week** and three bounded source processes (12 minutes maximum combined wall time under the per-process cap), plus offline calculation/scoring. One manually approved retry can double that week's ceiling. These are design ceilings, not measured production consumption. If source pagination cannot exhaust within budget, fail and reduce an explicitly approved scope; do not silently sample. Measure actual payload size, query count, coverage and duration during a manual pilot before choosing capacity or adding scopes.

## Local operation

Use the repository's existing Python dependencies. No new runtime package or business migration is required. For a local test environment only, configure `ENVIRONMENT=test`, `FORECAST_VALIDATION_WORKER_ENABLED=true`, and `FORECAST_VALIDATION_DATABASE_URL=sqlite:////absolute/path/evidence.sqlite`. Never put credentials into evidence JSON or logs.

```sh
python -m app.forecast_validation.cli init
python -m app.forecast_validation.cli forecast --file /absolute/path/frozen-snapshot.json
python -m app.forecast_validation.cli evaluate --run-id RUN_ID --file /absolute/path/outcome-v1.json
python -m app.forecast_validation.cli status
```

`capture` requires an explicit PostgreSQL `FORECAST_VALIDATION_SOURCE_URL` with a SELECT-only role and existing Square read credentials. `capture-outcome --run-id RUN_ID --horizon 28 --revision source-v1` captures mature outcomes. Revisions supplied as files may include `supersedes_id`. These commands are opt-in operator tools, never invoked by an HTTP GET. `init` is explicit provisioning; workers and dashboard never auto-create tables. Never run the local synthetic preview's dependency overrides in the production application.

For the web process, set only `FORECAST_VALIDATION_ENABLED=true` and a SELECT-only evidence DB URL after approval. Leave `FORECAST_VALIDATION_WORKER_ENABLED=false` in the web process. The worker gets SELECT+INSERT on evidence, SELECT on required source tables, read-only Square access; the dashboard gets SELECT only. A separate provisioning identity creates schema/triggers and is absent from runtime. Revoke PUBLIC schema creation, set role statement timeouts, require TLS/private networking and encrypted managed backups. Do not share the business write role with this worker.

## Deployment, activation, rollback and decision criteria

**Recommendation: approve deployment of the disabled foundation only after code review; do not activate automatic collection or scheduling yet.** Keep both flags false and URLs unset. This deployment registers the hidden Owner route but does not provision a database, collect a forecast, alter purchasing, or add a billable cron. No approval to deploy has been received or acted upon.

Before separately approving manual collection: provision isolated managed PostgreSQL and narrowly scoped roles; run the explicit evidence initializer; verify trigger protections under the runtime role; enable encrypted backups/PITR with expiry; restore a backup into another isolated database; verify source coverage, selected scope, owner-decision/cost reconciliation and Square request budget against a single read-only capture; freeze that capture and review all abstentions. Confirm deployment commit/model version and keep the web worker flag off. Do not use an existing order as an action target; #139 stays untouched.

Only after the pilot should Justin approve the proposed cadence, resource cost, durable cadence execution manifest, failure alert destination and retention policy. Alert only for failure, unexplained missing coverage or an action needed; do not send vendor orders or employee recommendations. Scheduling orchestration, alert delivery and managed resources are intentionally unactivated and must not be treated as already operational.

Rollback: set both flags false, stop only the new worker/service, and revert the code release if necessary. No business schema downgrade is needed. Leave evidence in place and retain its backup; the previous purchasing release ignores it. A failed/partial source capture publishes no forecast. A failed evaluation leaves the original forecast unchanged. Restart/retry uses the same immutable input and revision. Dedicated database custom-dump/restore, content equality and trigger survival were verified locally with PostgreSQL 16; production backup provisioning has not occurred.

Before considering replacement, pre-register a stable cohort and criteria. Proposed minimum: 6 non-overlapping 28-day origins, 3 non-overlapping 56-day origins, at least 100 distinct SKUs, all four stores and each material vendor/category represented; at least 80% eligible coverage or an explicit supported-segment restriction. Require at least 10% relative reduction in primary WAPE and cost WAPE versus B and against Raw84, with a paired 95% interval clustered by SKU and origin excluding no improvement. Bias should stay within ±10% and no material store/vendor/category should worsen WAPE by more than 5 percentage points without explicit review. Report coverage, correlated periods and any exclusions. These thresholds are proposed business acceptance standards, not observed achievements or an automatic promotion rule.

Review purchasing exposure and observed service/inventory/aging outcomes separately. A future policy experiment or inventory simulator must carry prior hypothetical orders, delivery delays and receipts forward. Snapshot differences alone are not realized working-capital savings. A replacement requires Justin's separate approval and its own implementation/release; this foundation never promotes a model.
