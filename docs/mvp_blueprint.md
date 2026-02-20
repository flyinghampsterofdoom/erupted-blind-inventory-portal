# Blind Inventory Portal MVP Blueprint

## 1) Architecture
- Backend: FastAPI + server-rendered Jinja templates
- Database: PostgreSQL
- Auth: username/password (Argon2id preferred)
- Session auth: signed secure cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, short TTL)
- Authorization: route-level RBAC + store scoping in all DB queries
- Auditability: append-only auth and action logs

## 2) Principal/RBAC model
Use one principal model for auth:
- `MANAGER`: global access; can view expected/count/variance for all stores; can unlock submitted sessions
- `STORE`: scoped to one `store_id`; can only create/edit/submit their own sessions; never sees expected on-hand

## 3) Route map
All routes require auth except `GET/POST /login`.

### Auth
- `GET /login` -> login page
- `POST /login` -> verify credentials; create session cookie; log auth event (success/fail)
- `POST /logout` -> invalidate session; clear cookie; audit logout

### Store user
- `GET /` -> redirect by role (`/store/home` or `/management/sessions`)
- `GET /store/home` -> employee name prompt + generate count sheet action
- `POST /store/sessions/generate` -> create `count_session` + snapshot from provider
- `GET /store/sessions/{session_id}` -> blind entry page (SKU, name, variation, counted qty)
- `POST /store/sessions/{session_id}/draft` -> upsert entry rows
- `POST /store/sessions/{session_id}/submit` -> transactional submit + lock + audit

### Management
- `GET /management/sessions` -> list all sessions w/ filters (store, status, date)
- `GET /management/sessions/{session_id}` -> full variance view (expected/count/delta)
- `POST /management/sessions/{session_id}/unlock` -> set editable status + audit reason
- `GET /management/sessions/{session_id}/export.csv` -> CSV export + audit event

### Infra
- `GET /robots.txt` -> disallow all
- Middleware/header on all responses: `X-Robots-Tag: noindex, nofollow, noarchive`

## 4) Request guards and invariants
- Require auth on all routes (deny by default)
- Role guard (`require_role`) on each router
- Store scope guard:
  - Store principals can only access rows where `count_sessions.store_id = principal.store_id`
  - Store-facing templates never include `expected_on_hand`
- Session status guard:
  - Only `DRAFT` sessions can accept draft edits/submit
  - `SUBMITTED` (locked) cannot be edited until manager unlocks back to `DRAFT`
- All state transitions and sensitive views/exports write to `audit_log`

## 5) Transaction boundaries
`POST /store/sessions/{id}/submit` should run in one DB transaction:
1. Lock session row (`SELECT ... FOR UPDATE`)
2. Validate principal scope + status is `DRAFT`
3. Validate required entries exist (or define partial-submit policy)
4. Persist any final entries
5. Set `submitted_at`, `status='SUBMITTED'`
6. Insert `audit_log` record
7. Commit

## 6) Snapshot provider abstraction
Define interface now to keep Square wiring isolated:
- `SnapshotProvider.generate(store_id, campaign_id) -> list[SnapshotLineInput]`

Implementations:
- `MockSnapshotProvider`: static/mock catalog + mock expected quantities
- `SquareSnapshotProvider` (later):
  - fetch campaign-filtered variations
  - fetch inventory counts for store `square_location_id`
  - return snapshot lines persisted into DB

## 7) Security checklist
- Argon2id password hashing (`pwdlib`/`argon2-cffi`) or bcrypt fallback
- Login rate limit and backoff
- CSRF token on all mutating form posts
- Rotate session ID on login
- Session TTL short (e.g., 20-30 min idle); server-side revocation table optional for MVP
- `Secure` cookies in non-local environments
- Never leak username existence in login response text

## 8) Suggested project layout
```text
app/
  main.py
  db.py
  models.py
  auth.py
  dependencies.py
  security/
    passwords.py
    sessions.py
    csrf.py
    headers.py
  services/
    snapshot_provider.py
    mock_snapshot_provider.py
    square_snapshot_provider.py
    session_service.py
    audit_service.py
  routers/
    auth.py
    store.py
    management.py
  templates/
    base.html
    login.html
    store_home.html
    count_entry.html
    management_sessions.html
    management_session_detail.html
sql/
  schema.sql
```

## 9) MVP acceptance criteria
- Store user cannot access another store’s session by URL tampering
- Store user never sees expected on-hand in HTML or response payloads
- All login attempts logged with timestamp, username, IP, user-agent, success/failure
- All protected routes require authenticated principal
- Robots protections present (`robots.txt` + `X-Robots-Tag`)
- Submit locks a session; edits fail until manager unlock
- Manager can export CSV and see “No variance to report” when applicable
