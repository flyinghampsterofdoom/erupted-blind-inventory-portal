# Blind Inventory Portal (MVP)

FastAPI + Jinja + PostgreSQL starter for blind store inventory counts.

## What is implemented
- Cookie-based auth with hashed passwords (`pwdlib` recommended Argon2id profile)
- Route-level RBAC (`STORE`, `LEAD`, `ADMIN`/legacy `MANAGER`) with store scoping
- Audit logging (`auth_events`, `audit_log`)
- Store flow: generate count sheet from campaign + RECOUNT queue, save draft, submit/lock
- Submit flow now fetches current on-hand and computes variance (`counted - current_on_hand`)
- Rotation engine per store with manager force-next controls
- Recount loop: non-zero variances appear next day under `RECOUNT`; items drop immediately when variance reaches `0`
- Per-item closeout: if an item shows the same non-zero variance for three total counts, Square on-hand is auto-updated to counted qty and the item is removed from `RECOUNT` on successful push
- Manager flow: list sessions, dedicated group-management page, force recount, view expected/count/variance, unlock, export CSV
- Robots protections: `X-Robots-Tag` + `robots.txt` disallow all
- CSRF protection for mutating form posts

## Schema
Schema changes are versioned with Alembic. For a new empty database run:

```bash
python -m app.schema_contract upgrade
```

For an existing database, first create a disposable migrated reference, then use `validate` and `stamp-existing`; see `docs/v2/v2-schema-baseline-and-environment.md`. Application startup validates the revision and never mutates schema.

## Environment
Create `.env` in project root:

```env
DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/blind_inventory
ENVIRONMENT=development
DEMO_SEED_ENABLED=false
SCHEMA_REVISION_CHECK_ENABLED=true
APP_SECRET_KEY=replace-this
SESSION_COOKIE_NAME=blind_inventory_session
SESSION_TTL_MINUTES=60
SESSION_COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=lax
SNAPSHOT_PROVIDER=square
SQUARE_READ_ONLY=true
SQUARE_ACCESS_TOKEN=your_production_read_token
SQUARE_APPLICATION_ID=your_app_id
SQUARE_TIMEOUT_SECONDS=3600
# Optional: pin a Square API version date. Leave blank to use account default.
SQUARE_API_VERSION=
ORDERING_REORDER_WEEKS_DEFAULT=5
ORDERING_STOCK_UP_WEEKS_DEFAULT=10
ORDERING_HISTORY_LOOKBACK_DAYS_DEFAULT=120
V2_ENABLED_FEATURES=
V2_PRINCIPAL_FEATURES=
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
cd "/Users/justinrawlinson/Desktop/Erupted Admin Backend"
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
- Double-click `/Users/justinrawlinson/Desktop/Erupted Admin Backend/Setup Blind Inventory Portal.command` once to install/check dependencies and initialize DB.
- Then double-click `/Users/justinrawlinson/Desktop/Erupted Admin Backend/Start Blind Inventory Portal.command` to launch the app.
- The launcher uses `/Users/justinrawlinson/Desktop/Erupted Admin Backend/scripts/bootstrap_and_run.sh` and is safe to rerun (idempotent setup).

CLI equivalents:
```bash
cd "/Users/justinrawlinson/Desktop/Erupted Admin Backend"
./scripts/bootstrap_and_run.sh setup-only
./scripts/bootstrap_and_run.sh run
```

## Demo seed policy

Demo data is disabled by default. To deliberately seed a local non-production database, set `ENVIRONMENT=development` and `DEMO_SEED_ENABLED=true`, then run `python -m app.seed_example`. Production-like environments refuse demo seeding even if enabled.

If `ENVIRONMENT` is omitted, the application uses the production-safe value `production`. The convenience bootstrap refuses non-local database URLs.

## Required operational data
Insert at least:
- one `stores` row
- one `campaigns` row with `active=true`
- one `principals` row for manager/admin (`role='ADMIN'` or legacy `role='MANAGER'`, `store_id=NULL`)
- optional: one `principals` row for lead (`role='LEAD'`, `store_id=NULL`)
- one individual `principals` account per employee; a STORE employee uses `role='STORE'` and the current single assigned `store_id`

Use hashed passwords from `pwdlib` (same algorithm used in app).

V2 treats authentication as per-person. Roles and capability behavior are unchanged. Existing shared V1 store principals remain a compatibility concern and are not migrated automatically.

## V2 Digital Signage

The default-disabled Digital Signage module and its private R2 configuration are documented in [`docs/v2/digital-signage.md`](docs/v2/digital-signage.md). It does not alter V1 or expose itself without explicit V2 feature configuration.
