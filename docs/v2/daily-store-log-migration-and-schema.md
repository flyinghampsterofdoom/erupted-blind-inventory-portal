# Daily Store Log schema and migration record

Alembic revision file `20260716_0002_daily_store_logs.py` has revision ID `20260716_0002` and follows `20260715_0001`.

It creates:

- `daily_store_logs`
- `daily_store_log_actions`

It also adds nullable `current_store_id` and `current_store_checked_at` columns to `web_sessions`. These fields hold temporary server-side operating context and do not change principals or authorization.

The business uniqueness contract is `UNIQUE (store_id, log_date)`. Submission and action fingerprints have independent unique constraints. Lifecycle/action/check constraints reject unsupported state and prohibit no-issues plus follow-up.

No scheduling, shift, task, notification, attachment, inventory, ordering, maintenance-ticket, or cash columns/tables are added.

Upgrade paths:

1. Fresh disposable database: upgrade to `head`.
2. Revision `20260715_0001`: upgrade to `head`.
3. Matching unversioned V1 schema: compare with a reference at `20260715_0001`, stamp that exact revision, then upgrade to `head`.

The Daily Store Log revision remains a required ancestor. Runtime startup accepts only current repository head `20260725_0008` and does not create or alter schema.

The revision's downgrade to `20260715_0001` drops the two V2 Daily Store Log tables and the two nullable Current Store session columns. Production rollback defaults to disabling exposure and restoring compatible application code; no production downgrade or data rewrite is authorized.
