# Milestone 4: V2 Exchanges & Returns

## Implementation status

Implemented locally behind `exchanges_returns_v2`. V1 routes remain active and canonical for production; no redirect, deployment, historical rewrite, or retirement occurred. See `exchanges-returns-v2-implementation.md` and `exchanges-returns-cutover.md`.

## Recommendation

Milestone 3 gates were accepted for local implementation. This remains the first business slice because it is a local append-only submission plus read workflow with no Square dependency, mutable balance, draft state machine, PDF, or background job.

## Proposed routes

- `GET /v2/customer-forms/exchanges-returns`
- `POST /v2/customer-forms/exchanges-returns`
- `GET /v2/customer-forms/exchanges-returns/history`
- `GET /v2/customer-forms/exchanges-returns/{record_id}`

These routes are implemented locally. V1 routes remain unchanged during limited exposure.

## Exact scope

- Store/mobile submission for an individually authenticated employee using locked assigned-store scope.
- Existing field/data parity unless an explicit decision below changes presentation.
- Authenticated actor attribution in record and V2 audit envelope.
- Authorized management single/multiple/all-store list and detail with date filters.
- Shared V2 status/error/empty/loading/form patterns as applicable.
- Principal-limited feature exposure, parity reconciliation, cutover and rollback evidence.

## Explicit non-goals

- No edit/delete/correction unless separately approved.
- No customer-request merge, refund processing, Square lookup/write, ticket validation against Square, attachments, notifications, exports, analytics, autosave/drafts, or historical data migration.
- No role/capability normalization or multi-store employee-assignment schema.
- No re-attribution of records created by shared V1 principals.

## Prerequisites and decisions

- Milestone 3 schema/auth/scope/status/error/audit/exposure contracts accepted.
- Decide whether free-text employee name remains, is prefilled/read-only, or is removed from new presentation while retaining stored parity.
- Decide whether refund approver is required when refund is No.
- Define store-local generated timestamp versus immutable server created time.
- Decide detail-view audit-on-GET behavior and correction/retention policy.
- Name tester accounts; shared credentials are not eligible for V2 operational exposure.

## Database and integration impact

Database impact is none: the implementation reuses `exchange_return_forms`, `audit_log`, principals, and stores. Duplicate protection uses a signed one-time form token, a stored safe fingerprint in V2 audit metadata, and a PostgreSQL transaction advisory lock. External integration impact is none.

## Definition of done

- V1 characterization and the parity plan pass.
- Each submission identifies the individual authenticated employee and locked assigned store.
- Role/override/read scope and CSRF/session cases pass with no privilege regression.
- Stored rows/fields/timestamps reconcile to approved semantics.
- Mobile/desktop accessibility and duplicate-submit recovery pass.
- V2 audit metadata is redacted and correlated; no secrets appear.
- Feature is disabled by default, limited tester exposure works, and rollback is rehearsed.
- No V1 route is removed or redirected until the observation period and explicit product-owner approval.

## Cutover and rollback

Start with individual principal exposure. Keep V1 navigation and routes. At staged write cutover, a user interaction reaches only one writer; existing records remain readable. Reconcile by ID/store/time and authenticated actor. Rollback disables exposure, returns users to V1, preserves V2-created append-only rows, and investigates rather than deletes discrepancies.

## Readiness assessment

The module is implemented and reviewable locally. Automated route, permission, scope, persistence, audit, duplicate/retry, and V1 regression coverage passes. Production cutover is **not ready** until individual tester accounts, visual acceptance, production-schema stamp/deployment planning, observation criteria, and explicit product-owner approval are complete.
