# Exchanges & Returns V2 implementation

## Status

Implemented locally for Milestone 4. Feature key: `exchanges_returns_v2`. Disabled by default, not deployed, not redirected, and not cut over.

## Routes

| Route | Purpose | Exposure and authorization |
|---|---|---|
| `GET /v2/customer-forms/exchanges-returns` | Assigned-store submission form and durable receipt | Feature + existing `store.access`; assigned store required |
| `POST /v2/customer-forms/exchanges-returns` | Immutable local submission | Feature + `store.access` + CSRF + one resolved assigned store |
| `GET /v2/customer-forms/exchanges-returns/history` | Scoped history/search | Feature + existing `management.access` |
| `GET /v2/customer-forms/exchanges-returns/{record_id}` | Read-only detail and existing-style view audit | Feature + `management.access` + record store in authorized scope |

V1 `/store/exchange-return-form*` and `/management/exchange-return-forms*` routes are unchanged.

## Data and service boundary

The focused service is `app/services/v2_exchange_return_service.py`. It reuses the V1 `create_exchange_return_form` validation/insert function and the existing `exchange_return_forms` table. History/detail queries join stores and principals and never execute in templates.

No migration was needed. No Square, refund processing, inventory movement, notification, attachment, export, edit, delete, void, or correction behavior was added.

## Individual-account attribution

The authenticated principal ID is stored in `created_by_principal_id`; its username is stored in the required V1 `employee_name` field and shown read-only as Employee. Store comes from `principal.store_id`, never form input. The server supplies both the V2 submission timestamp and database creation time.

Historical free text and actor IDs remain unchanged. A “Legacy/shared account attribution” note appears only when the actor principal’s explicit custom role label contains both “legacy” and “shared”; V2 does not infer or retroactively assign a person.

## V1 behavior preserved

- Active assigned store required.
- Original purchase date, original/exchange ticket numbers, items, reason, refund yes/no, and refund approval name required.
- Refund approval name remains required even when refund is No because that is V1 validation behavior.
- Submitted row is immutable; no V2 status beyond Submitted.
- Management access includes current ADMIN/MANAGER/LEAD fallback and effective overrides.
- V1 management detail records view audit; V2 detail therefore writes a compatible V2 VIEWED event.
- Records remain local and do not imply a refund, Square action, or inventory movement.

## Intentional presentation differences

- Employee is derived/read-only rather than typed and is not audit identity.
- Assigned store is displayed and locked.
- Browser `generated_at` is not accepted; server time is authoritative.
- Successful submission provides record ID, server time, clear local-only language, and safe duplicate recognition.
- Validation retains safe entered values with inline errors and an accessible summary.
- History adds authorized multi-store scope, actor/ticket/detail search, refund/date filters, result count, desktop table, and mobile cards.
- Detail shows actor, preserved employee text, server timestamps, multiline escaped content, and linked audit evidence.

## Duplicate protection

GET issues a signed four-hour token containing version, principal ID, form intent, issue time, and random nonce. POST verifies signature, age, actor, and intent. Only a SHA-256 fingerprint is retained. A PostgreSQL transaction advisory lock serializes the fingerprint; existing V2 submission audit metadata returns the original record/correlation ID. Sequential retries and concurrent double-click tests prove one form row and one submission audit event.

Audit evidence is required for this narrow idempotency contract and is committed atomically with the form. Raw submission and CSRF tokens are never logged or stored.

## Errors and audit

Validation, authorization/scope, and unexpected persistence outcomes use the shared result vocabulary. Unexpected failures roll back, return a safe reference ID, and do not expose database details.
An unexpected persistence failure retains the original signed submission token so a safe retry can recognize an uncertain prior commit rather than minting a new idempotency identity.

Successful submission writes `V2:CUSTOMER_FORMS:SUBMITTED` with actor, entity/store, correlation ID, refund boolean, and safe submission fingerprint. Detail writes `V2:CUSTOMER_FORMS:VIEWED`, matching V1 view-audit intent. Request bodies, tickets, item/reason text, credentials, CSRF, session tokens, and raw form tokens are excluded.

## Exposure

Global local exposure:

```env
V2_ENABLED_FEATURES=exchanges_returns_v2
```

Individual tester exposure:

```env
V2_PRINCIPAL_FEATURES=123:exchanges_returns_v2,456:exchanges_returns_v2
```

Exposure is checked before module authorization and never grants `store.access`, `management.access`, or store scope. Navigation links appear only when both exposure and current permission flags allow the module.

## Test coverage

`tests/test_v2_exchange_returns.py` uses disposable PostgreSQL databases for authentication/exposure, every required field, CSRF, inactive/missing-store cases, actor/store/server time, safe audit, manipulated scope, sequential/concurrent duplicates, role/principal overrides, single/multi/all scope, unauthorized scope, date/search/refund/actor filters, sorting, leakage, detail/legacy/XSS/not-found, and V1/V2 shell regressions.

Existing V1 service characterization remains in `tests/test_exchange_return_characterization.py`.

## Future improvements not included

- Explicit database model for identifying shared principals rather than an admin label.
- Product decision on refund approver behavior.
- Store-specific timezone data instead of the current portal Pacific timezone.
- Pagination/exports at demonstrated data scale.
- Approved correction/retention workflow.
- Multi-store employee assignments.
