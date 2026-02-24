# Blind Inventory Portal (MVP)

FastAPI + Jinja + PostgreSQL starter for blind store inventory counts.

## What is implemented
- Cookie-based auth with hashed passwords (`pwdlib` recommended Argon2id profile)
- Route-level RBAC (`STORE`, `LEAD`, `ADMIN`/legacy `MANAGER`) with store scoping
- Audit logging (`auth_events`, `audit_log`)
- Store flow: generate count sheet from campaign + RECOUNT queue, save draft, submit/lock
- Submit flow now fetches current on-hand and computes variance (`counted - current_on_hand`)
- Rotation engine per store with manager force-next controls
- Recount loop: non-zero variances appear next day under `RECOUNT` until two consecutive variance signatures match
- Square update stub audit event when consecutive signatures match
- Manager flow: list sessions, dedicated group-management page, force recount, view expected/count/variance, unlock, export CSV
- Robots protections: `X-Robots-Tag` + `robots.txt` disallow all
- CSRF protection for mutating form posts

## Schema
Run `/Users/justinrawlinson/Desktop/Erupted Weekly Stock Automation/sql/schema.sql` against PostgreSQL.

## Environment
Create `.env` in project root:

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/blind_inventory
APP_SECRET_KEY=replace-this
SESSION_COOKIE_NAME=blind_inventory_session
SESSION_TTL_MINUTES=60
SESSION_COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=lax
SNAPSHOT_PROVIDER=square
SQUARE_READ_ONLY=true
SQUARE_ACCESS_TOKEN=your_production_read_token
SQUARE_APPLICATION_ID=your_app_id
# Optional: pin a Square API version date. Leave blank to use account default.
SQUARE_API_VERSION=
ORDERING_REORDER_WEEKS_DEFAULT=5
ORDERING_STOCK_UP_WEEKS_DEFAULT=10
ORDERING_HISTORY_LOOKBACK_DAYS_DEFAULT=120
```

When `SNAPSHOT_PROVIDER=square`, each `stores.square_location_id` must be populated with one of your real Square location IDs.

## Sync all campaigns from Square
You can pull all current campaign candidates from Square (read-only) in two ways.
Campaign candidates are generated from Square **reporting categories only** (sidecar categories are ignored):

1. Manager UI:
   - Login as manager
   - Open `/management/groups`
   - Use **Sync Campaigns From Square**
2. CLI:
```bash
cd "/Users/justinrawlinson/Desktop/Erupted Weekly Stock Automation"
source .venv/bin/activate
python -m app.sync_square_campaigns --min-items 1
```

Then create your counting groups on `/management/groups`. Any active campaign not assigned to a group is ignored during store count-sheet generation.

## Suggested dependencies
```bash
pip install fastapi uvicorn sqlalchemy psycopg[binary] jinja2 python-multipart pwdlib pydantic-settings
```

## Run
```bash
uvicorn app.main:app --reload
```

## One-click macOS setup/run
- Double-click `/Users/justinrawlinson/Desktop/Erupted Weekly Stock Automation/Setup Blind Inventory Portal.command` once to install/check dependencies and initialize DB.
- Then double-click `/Users/justinrawlinson/Desktop/Erupted Weekly Stock Automation/Start Blind Inventory Portal.command` to launch the app.
- The launcher uses `/Users/justinrawlinson/Desktop/Erupted Weekly Stock Automation/scripts/bootstrap_and_run.sh` and is safe to rerun (idempotent setup).

CLI equivalents:
```bash
cd "/Users/justinrawlinson/Desktop/Erupted Weekly Stock Automation"
./scripts/bootstrap_and_run.sh setup-only
./scripts/bootstrap_and_run.sh run
```

## Required seed data
Insert at least:
- one `stores` row
- one `campaigns` row with `active=true`
- one `principals` row for manager/admin (`role='ADMIN'` or legacy `role='MANAGER'`, `store_id=NULL`)
- optional: one `principals` row for lead (`role='LEAD'`, `store_id=NULL`)
- one `principals` row for a store login (`role='STORE'`, `store_id=<store id>`)

Use hashed passwords from `pwdlib` (same algorithm used in app).
