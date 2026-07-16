# Daily Store Log V2 implementation record

The [V1 Preservation Guarantee](./v1-preservation-guarantee.md) applies. This new V2 module does not transfer canonical ownership from any existing V1 tool.

Status: implemented locally for Milestone 5 behind `daily_store_logs_v2`. The key is disabled by default.

## Employee workflow

- Requires an authenticated account with effective `store.access`.
- Requires a validated Current Store on the authenticated `web_sessions` row.
- `GET|POST /v2/current-store` lists all active Erupted stores and accepts only a freshly validated active ID.
- Current Store is temporary operating context; it does not change `principal.store_id`, assignments, permissions, management scope, prior records, or future scheduling.
- Safe return targets are restricted to local `/v2/store-operations` paths.
- The form contains no employee-facing store selector or date picker.
- Submission revalidates Current Store and derives the Pacific store-day from the server submission timestamp.
- Previous-day entry is deferred to a future authorized exception workflow.
- Requires substantive section content or an explicit no-issues confirmation.
- Requires substantive follow-up detail when follow-up is requested.
- Stores an immutable submission attributed to the authenticated principal.
- Returns a durable receipt and allows only the submitting employee to open their record.
- Ordinary STORE users receive no history access and receive 404 for another employee’s detail.

## Management workflow

Effective `management.access` gates history, detail, and actions. The existing record-level management store-scope resolver remains authoritative; permission overrides do not expand the stores returned by that resolver. Filters include store, business date range, employee, lifecycle status, follow-up state, and content/management-note search.

Actions are append-only:

- `ACKNOWLEDGED`: `SUBMITTED` to `ACKNOWLEDGED`; note optional.
- `MARKED_FOLLOW_UP`: retains lifecycle status and sets follow-up; note required.
- `RESOLVED`: requires follow-up, changes lifecycle status to `RESOLVED`, clears follow-up; resolution note required.

Resolved records are not reopened in this milestone.

## Idempotency and atomicity

Signed single-use browser tokens are bound to the authenticated principal and intent. Only SHA-256 fingerprints are stored. Raw tokens are not persisted or audited.

- Same submission fingerprint returns the original record.
- A different fingerprint for an occupied `(store_id, log_date)` returns 409.
- Own conflicts include a safe own-record link; other-employee conflicts disclose no actor or record content.
- PostgreSQL transaction advisory locks serialize token and store/date contention.
- Submission/action rows and their audit events share one transaction.
- Persistence-error renders preserve the original token so an uncertain retry keeps the same identity.

## Routes

- `GET|POST /v2/current-store`
- `GET|POST /v2/store-operations/daily-logs`
- `GET /v2/store-operations/daily-logs/history`
- `GET /v2/store-operations/daily-logs/{record_id}`
- `POST /v2/store-operations/daily-logs/{record_id}/acknowledge`
- `POST /v2/store-operations/daily-logs/{record_id}/follow-up`
- `POST /v2/store-operations/daily-logs/{record_id}/resolve`

No V1 route redirects to these endpoints.

## Store Operations workspace

The V2 shell displays Current Store with a Change store action for employee submission pages. `/v2/store-operations` is now the employee daily completion dashboard; Daily Store Log remains available from its dashboard action and is not repeated as a child. The section seeds Daily Chore List, Inventory Counts, Non-Sellable Counts, Change Box Count, Customer Requests, Item Errors, Customer Rewards Errors, Repair Requests, and feature-gated Exchange Forms. Only Daily Store Log and previously implemented Exchanges & Returns routes are operational; other authorized labels are disabled as `Coming Later`.

See [Store Operations daily completion dashboard](./store-operations-completion-dashboard.md).
