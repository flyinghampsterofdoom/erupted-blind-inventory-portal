# Proposed phased implementation roadmap

Status: proposed; no phase is authorized or implemented. Each phase is independently deployable behind principal-scoped exposure and keeps V1 canonical until explicit cutover.

## Phase 0 — Policy and data validation

- **Scope:** resolve blocking decision-register items; profile production uniqueness/nulls/statuses/dormant receipt rows; capture Square read fixtures; define product/store identity and freshness contracts.
- **Out of scope:** UI, new operational writes, Square writes.
- **Database/routes/permissions:** none; approved design artifacts only.
- **Tests/dependencies:** repeatable read-only profiling; owner/accounting decisions; production-safe query access.
- **Canary/rollback:** not exposed; documentation rollback only.
- **Acceptance:** blocking rules signed, data anomalies quantified, identity contract approved.
- **Risk:** Low operational / High discovery importance.

## Phase 1 — Read-only ordering intelligence

- **Scope:** dashboard, normalized Square/catalog/inventory/sales reads, velocity, stockout projection, explanation and freshness warnings.
- **Out of scope:** controls, PO creation, Square writes, background auto-actions.
- **Database:** recommendation run/evidence snapshots only if approved; no V1-table mutation.
- **Routes/permissions:** GET dashboard/detail/run status; `ordering.recommendations.view`, store-scoped.
- **Tests/dependencies:** deterministic formulas, captured Square fixtures, stale/missing data, authorization, PostgreSQL migration tests if snapshots persist.
- **Canary/rollback:** one principal, then selected stores; disable exposure and retain evidence rows.
- **Acceptance:** results reproduce from inputs, explain every number, and cannot mutate ordering/Square state.
- **Risk:** Medium.

## Phase 2 — Merchandising controls

- **Scope:** do-not-reorder, dated exclusion, preferred vendor, manual lock/target, product status, decision reasons.
- **Out of scope:** drafts, vendor submission, Square writes.
- **Database:** V2 decision/event tables with scope/effective dates/version; do not overload V1 par zero.
- **Routes/permissions:** decision view/create/expire; `ordering.controls.edit` separate from view.
- **Tests/dependencies:** precedence/scope/date/concurrency/audit tests; owner-approved rules.
- **Canary/rollback:** principal-scoped; disabling UI leaves durable decisions but V1 remains unaffected.
- **Acceptance:** every decision is explainable, reversible by event, and never silently changes V1.
- **Risk:** Medium.

## Phase 3 — Draft purchase orders

- **Scope:** human selection from recommendations, vendor grouping, editable quantities/reasons, draft persistence and optimistic concurrency.
- **Out of scope:** approval snapshot, PDF, placement, receipt, payment, Square writes.
- **Database:** V2 draft/line/selection tables with separate IDs and V1 collision guard.
- **Routes/permissions:** draft CRUD; `ordering.po.create_draft` and scoped vendor-price view.
- **Tests/dependencies:** duplicate selection/grouping, rounding, conflict, audit, cross-store access.
- **Canary/rollback:** mark V2 drafts suspended/hidden; never translate automatically to V1 POs.
- **Acceptance:** no duplicate active draft for the protected business key; recommendation evidence remains intact after override.
- **Risk:** Medium-high.

## Phase 4 — Approved snapshots and PDFs

- **Scope:** approval, immutable commercial snapshot, versioned PDF, history, cancellation/replacement.
- **Out of scope:** vendor electronic submission, receiving, payments, Square writes.
- **Database:** approved snapshot/line/vendor/address/terms and document metadata/hash.
- **Routes/permissions:** approve/cancel/replace/download; `ordering.po.approve` separate from draft creator.
- **Tests/dependencies:** double approval, immutability, reproducibility/golden semantic checks, object storage policy.
- **Canary/rollback:** prevent new approvals; retain snapshots/documents read-only.
- **Acceptance:** same snapshot and renderer version reproduce equivalent document; no historical order mutates.
- **Risk:** High.

## Phase 5 — Receiving without Square writes

This moves ahead of payments because physical receipt facts and immutable PO lines are prerequisites for reliable invoice/payment and COGS reconciliation.

- **Scope:** receipt submissions, partials, damage/short/backorder/mis-ship/unknown dispositions, local store allocation and reconciliation.
- **Out of scope:** Square inventory updates, payment, auto-close.
- **Database:** receipt headers/lines/events with submission idempotency; reuse of V1 dormant receipt tables only after Phase 0 evidence.
- **Routes/permissions:** receive/reconcile/view; `ordering.receiving.record` and `.reconcile`.
- **Tests/dependencies:** duplicate submission, concurrent receipt, overage, unknown barcode, partials, audit.
- **Canary/rollback:** stop new V2 receipts; retain them and continue V1 as explicitly chosen per PO—never both.
- **Acceptance:** a PO has one receiving owner; physical facts are complete without changing Square.
- **Risk:** High.

## Phase 6 — Vendor payments and COGS linkage

- **Scope:** invoices, card/wire payment facts, funding-account reference, partial/combined allocations, reconciliation, versioned weekly vendor COGS view.
- **Out of scope:** bank integration, autonomous accounting entries, unresolved recognition policy.
- **Database:** invoice/payment/allocation/account-reference/cost evidence and accounting-period state.
- **Routes/permissions:** payment and COGS views/mutations with separate sensitive capabilities.
- **Tests/dependencies:** accounting-policy approval, allocations/reversals/period corrections, sensitive authorization.
- **Canary/rollback:** reporting first; suspend mutations while retaining immutable finance records.
- **Acceptance:** allocations balance, corrections are events, report states basis and is reproducible.
- **Risk:** High.

## Phase 7 — Controlled Square write gateway

- **Scope:** dry-run, explicit approval, durable commands, deterministic keys, narrow send, outcome-unknown reconciliation, monitoring.
- **Out of scope:** unattended writes, broad rollout, recommendation-triggered writes.
- **Database:** inventory commands/targets/attempts/reconciliation/audit.
- **Routes/permissions:** prepare/approve/send/reconcile; `ordering.square_write.approve` and `.reconcile` tightly separated.
- **Tests/dependencies:** Square sandbox/fixtures, timeout-after-success, partial batches, retry, read-only enforcement, production runbook.
- **Canary/rollback:** one principal/location/SKU class and low limits; disable sends, never delete command evidence, reconcile before fallback.
- **Acceptance:** duplicate commands cannot duplicate inventory; every unknown outcome blocks retry until reconciled.
- **Risk:** Very high.

## Phase 8 — Advanced intelligence

- **Scope:** learned lead-time evidence, seasonal weighting, vendor performance, accuracy, anomaly and low-confidence warnings.
- **Out of scope:** opaque/autonomous purchasing.
- **Database:** versioned derived metrics/models and evaluation evidence.
- **Routes/permissions:** enhanced explanations/configuration under existing separated capabilities.
- **Tests/dependencies:** backtesting, drift/fairness of rules, owner-approved seasonal ownership, sufficient history.
- **Canary/rollback:** shadow comparison first; revert algorithm version without changing prior evidence.
- **Acceptance:** measurable improvement over deterministic baseline, fully explained, human approval preserved.
- **Risk:** Medium-high.
