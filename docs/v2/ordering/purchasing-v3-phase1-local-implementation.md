# Purchasing V3 Phase 1 — local implementation and owner review

Date: October 17, 2026. Base: `83b2479`. Branch: `codex/purchasing-v3-phase1`.

Local development/testing only. No deployment, production migration, production configuration change,
Square mutation, automation activation, paid AI use, or Phase 2 implementation has occurred.

## Architectural result

- The central Square policy permits the current inventory-count read endpoint. All mutation restrictions remain.
- Both V2 inventory readers share explicit-pair parsing. Omitted/malformed counts remain UNKNOWN. Negative and zero observations remain distinct.
- Retrieval freshness and Square calculation time are retained separately. A freshly retrieved unchanged count is not made stale solely by an old calculation timestamp.
- Sales pagination, quantities, location/timestamp evidence and business-date handling are validated. Local business time is America/Los_Angeles. Failed source operations do not establish zero demand.
- Inventory refreshes and sales synchronization can retain durable scope/coverage evidence in the opt-in mode. Persisted recommendation consumers do not infer complete coverage from empty sale tables.
- Shared incoming resolution uses placed destination quantities minus received quantities, excludes cancellations, and detects unresolved receipt/Square effects. A legacy terminal order with remaining quantities is explicitly uncertain, not silently erased.
- Raw signed inventory remains source evidence; shared inventory-position normalization supplies usable quantities. No fifth replenishment engine was introduced. The four historical formula families remain, with common input contracts/adapters preparing their later consolidation.
- Canonical Square association evidence is separate from purchasing supplier configuration. BIG/Streamline identifiers must be verified and configured. Multiple/missing associations remain UNKNOWN. An explicit validated 9671 exception is supported by the authority model.
- PO financial initialization no longer creates `INVENTORY_PURCHASE` ledger rows. Existing rows are retained unchanged.
- Actual card balances use issuer checkpoints plus subsequent known actual activity. PO estimates, assignments and invoices are not inputs. Legacy ledger sums remain labeled historical compatibility evidence, not another actual balance.
- Actual authority remains sticky after a checkpoint: pausing the new writers does not restore the mixed legacy ledger as current truth.
- Existing ledger/payment records remain linked provenance. New actual activity is recorded once, with account locking, request identities and reversal linkage. Existing card forms carry stable submission identities in the opt-in mode.
- Adjust Balance requires only the corrected balance and Save. Audit actor/time/prior value/cutoff are automatic. It is protected by feature exposure, CSRF and the exact configured active owner principal.

## Additive schema

Migration: `20261017_0032_purchasing_authority`, parent `20260930_0031`.

Four new tables, with no legacy-row backfill or reinterpretation:

| Table | Justification |
|---|---|
| purchasing_evidence_runs | Durable requested scope and read completeness for inventory/sales/catalog evidence |
| purchasing_product_authority | Observed Square vendor relationships plus a rare explicit local cash-source exception |
| funding_balance_checkpoints | Append-only issuer correction and the actual-event sequence it covers |
| funding_actual_activity | Unambiguous known card events; references existing ledger/payments without treating their mixed historical contents as charges |

There is one actual balance calculation, in `card_balance_service.py`. The actual-activity table is a typed source of evidence, not a second balance total. It avoids mutating/relabeling old assignment-generated entries.

The planned evidence and financial migrations were consolidated into one additive revision to reduce intermediate schema states. Existing receipt, report, PO and payment schemas remain unchanged. Downgrade refuses to discard any populated new evidence table.

## Local controls and compatibility

All new authority persistence/writers are disabled by default through `purchasing_v3_phase1_enabled=False`.
Activation additionally requires the verified owner principal and canonical BIG/Streamline Square IDs.
Existing principal feature gates are retained; no exposures were granted.

Before adoption, legacy reports remain available with their original calculation semantics. After actual-balance adoption, active card readers use checkpoints; disabling new writers preserves those reads and pauses incompatible financial mutations. Historical reconstructions remain labeled as such.

The existing PO editor, PDFs, receiving functions and Square quantity writers were not replaced. Their regression tests remain in scope. New shared incoming queries treat unresolved legacy receipts conservatively. Some legacy aggregate records cannot prove which remote count includes a receipt; those cases need review rather than fabricated stock.

The opt-in actual writer accepts known current card activity. Historical/opening activity routes direct the owner to issuer reconciliation through Adjust Balance. A historical event covered by a checkpoint is never automatically replayed. Reversing covered history similarly requires a new issuer correction. These are explicit cutover boundaries, not deletion or reclassification of old evidence.

The existing per-vendor reports and FIFO inventory views are retained as legacy functionality. They are not used to calculate new actual balances. The new pooled weekly gross-sales report, four-account Monday screen, sessions and receiving modernization are later phases and were not implemented.

Full catalog search, effective-dated configured costs, PO/PDF and current receiving behavior remain intact. Ambiguous SKU-only catalog fallback no longer selects an arbitrary variation; exact-variation search remains available.

## Preservation and migration evidence

The migration test creates a uniquely named local disposable PostgreSQL database at the old head, seeds synthetic evidence, fingerprints all seeded rows in 16 existing tables, upgrades, and compares the fingerprints byte-for-byte.

Covered tables: principals, vendors, stores, payment_methods, funding_accounts, funding_ledger_entries,
purchase_orders, purchase_order_lines, purchase_order_store_allocations, square_sync_events, audit_log,
funding_reports, funding_payments, funding_payment_allocations, purchase_order_receipts,
purchase_order_receipt_lines.

The rehearsal also checks empty-schema downgrade/re-upgrade, populated-evidence downgrade refusal, and new-table ORM/migration agreement. This is synthetic-data preservation evidence, not proof about production records. Existing unrelated ORM/schema differences are outside this change; the comparison is intentionally restricted to the four new tables.

## Tests and acceptance

Completed verification:

| Check | Result |
|---|---|
| General regression suite against local fixtures | **849 passed, 444 skipped; 7 subtests passed** |
| Focused PostgreSQL authority, migration, canonical compatibility and schema suite | **38 passed** (overlaps the general suite) |
| Python compilation | Passed |
| All Jinja template syntax | Passed |
| `git diff --check` | Passed |
| Original canonical checkout | No source changes |

Skipped tests require additional dedicated integration settings/fixtures and are not claimed as passed. The focused PostgreSQL suite explicitly exercised real transactions, concurrency and migration behavior. Warnings include existing FastAPI/AnyIO deprecations and the existing cyclic count-table metadata warning during schema inspection.

Test suites cover:

- Explicit zero, negative, missing and malformed inventory; stale/omitted refresh pairs.
- Completed versus incomplete sales scope, repeated cursors, physical-count reconstruction uncertainty.
- Partial/cancelled/legacy-terminal incoming orders and pending Square synchronization.
- Canonical vendor ambiguity, verified vendor pool fallback and the 9671 exception.
- Assignment and existing invoice/payment flags creating no actual card charge.
- Actual charge/payment retries, correction replay protection and negative account credits.
- Two real concurrent PostgreSQL operations serialized by an account lock.
- HTTP owner enforcement, CSRF denial and duplicate balance submissions.
- Migration preservation and existing purchasing/PDF/receiving/authentication regressions.

Acceptance status:

- [x] Local endpoint policy and evidence contracts implemented.
- [x] Shared usable-inventory/incoming foundation implemented; no new replenishment formula.
- [x] Canonical association and classification foundation implemented.
- [x] Assignment-triggered card posting isolated.
- [x] Single actual-balance authority, owner corrections, retries and concurrency implemented.
- [x] Historical preservation and additive rollback behavior tested locally.
- [ ] Production schema/workflow verification: not performed.
- [ ] Real owner/account/vendor identifiers and issuer opening checkpoints: not established.
- [ ] Owner review and acceptance of local Phase 1 results.
- [ ] Separate authorization for deployment or Phase 2.

## Production prerequisites — no authorization implied

1. Verify deployed commit, migration revision and active financial workflows with bounded read-only access.
2. Verify four unique store/location links, canonical vendor associations, configured costs, inventory coverage and sales coverage.
3. Inventory assignment-generated purchases, payment/reversal history and unresolved receipt/Square commands.
4. Obtain an appropriate backup/preservation baseline and rehearse against an authorized representative non-production copy.
5. Confirm exact owner principal, BIG/Streamline IDs, card accounts and 9671 payment source.
6. Establish issuer-backed card checkpoints at an agreed cutover instant. Do not infer them from legacy sums.
7. Obtain explicit deployment/migration/financial-writer activation authorization.
8. Apply the additive migration before this application revision; keep activation off until verification is accepted.

No safe production connection was established in this task. Production facts were not inferred from local source or schema.

`scripts/purchasing_phase1_baseline.py` is available for authorized direct read-only inspection. It imports no application startup, calls no Square APIs, sets the transaction read-only, applies query timeouts, and emits hashes instead of raw financial records. Its 200-row-per-table fingerprints are bounded samples, not a full backup or complete preservation proof. It was exercised only against the local disposable database.

## Rollback limitations

An old binary expecting schema 0031 cannot simply run against 0032. Before new evidence exists, the empty additive migration can be downgraded. Once evidence/checkpoints exist, retain the schema and compatible readers; pause financial writers instead. Checkpoint adoption remains authoritative even if the activation switch is turned off. Do not restart legacy balance writers or drop new evidence to make an old binary start.

Recommended next action: owner review of this worktree and local acceptance evidence. No Phase 2 or production action should begin without separate authorization.


## Changed files

The worktree contains uncommitted local changes for owner review; no commit was pushed.

```text
app/config.py
app/models.py
app/routers/v2_funding_reports.py
app/schema_contract.py
app/services/card_balance_service.py
app/services/inventory_velocity_report_service.py
app/services/purchase_order_generation_service.py
app/services/purchasing_classification_service.py
app/services/purchasing_evidence_service.py
app/services/purchasing_inventory_service.py
app/services/square_ordering_data_service.py
app/services/square_request_policy.py
app/services/v2_consignment_facts_service.py
app/services/v2_funding_reports_service.py
app/services/v2_order_payments_service.py
app/services/v2_ordering_catalog_service.py
app/services/v2_ordering_data_coordinator.py
app/services/v2_ordering_inventory_refresh_service.py
app/services/v2_ordering_normalization_service.py
app/services/v2_ordering_recommendation_service.py
app/services/v2_ordering_square_gateway.py
app/services/v2_ordering_view_model_service.py
app/services/v2_replenishment_report_service.py
app/templates/v2/order_payments/funding_account_detail.html
app/templates/v2/order_payments/funding_accounts.html
app/templates/v2/order_payments/funding_combined_report_detail.html
docs/v2/ordering/purchasing-v3-phase1-local-implementation.md
migrations/versions/20261017_0032_purchasing_authority.py
scripts/purchasing_phase1_baseline.py
tests/test_canonical_migration.py
tests/test_inventory_velocity_report_service.py
tests/test_purchasing_phase1.py
tests/test_purchasing_phase1_migration.py
tests/test_schema_migration_postgres.py
tests/test_square_cost_workflow_boundaries.py
tests/test_v2_digital_signage.py
tests/test_v2_ordering_data_coordinator.py
tests/test_v2_ordering_square_gateway.py
```

## Remaining risks and review requirements

- Production revision, enabled routes, vendor/account identities, source completeness and issuer balances remain unverified.
- Current source evidence cannot reconstruct every legacy receipt or physical-count interval. These cases are explicitly uncertain; Phase 1 does not invent event history or modify inventory.
- First activation needs fresh scoped inventory/sales evidence and verified classification identifiers. Missing setup blocks authoritative results rather than defaulting to zero or the card pool.
- Legacy SKU-only paths reject conflicting variation identities instead of pooling unrelated incoming stock.
- The four historical replenishment formulas and old settlement/FIFO views remain compatibility code. Phase 1 provides their common evidence foundation; later consolidation is not represented as already completed.
- Paused writers preserve adopted actual-balance reads. A binary/schema downgrade after populated evidence is not a safe rollback.
- Known historical card activity cannot be automatically replayed across a manual correction. The owner reconciles the issuer baseline; future ingestion needs an explicit reviewed import boundary.
- This is local acceptance evidence, not owner acceptance or deployment approval.

## Corrective implementation and final local acceptance evidence — October 17, 2026

This section supersedes the earlier acceptance claims and test totals. The owner's
corrective authorization was limited to the five review findings and isolated
local testing. No production access, deployment, production migration, Square
mutation, automation, paid AI service, new service, new table, or Phase 2 work was
performed. Changes remain local and uncommitted for owner review.

### Corrections and acceptance

| Finding | Result | Correction and evidence |
|---|---|---|
| Mixed-state actual card reads | PASS | `actual_card_balance` now selects the latest checkpoint and its subsequent activity in one SQL statement. Both inputs share the statement's MVCC snapshot, without locking readers or trusting cached ORM checkpoint objects. Deterministic PostgreSQL tests pause readers after execution while checkpoint/charge/payment writers commit. Additional concurrent readers and writers, replayed checkpoints, existing event replay, and writer serialization pass. |
| Sales-period coverage confused with update intervals | PASS | The original UPDATED_AT synchronization remains separate. Opt-in synchronization also exhausts a CLOSED_AT query with matching CLOSED_AT sort and validates returned scope. Only fresh COMPLETE evidence with `coverage_contract=2`, `source_filter=CLOSED_AT`, and `source_complete=true` certifies a sales period. Legacy envelopes and untyped evidence remain unknown. Updated-only reads never establish sales-period coverage. Replenishment and activated financial/consignment report readiness/finalization consume this rule. |
| Receipts missing from cached on-hand counts | PASS, conservative | Existing receipt effects must reconcile exactly to recorded destination received quantities, all effects must be successful, and each needs a reliable completion timestamp. The Square count calculation must be at or after every relevant completion and at or before the observation boundary. Pending, failed, partial, ambiguous, or untimed evidence makes inventory uncertain. Cancellation/removal does not erase a prior physical receipt barrier. No receiving writer was changed or added. |
| Inconsistent purchasing definitions | PASS in the activated Phase 1 boundary | Standard generation, Ordering Intelligence, Inventory Velocity/stock coverage, and V2 Target Weeks use the shared supply and receipt evidence. Standard generation recognizes historical incoming independently of the next supplier, including non-default suppliers. Inventory Velocity deducts incoming once and preserves the same evidence in its stock-coverage wrapper. Signed source counts remain preserved; usable quantities normalize at destination before aggregation. |
| Missing verification | PASS locally | Actual database-backed PO PDFs, template/multipage behavior, account/list/single-report/combined-report rendering, previously skipped relevant PostgreSQL suites, migration preservation, and full regression were exercised. Remaining skips are classified below. |

Supporting implementation: `app/services/card_balance_service.py`,
`purchasing_evidence_service.py`, `purchasing_inventory_service.py`, and their
existing purchasing/reporting consumers. The corrective test module is
`tests/test_purchasing_phase1_corrections.py` (38 collected cases).

The timestamp filter/sort pairing follows [Square's SearchOrders contract](https://developer.squareup.com/reference/square/objects/SearchOrdersSort).

The four existing replenishment formula families remain; there is no fifth
engine or forecasting consolidation. Replace Sales remains an explicit exception:
it replaces selected-period sales without considering current or incoming stock.
Its selector and preview note now say that it ignores inventory. It is not an
inventory-position recommendation; Target Weeks requires trusted inventory.

### Exact corrective files

The corrective delta comprises these 17 source/test/document files. These are
changes since the reviewed Phase 1 implementation, not a replacement list for
all earlier Phase 1 changes.

```text
app/services/card_balance_service.py
app/services/inventory_velocity_report_service.py
app/services/purchase_order_generation_service.py
app/services/purchasing_evidence_service.py
app/services/purchasing_inventory_service.py
app/services/square_ordering_data_service.py
app/services/v2_consignment_facts_service.py
app/services/v2_funding_reports_service.py
app/services/v2_ordering_data_coordinator.py
app/services/v2_replenishment_report_service.py
app/templates/v2/reporting/workbench.html
tests/test_purchasing_phase1.py
tests/test_purchasing_phase1_corrections.py
tests/test_square_vendor_reassignment.py
tests/test_v2_ordering_data_coordinator.py
tests/test_v2_replenishment_report.py
docs/v2/ordering/purchasing-v3-phase1-local-implementation.md
```

Two additional synthetic verification artifacts are supplied for owner inspection:

```text
output/pdf/purchasing-phase1-corrective-default.pdf
output/pdf/purchasing-phase1-corrective-template.pdf
```

No corrective migration was added. `20261017_0032` and the ORM schema remain
unchanged by these corrections. Existing purchasing/PDF/receiving/authentication
infrastructure was retained.

### Completed verification

| Selection/check | Final result | Recorded evidence |
|---|---|---|
| Full general regression, local fixture configuration | **887 passed, 444 skipped; 7 subtests passed** | `/tmp/erupted_p1_corrective_full_regression.log` |
| Focused real PostgreSQL Phase 1 authority, corrections, preservation, canonical migration and schema suites | **76 passed, no skips** | `/tmp/erupted_p1_corrective_authority_postgres.log` |
| Previously skipped relevant PostgreSQL purchasing/finance/inventory/staff authorization suites | **105 passed, no skips** | `/tmp/erupted_p1_corrective_relevant_postgres.log` |
| Python compilation | Passed | `python -m compileall -q app tests/test_purchasing_phase1_corrections.py` |
| Jinja syntax | **143 templates parsed** | Jinja environment parsed every `app/templates/**/*.html` file |
| Whitespace/diff validation | Passed | `git diff --check` |

The selections overlap the general run and must not be added together. The
focused selection uses `test_purchasing_phase1.py`,
`test_purchasing_phase1_corrections.py`, `test_purchasing_phase1_migration.py`,
`test_canonical_migration.py`, and `test_schema_migration_postgres.py`.

All database testing used the explicitly disposable local
`postgresql+psycopg:///erupted_purchasing_p1_test` or uniquely named databases
created/dropped by the existing integration fixtures. Migration-creating suites
used `TEST_POSTGRES_ADMIN_URL=postgresql+psycopg:///postgres`. No production URL
was used. External integrations were mocked; no live Square mutations were made.
Warnings are the existing FastAPI/AnyIO deprecations and the pre-existing cyclic
count-table metadata warning during the scoped migration comparison.

The 105-case selection breaks down as follows:

| PostgreSQL suite | Passed |
|---|---:|
| Square vendor reassignment | 9 |
| Ordering lifecycle | 2 |
| Order-payment cleanup | 1 |
| Replenishment migration | 1 |
| Consignment-report migration | 1 |
| Blind-count reliability, including inventory and HTTP scope/CSRF | 11 |
| Employee access | 30 |
| Employee profiles | 8 |
| Primary staff/access workflows | 42 |
| Total | 105 |

### Skips and exclusions

The final general run includes explicit `-ra` skip reasons. All 444 are explained:
443 require `TEST_POSTGRES_ADMIN_URL`; one requires private R2 integration opt-in.
Of those original skips, 105 passed in the relevant PostgreSQL selection and
another 11 passed in the canonical/schema selection. Thus 116 have subsequent
passing evidence; **328 remain unverified**, not passed.

| Still unverified | Count | Reason / Phase 1 relevance |
|---|---:|---|
| Scheduling foundation 157, exceptions 41, repair 26, snapshots 11, lead duty 34, Longview 11, coverage migration 2, lead-duty migration 2 | 284 | Separate legacy scheduling PostgreSQL coverage; no changes to those workflows |
| Daily store logs | 24 | Separate legacy PostgreSQL coverage |
| Exchange returns | 14 | Separate legacy PostgreSQL coverage |
| Digital signage PostgreSQL cases | 5 | Separate legacy PostgreSQL coverage |
| Private R2 delivery | 1 | Explicit external-integration opt-out; no private service exercise |
| Total | 328 | 327 PostgreSQL environment-gated + 1 external opt-out |

No skip reason indicates unfinished Phase 1 functionality or a failure hidden by
a skip. No requested purchasing/finance PostgreSQL suite remains blocked. The
above unexecuted suites are still not evidence of success. No whole suite was
excluded from the completed general run.

### PO PDF and financial rendering evidence

The PDF tests seed synthetic PO/vendor/line/pack/template rows in the disposable
PostgreSQL fixture, then call the actual `_generate_purchase_order_pdf`. Only
the filesystem root is redirected into a test temporary directory; ReportLab
page drawing runs normally. Stream compression is disabled only in these tests
so PDF text operators can also be asserted directly.

Both PDFs were separately reopened with pypdf, rendered with Poppler, and visually
inspected. The default is one page. The template case is two pages with all 35
products, repeated header, and the disclaimer on the final page. Verified content
includes vendor and PO identity, saved product/variation, quantity 12 and
`12 (3 pk @ 5)`, the final product, template terms, and exclusion of the removed
line. No existing generated PDF or saved template was overwritten.

`test_financial_account_and_report_routes_render_with_actual_authority` renders
actual route/template responses for account list, account detail, finalized
single-vendor report, and finalized combined report. Before a checkpoint, the
account displays UNKNOWN. After activation/checkpoint, account views display
$777.50 and the actual-balance label; report COGS remains $23.00. A historical
$99,999 planning ledger row does not alter the actual balance. Rendering creates
no actual activity. Synthetic HTML captures are retained under
`/tmp/erupted-p1-corrective-evidence/html/`.

### Preservation and compatibility

The existing migration rehearsal passed again: upgrade from 0031 to 0032,
byte-for-byte fingerprints of seeded rows across 16 existing tables, empty
schema downgrade/re-upgrade, refusal to discard populated new evidence, and
new-table ORM/migration agreement. This verifies synthetic preservation, not
production preservation or whole-legacy-schema ORM agreement.

Checkpoint replay, historical-event replay rejection, charge/payment retries,
covered-reversal rejection, paused-writer sticky authority, owner enforcement,
and CSRF protection continue to pass. PO estimates, intended payment accounts,
and invoice fields remain outside actual balance calculation. Existing legacy
ledger/report/FIFO evidence remains available and is not another actual balance.

Activation now requires fresh typed sales-period proof; old updated-at envelopes
cannot authorize new inventory advice or financial report finalization. Legacy
mode remains behind the existing compatibility boundary. Receiving, PDF
production code, authentication, and Square inventory writers were not replaced.

### Remaining risks and owner action

- Unknown or untimed legacy receipt evidence remains blocked. A fresh retrieval
  alone may still be insufficient when Square's count calculation cannot be
  shown to include the acknowledged receipt. No historical timestamps are
  invented or rewritten to clear this condition.
- Coverage certifies the queried Square evidence as of the read, not transactions
  that have not yet reached Square. Existing consignment offline-lag checks
  remain; synchronization and freshness must continue operationally.
- Existing formula families retain their demand, rounding, and store-allocation
  differences. Their evidence definitions are shared; forecast consolidation is
  deferred to the separately authorized later phase.
- The 328 remaining unrelated/environment-dependent tests above are unverified.
- Production schema, actual active writers, identities, source completeness,
  issuer checkpoints, backups, and deployment/cutover remain unverified. The
  production prerequisites and populated-schema rollback restrictions above
  still apply.

**Recommendation: grant final local acceptance to the corrected Phase 1
foundation, pending explicit owner review.** No known unresolved defect from the five authorized findings remains
under the documented evidence contracts. This is not production approval or
Phase 2 authorization. Stop here for explicit owner acceptance.


## Phase 1 extension: inventory authority and receiving modernization — local completion

Owner authorization: “Phase 1 Extension — Inventory Authority and Receiving Modernization,” supplied October 17, 2026. This section supersedes the earlier receipt-timestamp barrier and cumulative receiving descriptions above. Scope remains local Phase 1 only.

Implementation is in `worktrees/purchasing-v3-phase1`, branch `codex/purchasing-v3-phase1`. Earlier uncommitted Phase 1 work was preserved. No deployment, production migration, production financial change, Square mutation, automation activation, paid AI activation, production configuration change, or Phase 2 work was performed.

### 1. Acceptance findings

| Requirement | Result | Evidence |
| --- | --- | --- |
| Live Square is current inventory authority; retain valid pairs, missing remains unknown | PASS locally | Existing Square read gateway; `fetch_on_hand_by_store_variation`; extension tests for four stores, partial pages, and missing observations |
| All four named stores are requested; ambiguous mappings remain unknown | PASS locally | `purchasing_store_locations`; four-store read and ambiguity tests. Display scopes remain restricted to requested stores in Ordering Intelligence. |
| Cached inventory never supplies V3 generation | PASS locally | Replenishment explicitly replaces cached observations with live reads. Regression seeds cached 18 and verifies live 23 is used. No fallback on failed reads. |
| Historical receipt timestamps cannot veto valid live stock | PASS locally | Prior barrier is retired. Former barrier tests now verify live-count independence for pending, failed, cancelled, mismatched, and legacy evidence. |
| Position = exact live quantity + outstanding active commitments | PASS locally | `inventory_position` and `load_incoming_supply`; standard math and Ordering Intelligence consume exact counts, including negative/fractional values; only final order quantities are rounded. |
| Receiving owner identity checked at route and service | PASS locally | `purchasing_receiving_owner`, `authorize_purchasing_owner`; non-owner ADMIN/MANAGER and absent actors denied; seven protected routes exercised |
| General PO Save cannot smuggle receiving | PASS locally | Receiving fields are rejected; old hidden-field copying removed; HTTP bypass test |
| Repeated partial delivery saves never send to Square | PASS locally | Immutable receipt destination events; repeat-delivery and stable-request identity tests |
| Explicit Push, durable payloads, partial success, uncertainty, restart recovery | PASS locally | Existing `SquareSyncEvent` plus PostgreSQL serialization; observer session proves payload/association committed before mocked HTTP; interruption and lost-reply tests |
| Successful commands are sticky; retries preserve identity, payload, timestamp | PASS locally | Database trigger and retry comparisons. Retry/Recover only processes existing commands; it does not first-post newly saved deliveries. |
| Corrections preserve original and explicitly post signed difference | PASS locally | Pushed 20 → corrected 19 produces a separate IN_STOCK → NONE adjustment of 1, never an absolute reset. Before-push corrections net only unprepared events. |
| Close Remainder retains optional reason and reduces incoming without Square | PASS locally | Closure event, owner/time metadata, concurrent correction/closure and bounded-outstanding tests |
| Historical data and financial authority preserved | PASS locally | Sixteen historical-table fingerprints before/after migration; legacy aggregate no-replay tests; existing financial suite and explicit consignment lifecycle integration |
| Existing PO editing, manual product addition, PDF generation, permissions | PASS within tested scope | Existing unit/integration suites, both real saved-PO PDF regression variants, owner/non-owner page rendering, and 82 PostgreSQL access/lifecycle tests |
| Blind-count compatibility, with no blind-count edits | PASS locally | Existing reliability/controller/recount tests; all 11 identified blind/count-related source/test files match the pre-extension hashes |
| Production identities, four-store Square coverage, real API acknowledgements | UNVERIFIED | Deliberately not inspected or mutated during this local implementation |
| Remaining environment-gated suites | UNVERIFIED | Classification below; no skip is represented as a pass |

### 2. Architecture and schema

No new service module, replenishment engine, inventory ledger, card-balance authority, financing attribution scheme, or accounting workflow was introduced. The four existing replenishment formula families remain; they consume the same stock-position and incoming definitions. Their demand policies and pack rounding remain their existing responsibilities.

- `purchasing_inventory_service`: named-store location resolution, exact stock position, and qualified outstanding incoming. Only `IN_TRANSIT` and `RECEIVED_SPLIT_PENDING` are active incoming statuses. Remaining quantity is `max(allocated − received − explicitly closed, 0)`. Removed lines, cancelled/completed/terminal legacy orders do not supply incoming. A null legacy received field means no entered receipt, not a Square inventory count.
- `square_ordering_data_service` and the existing `SquareOrderingReadGateway`: current inventory reads share the existing parser/transport/policy. Count omission or malformed quantities never create zero. Partial read failures preserve already observed pairs; missing pairs remain absent/unknown. Coverage manifests distinguish requested, observed, missing, unresolved SKU, and mapping cases. Standard PO generation records its coverage using the existing `PurchasingEvidenceRun` structure when it generates an order.
- `purchase_order_admin_service`: the existing receiving boundary now owns delivery, correction, closure, audit identity, operation preparation, and explicit dispatch. The old aggregate-overwrite writer and old aggregate-to-Square posting implementation were removed. The existing barcode matching, store priority, pack increment, and transport infrastructure are reused.
- `card_balance_service` and financial classification authority remain unchanged by this extension. Explicit receiving retains the existing idempotent `initialize_new_order_if_configured` lifecycle hook. A configured consignment fixture proves that repeated Push initializes once and produces no actual card activity or legacy purchase-charge entry.

Migration `20261017_0033_receiving_events.py`, parent `20261017_0032`, adds:

1. Nullable `event_kind`, `request_key`, and `request_payload` on existing receipt headers. A unique request key prevents duplicate submissions. Existing rows remain null and are not backfilled into invented deliveries.
2. `purchase_order_receipt_allocations`, a destination child of existing receipt lines. It retains PO line, Square variation, store, delivery/correction/closure kind, signed delta, closed quantity, corrected quantity, original delivery reference, owner/time, and optional existing Square operation association. Foreign keys, kind/quantity checks, per-receipt/store uniqueness, and line/store, operation, and original-reference indexes are included. Closure reasons remain on the existing receipt header notes field.
3. Database guards prevent modifying/deleting modern receipt headers, lines, and destination evidence. Only a one-time operation association and one-way no-posting marker are permitted on destination evidence. For `PURCHASING_RECEIPT_V3` operations, payload, idempotency key, and target identity are immutable; successful status cannot be downgraded. Other Square workflows retain their previous behavior.
4. Downgrade refuses to discard modern receiving evidence. An empty extension schema can be removed; once populated, retain the additive schema and plan any application rollback explicitly.

The ORM also declares the already-existing receipt order/date index; the migration does not recreate or remove that historical index. The schema contract head is now `20261017_0033`.

### 3. Posting and receiving interface

Received Today begins blank. Previously Received is durable receiving evidence and is not replaced by the browser's unsaved-input totals. Store cells show ordered/received/outstanding alongside Received Today. Save Receiving records deltas and does not send Square requests. Push to Square prepares only saved delivery/correction effects. The history shows saved/not pushed, successful, failed, and uncertain outcomes, with event, actor, time, original/corrected values, and destination identity. Correction and Close Remainder have separate CSRF-protected owner actions.

Before sending, the service commits the exact Square body and UUID idempotency key into an existing sync event and commits its receipt associations. It commits the attempt marker before transmission. A confirmed rejection is FAILED; network/server/interruption ambiguity remains PENDING with `OUTCOME_UNCERTAIN`, displayed distinctly. A subsequent explicit Push/Retry reuses the body, quantity, timestamp, and key. Successful operations are not sent again. A failed/uncertain predecessor blocks later commands for that line/store during that push. Retry/Recover processes existing commands; new deliveries await Push to Square.

A PO-scoped PostgreSQL session advisory lock prevents overlapping pushes across processes while allowing the prepared command to commit before HTTP. PO row locks serialize receiving, correction, closure, and ordinary PO saves. Receiving refreshes existing ORM objects after acquiring the lock, so a previously read Session cannot lose a concurrent receipt increment. No blind-count lock, permission, table, or writer was changed.

Negative correction transitions use the inventory adjustment API, not a physical recount. Square's current [inventory process-flow documentation](https://developer.squareup.com/docs/inventory-api/how-it-works) describes IN_STOCK → NONE as a stock decrease. Actual configured API-version behavior remains an explicit future verification prerequisite; no Square writes were used to prove it locally.

The owner receiving page was rendered through the actual route and inspected in a local browser with synthetic data. That check caught and corrected an inherited JavaScript total overwrite. Missing on-hand values now render UNKNOWN, and negative/fractional observations remain exact. Inventory Velocity/Stock Coverage label partial observations and produce store recommendations only for known pairs. Empty coverage never manufactures an all-zero product.

### 4. Historical preservation and controlled compatibility boundaries

The migration rehearsal seeds historical principals, vendors, stores, payment methods/accounts, financial ledger/report/payment/allocation evidence, PO headers/lines/store allocations, receipt headers/lines, Square success/key evidence, and audit records. SHA-256 fingerprints of all sixteen pre-existing table projections match through `0031 → 0032 → 0033`, empty-schema downgrade/re-upgrade, and refused evidence-losing downgrade. The three new nullable receipt-header columns are excluded from the historical projection; existing column values are unchanged. New receiving rows remain empty after upgrade.

Legacy aggregate receipts are not converted into deliveries and are never automatically queued. Their totals, old sync events, and old idempotency keys remain. Push can post a newly saved delta alongside a preserved legacy baseline, but cannot infer that the old baseline should be sent. The interface states that boundary, and push results report acknowledged and already-successful command counts rather than claiming to close the PO.

Successful posting does not move a partial PO to `SENT_TO_STORES`. It stays active with its actual remaining commitments. Fully received/closed active orders contribute zero incoming. Historical terminal statuses are excluded; any legacy terminal PO that still represents a real future commitment requires explicit owner reconciliation before cutover.

Ordinary PO saves retain existing receiving totals when a line is removed. Modern receiving evidence is additionally protected by restrictive references and immutable triggers. Discard is denied when any receipt, receiving total, or PO Square evidence exists, including legacy evidence; Close Remainder provides the operational alternative. Unreceived ordinary PO editing and product addition retain their existing permissions. Unresolved/ambiguous unexpected barcodes are rejected with an explicit resolution message; uniquely catalog-resolvable barcodes can be recorded without inventing a product identity or cost.

### 5. Test results and skipped-test classification

Final broad regression: **920 passed, 444 skipped, 7 subtests passed**. Separate PostgreSQL compatibility: **240 passed**. Separate employee/access/primary/lifecycle integration: **82 passed**. Final modern receiving suite: **33 passed**, including the additional configured-consignment lifecycle test. Counts overlap; do not add them as unique tests.

The broad regression includes actual saved-PO PDF generation with and without a saved template; financial page rendering; charge/payment/checkpoint/replay/concurrency tests; Square policy checks; PO creation/editing; live-read behavior; and the extension tests. All Square posting tests use mocks and all destructive schema/data tests use disposable local PostgreSQL databases.

All 444 full-run skips have explicit environment reasons: 443 require `TEST_POSTGRES_ADMIN_URL`, and one requires private real-R2 integration credentials/enablement. No test is skipped because this extension is incomplete or because a failure was converted to a skip. The separate PostgreSQL runs exercise **116** of those skips successfully, including all skipped purchasing, receiving/blind-count, finance cleanup, Square vendor mapping, schema, and ordering lifecycle cases.

Remaining **328 unverified** cases:

| Suite | Count | Reason |
| --- | ---: | --- |
| Scheduling foundation | 157 | PostgreSQL environment not enabled in the broad run; outside this extension |
| Scheduling exceptions | 41 | Same |
| Scheduling repair | 26 | Same |
| Scheduling snapshots | 11 | Same |
| Lead duty | 34 | Same |
| Longview weekly scheduling | 11 | Same |
| Lead-duty migration | 2 | Same |
| Coverage-commitment migration | 2 | Same |
| Daily store logs | 24 | Same |
| Exchange/return routes | 14 | Same |
| Digital signage | 5 | Same |
| Real private R2 integration | 1 | Explicit real-service opt-in/credentials unavailable |

There are no unexplained skips, intentionally disabled Phase 1 extension suites, or known failure-based skips. These 328 cases are unverified, not accepted by implication. Expected warnings concern existing FastAPI/TestClient deprecations and the existing count-session/forced-count metadata cycle; the existing blind-count migration and runtime tests pass.

Reproducible receiving tests: `tests/test_purchasing_receiving_extension.py`. Preservation test: `tests/test_purchasing_phase1_migration.py`. Superseded inventory-barrier expectations are updated in the original Phase 1 tests; financial assertions remain intact.

### 6. Exact extension files

This list is relative to the preserved pre-extension worktree hashes, not a claim that earlier Phase 1 files were newly created by this extension.

- `app/models.py`
- `app/routers/management.py`
- `app/routers/v2_ordering.py`
- `app/schema_contract.py`
- `app/services/inventory_velocity_report_service.py`
- `app/services/purchase_order_admin_service.py`
- `app/services/purchase_order_generation_service.py`
- `app/services/purchase_order_math_service.py`
- `app/services/purchasing_inventory_service.py`
- `app/services/square_ordering_data_service.py`
- `app/services/v2_ordering_data_coordinator.py`
- `app/services/v2_ordering_recommendation_service.py`
- `app/services/v2_ordering_square_gateway.py`
- `app/services/v2_ordering_view_model_service.py`
- `app/services/v2_replenishment_report_service.py`
- `app/templates/management_inventory_velocity.html`
- `app/templates/management_ordering_order_detail.html`
- `app/templates/management_ordering_tool.html`
- `app/templates/management_stock_coverage_purchase.html`
- `app/templates/v2/ordering/dashboard.html`
- `docs/v2/ordering/purchasing-v3-phase1-local-implementation.md`
- `migrations/versions/20261017_0033_receiving_events.py` — new
- `tests/test_canonical_migration.py`
- `tests/test_manual_ordering.py`
- `tests/test_purchasing_phase1.py`
- `tests/test_purchasing_phase1_corrections.py`
- `tests/test_purchasing_phase1_migration.py`
- `tests/test_purchasing_receiving_extension.py` — new
- `tests/test_schema_migration_postgres.py`
- `tests/test_square_cost_workflow_boundaries.py`
- `tests/test_square_vendor_reassignment.py`
- `tests/test_v2_digital_signage.py`
- `tests/test_v2_ordering_recommendation_service.py`

Review evidence is copied to `output/purchasing-phase1-extension/`: the full/compatibility/access/receiving JUnit results and final logs, the synthetic owner receiving HTML, and the file/skip/protected-file audit. These evidence artifacts are additional to the source list above. Earlier PDF artifacts remain preserved.

### 7. Remaining risks and deployment prerequisites

- Production schema, current active writers, owner ID, store/location identities, live variation coverage, legacy receiving ambiguities, and database backups remain unverified. This local report does not certify production data.
- Modern receiving requires `0033` and the configured active `purchasing_owner_principal_id`; it does not fall back to a permissive ADMIN/MANAGER writer when configuration is missing or the Phase 1 flag changes.
- Before any separately authorized deployment, verify all four approved store mappings and relevant active variation identities, review unresolved SKU/coverage manifests, and reconcile legacy terminal remainders and unposted aggregate receipts explicitly. Do not bulk replay or backfill invented deliveries.
- Verify that production PostgreSQL connections preserve session advisory locks; transaction-mode connection pooling must not be assumed compatible. Keep immutable prepared operations during any application rollback or restart.
- Real Square acknowledgement/idempotency behavior, negative adjustment support under the configured API version, and integration latency are not live-tested here. Permanent API rejection requires owner investigation; the implementation does not edit an immutable failed payload or silently issue a replacement command.
- Financial corrections, card checkpoints, and existing report evidence retain their prior Phase 1 rules. No new financing or weekly COGS behavior is authorized by this extension.
- The 328 remaining unrelated/environment-gated cases above remain unverified.

### 8. Owner acceptance checklist and next action

- [x] Local source and separate additive migration implemented within the extension.
- [x] Live Square authority, exact stock position, four-store reads, unknown/partial handling, and shared incoming verified with isolated fixtures.
- [x] Owner route/service enforcement, general Save bypass rejection, repeated deliveries, correction/closure, concurrency, and durable posting recovery verified.
- [x] Existing PO/PDF, tested financial lifecycle, access controls, historical preservation, and unchanged blind-count behavior verified locally.
- [x] Known skips and production limitations disclosed; no production or Square mutations performed.
- [ ] Owner review and final local acceptance.
- [ ] Separate explicit authorization for any production preparation, migration, configuration change, Square write verification, or deployment.

**Recommendation: accept the Phase 1 extension for local review/acceptance under the documented boundaries.** The next action is owner review of this report and the synthetic receiving evidence. No production acceptance, deployment, or Phase 2 authorization is inferred. Stop for owner review.
