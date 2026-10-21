# Phase 1: canonical V2 entry and workflow-preserving shell

Baseline: `94594090ef44ee3c329f75069407bc599b268aae` in the clean canonical
checkout. This release candidate changes human routing and presentation only.
It does not deploy, activate production exposure, migrate a schema, change
business services, or contact/configure external integrations.

## Entry and rollback contract

The single `app/v2/entry.py:landing_destination` policy serves successful login,
authenticated `/`, `/v2`, `/v2/`, already-authenticated `/login`, and primary-mode
V2 forbidden-page recovery. In primary mode it chooses:

1. An explicitly reviewed local GET return destination, after authorization.
2. An active linked employee's exposed own-schedule workspace.
3. Operational overview when an operational capability is available.
4. Store Operations for store access.
5. Account/access help for an otherwise active authenticated account.

ADMIN/MANAGER retain operational priority when they have operational access.
A linked Lead can acquire that priority through effective `management.admin`;
being linked alone never grants any operational capability. Shared accounts do
not get an employee schedule. An unavailable scheduling feature leads to a usable
operational/store/help destination instead of a known 404.

The exposure key is **`v2_primary`**, using the existing `FeatureExposure` global
and per-principal architecture. It is absent/off by default. Future authorized
activation can add it to `V2_ENABLED_FEATURES` or use
`V2_PRINCIPAL_FEATURES=<principal-id>:v2_primary`. Domain exposure remains
independent: for example, My Schedule still needs `staff_scheduling_v2` and its
permission. No production configuration was changed for this candidate.

Rollback removes `v2_primary` for the affected cohort, retaining every other
feature entry. The same running account/session and database records remain
valid; historical login/root behavior and legacy chrome return. Historical
`/v2` remains a management-only preview entry. If rollout is not configured at
all, anonymous GET redirects also retain the exact `/login` target. With a
per-principal rollout configured, anonymous GET intent can be carried to login,
but it is ignored after authentication for accounts outside the cohort.

## Return destinations

Only a GET can be captured by middleware. Unsafe requests are never stored or
replayed; autosave still receives 401. The login form carries the inert target
through credential retry. Authorization is re-evaluated against the successfully
authenticated account before issuing the redirect.

`app/v2/workspaces.py` is the shared primary presentation registry. It supplies
workflow links and the return allowlist. Reject external/scheme-relative URLs,
backslashes, ambiguous encoded paths, dot traversal, control characters,
fragments, unreviewed paths, and mutation/download-shaped paths. Preserve the
original query string for accepted destinations. This is deliberately not a
prefix-based allowlist or an execution of arbitrary GET handlers.

Reviewed record destinations cover purchase orders, count sessions, HR and
scheduling employee profiles, and legacy chore/opening/change/non-sellable/
exchange records. They require their workflow guard and existence; store count
records additionally use the existing ownership checker. Explicit schedule
period selection uses the existing draft-period access checker. Saved report
views use the existing private-view owner check. STORE query scope cannot select
another store. Unreviewed record links fall back to the user's workspace; direct
bookmarks still work normally after authentication. Broader record restoration
can be added only with the corresponding read/ownership guard.

Password-reset URL fragments, token redemption, eligibility, email delivery,
credential invalidation, and password rules are unchanged.

## Navigation and compatibility

The primary projection uses the existing `NavigationSection`/`NavigationChild`,
V2 base, sidebar/drawer JavaScript, component styles and accessibility conventions.
The previous preview registry remains intact when exposure is off. Primary
visibility follows actual route capabilities, feature exposure, literal role
restrictions, and required identity/context; it does not treat preview navigation
permissions as grants to domain routes. Route/service checks remain authoritative.

The launcher groups meaningful entry pages into My Schedule, Store Operations,
Counts & Cash, Scheduling, Inventory / Ordering, Financials, Reports, Employees /
HR, Administration / Settings, Digital Signage and Touchscreen. It does not list
underlying save/delete/retry endpoints. The registry's `compatibility` property
explicitly identifies legacy destinations internally. Domain exposure can hide
native modules without removing their legacy alternatives.

Retained compatibility entry points include:

- Store counts, chores, opening checklists, non-sellable counts, change forms,
  change-box counts, customer requests and exchange forms.
- Their management histories/review pages, correction queue and admin counts.
- Cash reconciliation, change-box/master-safe audits, store needs and delivery.
- Purchase-order execution/receiving, mappings, pars, PDF templates and emergency
  on-hand correction.
- Every dedicated legacy report entry, retaining ADMIN-only restrictions where
  applicable rather than substituting V2 reports.
- Employee logs, count groups/shared credentials, users, access controls, chore
  task editor, dashboard configuration and integrations.

Legacy pages that already inherit `base.html` select V2 chrome in primary mode.
Their content, form actions, service handlers and page scripts remain unchanged.
`legacy_base.html` retains prior chrome; the unchanged autosave/session script is
factored into `legacy_autosave.html` and shared by both presentations. Compatibility
styles remain scoped to main content and contain wide legacy tables. This is a
shared shell adapter, not a per-workflow migration or redesign. Standalone pages
such as integration settings retain their existing presentation and routes.

Native scheduling/financial pages keep their forms and domain implementations;
only the shared navigation/chrome changes. Device templates, authentication,
media, URLs, service worker and device sessions are untouched.

## Security and scope verification

- Principal override > role override > fallback precedence remains unchanged.
- Primary links are checked in tests against actual route dependency guards for
  every supported role with default and broad explicit overrides.
- Financial literal-role and integration ADMIN guards remain in place.
- Store and reporting scope policy is unchanged; current-store context is not
  promoted into an authorization grant.
- No domain router or service authorization is weakened to support navigation.
- Employee linkage is derived from authenticated `Principal.id`; submitted
  employee IDs do not select another employee's own schedule.
- Existing unsafe actions retain CSRF and audit behavior. The new launcher/help
  endpoints only render presentation.
- Session expiry, non-renewing session-status polling and logout revocation are
  exercised through the actual application middleware.
- No change to Square request policy, scheduling execution gates, models,
  migrations, financial services, or device routers/security modules.

## Validation

The existing worktree-local Python 3.10.13 environment passes `pip check`.
Database-backed tests use fresh randomly named local PostgreSQL databases,
apply the unchanged baseline migrations and drop those test databases afterwards.
The default application database is deliberately unreachable. Square credentials
are empty, snapshot provider is mock, and provider-specific tests use mocks.

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
DATABASE_URL=postgresql+psycopg://invalid:invalid@127.0.0.1:1/unreachable \
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://justinrawlinson@localhost/postgres \
SQUARE_ACCESS_TOKEN= SNAPSHOT_PROVIDER=mock \
.venv/bin/python -m pytest -q -rs --tb=short tests
```

New coverage is in `tests/test_v2_primary.py`. Existing tests were not relaxed.
It covers the role/link/exposure landing matrix, all entry aliases, authenticated
login, inactive identities, GET/query restoration, open-redirect and unsafe-action
rejection, record ownership, private saved views, draft schedule authorization,
CSRF, expiry, polling, logout, per-account rollback, native form preservation,
legacy bridges and independent device sessions.

Browser checks used a loopback-only, disposable-database synthetic admin preview:
desktop launcher, 390px mobile launcher and drawer, and legacy Users inside the
primary shell. Wide legacy tables scroll inside the workspace, not the document.
The preview was stopped and its database removed; no production data or provider
credentials were used. The preview proxy supported read-only navigation; HTTP
mutations and authentication were tested with the full-application TestClient.

## Final test results

- Final full suite, PostgreSQL enabled, final implementation tree:
  **1,263 passed, 2 failed, 1 skipped, 7 subtests passed**, 573.00 seconds.
  Both failures are the independently reproduced baseline scheduling cases below.
- The single skip is the explicitly opt-in real private R2 integration test
  (`RUN_REAL_R2_TESTS=1`); it was not enabled because external integration contact
  is outside this phase. No PostgreSQL-backed tests were skipped for unavailable
  database configuration.
- Focused primary-entry and existing shell suites: **62 passed**, 43.02 seconds.
  All are also included in the final full-suite result above.
- Earlier existing shell, employee-access, permission-characterization and
  employee-profile selection: **70 passed**, 40.57 seconds; also covered by the
  final full suite.
- Three dependency deprecation warnings concern AnyIO's BlockingPortal alias
  and FastAPI startup `on_event`; no test was relaxed or removed.
- `pip check`, `git diff --check`, and exact legacy shell/autosave extraction
  comparison pass. Desktop/mobile visual checks are described above.

An initial broad run additionally found a lightweight preview navigation context
without an `active` attribute. The primary exposure check now treats absent active
state as ineligible; the final suite confirms preview compatibility. No domain
permission fallback was introduced.

## Known baseline regression blocker

The full regression run exposed two existing failures in
`tests/test_v2_scheduling_repair.py::test_audited_generation_discovers_three_position_chain_and_is_deterministic`
(the `False` and `True` parameter cases). Both assert a particular employee
assignment and receive `13` where `9` is expected. Running only these two cases
against an independently extracted, untouched production revision
`94594090ef44ee3c329f75069407bc599b268aae` reproduces both failures:
**2 failed, 23 deselected in 10.86s**. No scheduling implementation or existing test
was changed to mask them. This candidate cannot claim an entirely passing suite;
these baseline failures remain an acceptance blocker requiring separate review.

## Explicit later-phase decisions

1. Reporting permission currently also permits replenishment PO creation. This
   discrepancy is unchanged and still needs an explicit authorization decision.
2. Legacy report definitions and exports are not replaced by similar V2 reports.
3. Standalone integration/auth presentation and the legacy workflows remain later
   migrations; this phase supplies reachability and shared chrome only.
4. Unreviewed record/export deep links need dedicated authorization contracts
   before being added to login restoration; never probe them by executing GETs.
5. Keep operational current store distinct from assigned-store authorization.
6. New routes must update the presentation registry/guard comparison when exposed;
   visibility must not become a second authorization engine.

## Exact files changed

- `app/main.py`
- `app/routers/auth.py`
- `app/routers/v2.py`
- `app/security/sessions.py`
- `app/static/v2/compatibility.css` (new)
- `app/static/v2/primary.css` (new)
- `app/templates/base.html`
- `app/templates/legacy_autosave.html` (new; extracted unchanged script)
- `app/templates/legacy_base.html` (new; preserved legacy shell)
- `app/templates/login.html`
- `app/templates/v2/access_denied.html`
- `app/templates/v2/access_help.html` (new)
- `app/templates/v2/base.html`
- `app/templates/v2/launcher.html` (new)
- `app/v2/entry.py` (new)
- `app/v2/navigation.py`
- `app/v2/workspaces.py` (new)
- `tests/test_v2_primary.py` (new)
- `docs/v2/primary-entry-phase-1-release-readiness.md` (new)
