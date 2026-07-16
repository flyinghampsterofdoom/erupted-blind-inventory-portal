# Exchanges & Returns V2 cutover record

## Identity and status

- Module: Exchanges & Returns
- Product owner: unassigned
- Feature key: `exchanges_returns_v2`
- Current state: implemented locally; production cutover not approved
- V1 retirement: not proposed or approved

## Route ownership

- V1: `/store/exchange-return-form`, `/store/exchange-return-form/submit`, `/management/exchange-return-forms`, `/management/exchange-return-forms/{id}`
- V2: submission GET/POST, history, detail under `/v2/customer-forms/exchanges-returns`
- Redirects: none
- Coexistence: both versions read/write the same append-only table; a single browser interaction must use one form version only

## Data and authorization

- Tables: `exchange_return_forms`, `audit_log`, `principals`, `stores`, permission/session tables
- Historical facts: exchange/return rows and audit events; immutable in V2
- Read owner: both V1 and feature-exposed V2 during observation
- Write owner: route handling each individual submission; no cross-version retry/token sharing
- V2 actor: individual authenticated principal; V1 shared principals preserved historically
- Store scope: STORE locked to assigned store; management one/multiple/all authorized reads; no management write
- Permissions: unchanged `store.access` and `management.access`, including overrides
- Migration/backfill: none

## Validation evidence

- Full automated suite and PostgreSQL route/concurrency suite pass.
- V1 store form, management history, and V2 shell route regressions pass.
- Server timestamps, field parity, actor/store attribution, audit redaction, CSRF, duplicate serialization, filters, detail escaping, and role/scope isolation are covered.
- No Square/network/background job/export exists.

## Pre-cutover requirements

- [ ] Product owner assigned and presentation accepted.
- [ ] Individual employee tester accounts selected; no shared credential is exposed to V2 submission.
- [ ] Production schema read-only validation and approved baseline stamp/deployment plan complete.
- [ ] Desktop/mobile/keyboard review signed off.
- [ ] Production-like row volumes tested; pagination decision made.
- [ ] V1/V2 timestamp and date-filter results sampled around Pacific midnight/DST.
- [ ] Refund approval compatibility rule explicitly accepted or separately changed.
- [ ] Shared-principal labeling source and support runbook approved.
- [ ] Observation metrics, duration, and rollback owner named.

## Staged exposure

1. Local global feature for development only.
2. Principal-level exposure for named individual STORE and management testers.
3. Reconcile every tester-created row and audit actor; keep V1 navigation unchanged.
4. Consider environment default only after product/operations approval.
5. Any redirect or V1 retirement requires a later explicit decision.

## Rollback triggers

- Duplicate row or missing audit evidence.
- Wrong actor/store attribution or unauthorized record visibility.
- Material field/date discrepancy from accepted behavior.
- Form unusable on supported mobile/keyboard workflow.
- Unexpected V1 regression or persistence error rate.

## Rollback steps

1. Remove `exchanges_returns_v2` from global/principal exposure.
2. Confirm V2 links and routes return hidden/not-found behavior.
3. Direct operators to unchanged V1 routes; add no redirect.
4. Preserve V2-created immutable rows; reconcile by record ID, actor, store, server time, and audit correlation.
5. Investigate and document; do not delete/rewrite historical rows.

The module is locally reviewable but **not production-cutover ready** until every prerequisite is signed.
