# Exchanges & Returns V1 current state

## Sources

- `v1-route-inventory.md` §Chores, checklists, cash/change audits, forms, and employee logs
- `v1-data-map.md` §Store operations and forms
- `app/routers/store.py` exchange/return routes
- `app/routers/management.py` exchange/return routes
- `app/services/exchange_return_form_service.py`
- `app/templates/store_exchange_return_form.html` and management list/detail templates
- `sql/schema.sql` table `exchange_return_forms`

## Routes and permissions

| Route | Method | Behavior | Access |
|---|---|---|---|
| `/store/exchange-return-form` | GET | Renders a blank store form with server-generated portal timestamp | `store.access` fallback STORE |
| `/store/exchange-return-form/submit` | POST | Validates, inserts, audits, commits, redirects to blank form | `store.access`, CSRF, principal must have `store_id` |
| `/management/exchange-return-forms` | GET | Filters/list by optional store/from/to | `management.access` fallback ADMIN/MANAGER/LEAD |
| `/management/exchange-return-forms/{form_id}` | GET | Shows detail; writes a view-audit event and commits | `management.access` |

## Fields and validation

`employee_name`, original purchase date, original/exchange ticket numbers, items, reason, refund yes/no, and refund approver are required. The approver is required even when refund is No. Text is stripped but length/format/uniqueness are not constrained. Original date is parsed as ISO date. The hidden generated timestamp is accepted when parseable and otherwise replaced with current portal time.

The page instructs users not to complete the form for a resellable returned item. This is guidance, not server validation.

## Data and timestamps

`exchange_return_forms` stores ID, `store_id`, all entered fields, `generated_at`, `created_by_principal_id`, and database `created_at`. Store deletion cascades to forms. Principal deletion behavior is not cascading.

Date filters compare `generated_at` against UTC midnight boundaries, while the form/default display uses America/Los_Angeles. This timezone mismatch is a parity risk. Ordering is newest `generated_at` first.

## Actor and store attribution

The submission stores both free-text `employee_name` and authenticated `created_by_principal_id`; the audit event also records that principal. List/detail do not display the principal ID/account. Under the individual-account V2 decision, the authenticated principal is authoritative actor identity; the name remains a captured business field only if product owners retain it.

V1 may use a shared store principal, so historical actor attribution may identify the shared account rather than a person. No historical re-attribution is justified. Current `principal.store_id` supplies the store and is checked for presence; client input cannot choose it.

## Audit, editing, and deletion

- Submit logs `EXCHANGE_RETURN_FORM_SUBMITTED` with record ID in metadata, then commits with the form.
- Detail view logs `EXCHANGE_RETURN_FORM_VIEWED_AUDIT` and commits on GET.
- No active edit, delete, correction, export, or Square integration was found.
- No status column exists; a row is a submitted historical fact.

## List/detail and mobile behavior

The management list shows date/time, store, employee name, refund yes/no, approver, and detail link. It supports one store or all plus inclusive from/to dates. Invalid date filters return 400; nonnumeric store values become All; existence/authorization is not independently validated.

The store form is a single-column HTML form with native date/radio controls and full-width textareas, inherited V1 styling, no autosave/draft state, no confirmation page, and a redirect to a fresh blank form. It is usable at narrow widths but lacks an explicit durable success receipt and dedicated mobile regression coverage.

## Edge cases and data-quality concerns

- Manipulable client `generated_at`; separate `created_at` exists.
- UTC filter boundary versus portal-local entry/display.
- Refund approver required for “No.”
- No ticket format/uniqueness or duplicate-submit protection.
- Unbounded free text and no normalized employee identity display.
- Historical shared principal may not identify the person.
- Store cascade deletion conflicts with historical-fact retention policy.
- Detail omits `created_by_principal_id` and `created_at`.
- GET detail creates an audit write.
- No correction workflow or explicit retention decision.

## Existing characterization tests

`tests/test_exchange_return_characterization.py` covers active-store enforcement, authenticated principal/store attribution, trimming, and all service-required text fields. Route, timezone, CSRF, duplicate-submit, list/detail, and browser/mobile cases remain in the parity plan.
