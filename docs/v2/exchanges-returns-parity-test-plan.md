# Exchanges & Returns parity test plan

## Goal

Prove a future V2 slice preserves intended V1 records and access while implementing approved individual-account, scope, error, and audit contracts. This plan does not authorize implementation.

## Fixtures

- Individual STORE employees assigned to two different stores.
- ADMIN, MANAGER, and LEAD with fallback and allow/deny overrides.
- One legacy shared STORE principal for compatibility cases.
- Active/inactive stores; records around Pacific/UTC midnight and DST boundaries.
- Refund Yes/No, whitespace, multiline content, long text, duplicate ticket/submission, missing/invalid dates.

## Store submission matrix

- GET requires authenticated `store.access`; assigned store is locked.
- Every required field and refund value matches accepted/rejected V1 behavior until a product decision changes it.
- Server records authenticated individual principal and assigned store; manipulated store/scope cannot expand access.
- `employee_name` treatment matches the approved V2 decision and never substitutes for actor ID.
- CSRF/session expiry behavior, double-submit/idempotency, validation error preservation, correlation ID, and durable success record are tested.
- Stored `generated_at`/`created_at` and store-local date meaning are compared around timezone boundaries.
- Insert and V2 audit event are atomic under the approved action contract.

## Management read matrix

- ADMIN/MANAGER/LEAD default, role override, principal override, and denied cases.
- Assigned/single/multiple/all read scope; unauthorized and partially unauthorized IDs return 403.
- From/to inclusivity and timezone basis; invalid filters return the approved validation result.
- Ordering, empty state, pagination if introduced, list fields, detail fields, raw multiline rendering, and missing record 404.
- Detail-view audit behavior is explicitly preserved or replaced only by an approved decision.
- Search/export are non-goals unless added to the milestone proposal.

## Data reconciliation

- Compare V1/V2 row counts by store/date.
- Compare every persisted field, null/empty distinction, actor principal, and timestamps.
- Confirm no update/delete path exists unless approved correction semantics are implemented.
- Confirm no Square/network call or shared balance mutation.
- Confirm legacy shared-principal records remain readable and labeled without guessed attribution.

## Browser/mobile

- Keyboard labels/errors/focus, screen-reader field names, radio group, touch targets.
- Narrow phone portrait, tablet, and desktop layouts.
- Long item/reason/approver values and error wrapping.
- Back/refresh/resubmit behavior and session expiration.

## Cutover and rollback proof

- Feature key disabled by default, enabled for individual tester principals only.
- V1 POST remains sole writer until V2 writer cutover; no dual submission path for the same interaction.
- Existing record IDs route to their owning/readable version.
- Disable exposure to roll back; V1 routes and schema remain intact.
- Reconcile test-window rows and audit actors before staged default.

## Required automated suites

- Service validation/attribution characterization.
- Route auth/capability/scope/CSRF tests.
- PostgreSQL persistence and timezone tests.
- Jinja rendering and browser accessibility/mobile checks.
- Audit redaction/envelope and feature-exposure tests.
- V1 route registration and regression suite.

## Exit evidence

Signed field/permission/scope matrix, golden V1/V2 record comparisons, zero unexplained row differences, actor attribution proof, mobile screenshots/test results, rollback rehearsal, and product-owner decisions for employee-name, refund approver, timestamp semantics, correction, and view auditing.
