# Purchasing V3 Phase 2 local implementation report

Local implementation is ready for owner review. Production deployment, production migrations, Square inventory writes, paid AI and Phase 3 have not been performed. The new workspace remains disabled by default through `purchasing_v3_phase2_enabled=False`.

The functional implementation and isolated acceptance workflow are verified below. Real historical forecast accuracy and visual acceptance on owner devices remain unverified. Recommendation: provisionally accept the local functional implementation and require those two acceptance checks before approving operational rollout.

## 1 Exact changed files

These changes are measured against the Phase 2 starting snapshot, preserving the pre-existing Phase 1 work in the same worktree. No commit or merge has been created.

Modified:

- `app/config.py`
- `app/main.py`
- `app/models.py`
- `app/routers/management.py`
- `app/routers/v2_reporting.py`
- `app/schema_contract.py`
- `app/services/inventory_velocity_report_service.py`
- `app/services/purchase_order_generation_service.py`
- `app/services/purchase_order_math_service.py`
- `app/services/v2_ordering_data_coordinator.py`
- `app/services/v2_ordering_normalization_service.py`
- `app/services/v2_ordering_recommendation_service.py`
- `app/services/v2_ordering_square_gateway.py`
- `app/services/v2_replenishment_report_service.py`
- `app/templates/management_ordering_order_detail.html`
- `app/templates/management_ordering_tool.html`
- `app/templates/v2/ordering/dashboard.html`
- `app/templates/v2/reporting/workbench.html`
- `tests/test_canonical_migration.py`
- `tests/test_inventory_velocity_report_service.py`
- `tests/test_purchase_order_math_service.py`
- `tests/test_purchasing_phase1_corrections.py`
- `tests/test_purchasing_receiving_extension.py`
- `tests/test_schema_migration_postgres.py`
- `tests/test_square_cost_workflow_boundaries.py`
- `tests/test_v2_digital_signage.py`
- `tests/test_v2_ordering_data_coordinator.py`
- `tests/test_v2_ordering_recommendation_service.py`
- `tests/test_v2_replenishment_report.py`

Added:

- `app/routers/purchasing.py`
- `app/services/purchasing_session_service.py`
- `app/templates/purchasing/home.html`
- `app/templates/purchasing/layout.html`
- `app/templates/purchasing/product.html`
- `app/templates/purchasing/session.html`
- `docs/v2/ordering/purchasing-v3-phase2-local-implementation.md`
- `migrations/versions/20261017_0034_purchasing_sessions.py`
- `scripts/evaluate_purchasing_forecast.py`
- `tests/test_purchasing_forecast.py`
- `tests/test_purchasing_phase2.py`

Generated local evidence is under `output/purchasing-phase2/`: four rendered route HTML files, an actual generated PO PDF, forecast evaluation JSON, change/preservation manifest and test logs. The output directory contains synthetic fixtures only.

## 2 Schema structures

Migration `20261017_0034_purchasing_sessions.py` follows `0033`. It adds four tables without altering or backfilling existing records:

- `purchasing_sessions`: owner, stable generation request identity, settings, status, source revision and decision version.
- `purchasing_session_revisions`: immutable evidence and policy snapshots, unique session/revision and request identity.
- `purchasing_session_lines`: accepted recommendation, proposed refresh, manual decisions, selected supplier, cost and exact allocations; unique session/variation.
- `purchasing_session_groups`: supplier status, intended account metadata and unique linked PO; unique session/supplier and PO link.

Foreign keys preserve lineage. Checks constrain statuses, quantities and costs. Indexes cover session/owner/status, session/supplier and group/status. Database triggers reject revision changes and converted lineage changes. Downgrade refuses to discard session history. Historical null costs remain unknown. No financial posting occurs during migration.

## 3 Unified replenishment architecture

`purchase_order_math_service.forecast_demand` is the sole quantity calculation. It lives in the existing math module. Standard PO generation, Ordering Intelligence, Inventory Velocity and V2 Replenishment delegate to it. Stock Coverage projects the per-store results from Velocity. Product pack rounding and exact allocation use shared helpers in the same module.

A parameterized parity test supplies identical stable, growing, declining and spike histories to all four calculation families and both legacy replenishment modes, asserting identical quantities. Descriptive report averages, stockout estimates and transfer diagnostics remain reporting metrics; they do not determine purchasing quantities.

`purchasing_session_service` owns evidence revisions, owner decisions and draft conversion. It reuses the Phase 1 inventory, incoming, identity, audit, permissions, PO editor and PDF infrastructure. It creates neither another replenishment authority nor a financial balance authority. Configured and historical costs are loaded in batches for catalog generation and session rendering.

Legacy replenishment entry modes now both use the unified calculation, including live inventory and incoming supply. Their labels explain the change; product multiples remain supported, vendor-wide MOQs no longer inflate recommendations. Existing reports retain user-selected coverage inputs; the new purchasing home defaults to reorder 5 weeks and target 10 weeks. Legacy replenishment now also defaults to target 10 weeks.

## 4 Forecasting methodology

Each store is forecast independently from completed business-day sales. Current inventory is requested directly from Square for all verified Andresen, Hwy 99, SR503 and Longview locations. Missing variation/location observations remain unknown; no cached inventory fills the gap. Incoming is the Phase 1 qualified outstanding commitment, excluding received quantities and historical/closed orders. The position is exact live Square inventory plus qualified incoming.

The deterministic engine uses up to 56 recent days from the retained 28–180 day source window. Sustained growth and decline require agreement across two recent weeks against prior history. Limited-history products receive conservative shrinkage. Isolated spikes are capped only with sufficient positive history. Confirmed unavailable zero-sales days can be excluded, with uplift bounded to twice observed velocity. All-censored or insufficient demand stays unknown. Recent zero observed demand suspends unnecessary replenishment.

Historical availability is qualified only with complete adjustment history and current-day deltas; physical-count ambiguity or malformed history removes that evidence. This backcast never establishes current inventory. Supplier lead time uses at least three separate POs and each PO's first positive, un-reversed recorded delivery. Last receipt is retained as reference evidence, not asserted to be the age of current stock. No supported lot-age evidence exists in the implemented sources, so inventory-age penalties are not invented.

Known store needs aggregate under the selected purchasing supplier. Configured product multiples round automatic quantities; explicit owner quantities remain authoritative. Largest-remainder allocations preserve the exact order total. One-unit standalone orders receive a restrained suggestion, not a universal prohibition.

## 5 Forecast evaluation results

`python -m scripts.evaluate_purchasing_forecast --output <file>` performs past-only rolling-origin evaluation against 28-day and 56-day averages. Owner-authorized historical exports can be supplied with `--input`.

The available run uses seven synthetic historical series, 180 days each: 68 matched 14-day holdouts total, with two censored future holdouts excluded. It is a reproducible behavioral benchmark, not evidence of Erupted production accuracy.

| Synthetic series | Unified MAE units | 28-day average | 56-day average |
|---|---:|---:|---:|
| stable | 0.000 | 0.000 | 0.000 |
| sustained_growth | 7.207 | 6.300 | 10.100 |
| sustained_decline | 6.888 | 6.300 | 10.300 |
| isolated_spike | 10.400 | 19.600 | 19.600 |
| slowdown | 4.425 | 4.200 | 7.000 |
| intermittent | 0.000 | 0.000 | 0.000 |
| stockout_history | 0.000 | 2.250 | 2.875 |

The unified method improves these spike and stockout fixtures; the 28-day baseline has lower error on the growth, decline and slowdown fixtures. No overall accuracy superiority is claimed. The JSON includes signed bias, WAPE, shortfall, excess and cost-weighted capital proxies for every method/series. Those proxies compare matched forecast horizons; they are not observed stockout incidents, net inventory investment or actual purchasing spend. A test proves holdout sales are excluded from fitting.

Real historical replay remains UNVERIFIED because no owner-authorized historical export was used. This is a material limit on forecasting acceptance.

## 6 Purchasing sessions

Generate requests fresh live evidence and creates a session, never a PO. Continue reads the persisted decisions without a new Square call. Refresh appends an immutable revision, preserves quantity, supplier, removals and allocations, and proposes new recommendations for individual acceptance. Decision versions reject stale owner edits. Session and group locks, uniqueness and stable PO creation keys prevent duplicate drafts under concurrent requests.

Full active-catalog name/SKU search includes products with zero current need and products without a SKU. Add is session-only. Remove supports undo before conversion. Discarded groups retain their evidence and are not silently reopened by refresh. Converted groups are frozen and linked to the existing editor; refresh never changes finalized PO allocations. Sessions complete after all actual groups are converted/discarded. An empty proposal stays active so the owner can add catalog products.

One-time supplier choices preserve the Square canonical identity and demand calculation. Local configured costs remain authoritative; missing cost is unknown and historical cost is provisional pending explicit owner confirmation. Neither substitution nor confirmed session cost updates vendor preference, COGS, Square or global cost configuration.

## 7 Interface evidence

`test_owner_http_acceptance_flow_and_existing_pdf` performs the requested owner workflow through real application routes and templates with deterministic Square mocks:

1. Generate with visible 5/10 defaults and collapsed advanced settings.
2. Verify collapsed supplier groups and expand-ready product forms.
3. Save a quantity and alternative supplier.
4. Search the complete catalog by SKU and add a product.
5. Remove and undo.
6. Refresh evidence and assert the saved quantity remains unchanged.
7. Save an intended payment account and create a linked PO.
8. Render the existing editor, verify allocations sum to the chosen total, download its actual PDF and return to the session.
9. Check CSRF and non-owner denial.

Captured HTML: `01-generate.html`, `02-collapsed-groups.html`, `03-review-refresh.html`, `04-existing-po-editor.html`. The initial groups are native collapsed details; product explanations and allocations are optional expansions. CSS includes narrow-screen layout rules.

This is equivalent route/render evidence, not a browser-click recording. Expansion is verified structurally through the native details markup, rather than a browser click. A browser policy blocked opening local HTML, so screenshot and device-level visual QA remain unverified. The generated PDF is retained as `05-created-po.pdf`.

## 8 PO and PDF integration

One supplier group creates or reopens exactly one draft in the existing editor. Supplier, product snapshots, costs, quantities and per-store allocations are prepopulated. Intended account is group metadata linked to the PO, displayed in the editor and editable through the session while the PO remains a draft. It records no charge. Submission freezes that intended account metadata.

The existing PDF generator and download endpoint are exercised with the created draft. Linked draft deletion now returns an explicit compatibility error instead of violating the lineage foreign key; preserve the linked draft or use the existing cancellation workflow. Existing non-session draft deletion is unchanged. No receiving, payment or actual-balance writer is invoked by preparation or conversion.

## 9 Test results

| Check | Result |
|---|---|
| Final general regression on final source | **967 passed, 444 skipped; 7 subtests passed** |
| Latest session, migration, concurrency and HTTP/PDF suite | **26 passed** |
| Engine, adapter levels and legacy replenishment checks | **42 passed** |
| Affected recommendation and Phase 1 corrective regression run | **103 passed** |
| Cost write boundary including session-only assignment guard | **6 passed** |
| Full PostgreSQL-enabled regression before final focused corrections | **1,393 passed, 3 failed, 1 skipped; 7 subtests passed** |
| Fresh schema/stamp/runtime mutation rehearsal | **1 passed** |
| Pre-Phase-2 scheduling failure reproduction on schema 0033 | **1 failed**, same assertion as broad run |

Counts overlap and must not be added as unique tests. The broad run's cost-writer allow-list failure was corrected with a narrow guard for owner session-line cost assignment, verified by six tests. Its obsolete sales-replacement expectation was corrected and the ten replenishment tests pass. The remaining scheduling failure is unchanged and reproduced on the pre-Phase-2 schema; no scheduling logic was modified.

All **444** final general-run skips are classified: **443** require `TEST_POSTGRES_ADMIN_URL`; **one** requires private real-R2 integration opt-in. The PostgreSQL-enabled broad run exercised the 443 integration cases instead of skipping them; only R2 remained skipped. No purchasing functionality test was skipped for incomplete implementation or converted from failure into a skip. The entire PostgreSQL regression suite remains **not green** because of the documented scheduling failure.

Reproduction uses the project's virtualenv with explicit local-only `DATABASE_URL` and `P1_TEST_DATABASE_URL`, and sets `TEST_POSTGRES_ADMIN_URL=postgresql+psycopg:///postgres` for the broad integration run. Full logs are retained alongside the HTML/PDF evidence. `compileall` and `git diff --check` pass.

All external Square operations in the acceptance tests are mocks. Test databases are uniquely created local PostgreSQL databases or isolated SQLite fixtures. No production data was used for destructive testing. Skips are not passes.

## 10 Historical preservation evidence

A disposable migration rehearsal starts at `0033`, seeds a modern receiving event and hashes every existing table's full row contents, including financial and receiving records. Upgrading to `0034` leaves all those hashes identical. The four new tables begin empty. ORM/schema comparison passes for all four new structures. Immutable revisions/conversion triggers and history-preserving downgrade refusal are tested.

The Phase 2 source manifest confirms unchanged SHA-256 hashes for `purchase_order_admin_service.py`, `card_balance_service.py`, migrations `0032` and `0033`, and all eight baseline blind-count-related source, migration, test and documentation files. The receiving test's head-revision assertion follows `0034`; receiving behavior is unchanged. Phase 1 recommendation tests were updated only where the approved shared trigger, 56-day coverage or quantity policy intentionally replaces their former expectations.

## 11 Remaining risks and operational prerequisites

- Real-demand accuracy, live Square latency at full catalog scale and owner-device usability remain unverified. Replay an authorized historical export, compare measured metrics and review the captured workflow on owner devices before rollout.
- Current stock age cannot be established from the latest receipt alone. The source retains this evidence without inventing lot age; reliable lead-time samples may initially be sparse.
- Legacy recommendation quantities intentionally change with the unified engine, stockout evidence rules and removal of vendor-wide MOQ. Owner review should include products that previously relied on those behaviors.
- Intended payment account is explicit metadata, separate from legacy financial assignment. Session-linked drafts preserve immutable lineage; deletion follows the controlled boundary described above.
- Source revisions retain full evidence, so database size and retention planning need review before operational use. Session generation is synchronous; full-catalog live-read performance needs measurement against actual workload.
- The broad PostgreSQL suite has an unrelated scheduling failure: `test_lead_repair_prefers_ordinary_shift_and_longview_credit_follows_final_assignee` expects `lead_repair_changed_assignment=False` but receives `True`. It reproduces with the fixture migrated only to `0033`, before this phase's schema, and all baseline scheduling source/test hashes are unchanged. No scheduling correction was made under this authorization.
- Framework deprecation warnings and an existing unrelated metadata sort-cycle warning remain; they did not invalidate the isolated checks.

Before any production action, obtain separate owner authorization, review this migration and rollback preservation boundary, back up the target database, confirm the owner identity and four location mappings, measure live-read performance, and complete real-history/visual acceptance. Feature flags remain off. No new financial writer should be enabled.

## 12 Local acceptance recommendation

| Requirement | Local result | Evidence |
|---|---|---|
| Single calculation across four families | PASS | `test_all_four_calculation_families_delegate_to_the_same_forecast` |
| Fresh four-store inventory and missing pairs | PASS with mocks | `test_real_gateway_requests_four_live_locations_and_never_fills_missing_count` |
| Persistent decisions and controlled refresh | PASS | Session refresh, stale-version and concurrent-edit tests |
| Exactly one draft and existing PDF | PASS | Concurrent conversion and full owner HTTP/PDF workflow tests |
| Financial and receiving boundaries | PASS within tested scope | Zero writer assertions, Phase 1 tests and source hashes |
| Additive migration and preservation | PASS | Full-table fingerprint, schema comparison and immutable-history tests |
| Actual forecast accuracy | UNVERIFIED | Synthetic benchmark only |
| Owner-device visual acceptance | UNVERIFIED | Rendered HTML evidence; browser opening blocked |
| Entire PostgreSQL regression suite | FAIL | Unchanged scheduling case reproduced on `0033` |


Provisionally accept the local functional implementation for owner review. The shared calculation, persistent decisions, controlled refresh, idempotent draft conversion, existing PDF integration and financial/receiving boundaries have isolated source and test evidence. Defer unconditional acceptance while real-history forecasting, device-level visual acceptance and the documented scheduling regression remain unresolved.

Stop here for explicit owner review. Do not deploy, migrate production, activate purchasing automation or start Phase 3.
