# V1 permission and role map

## Authentication model

Authentication is database-backed and cookie-based. `web_sessions.session_token` is stored directly and sent in an HttpOnly cookie. Middleware loads an active principal, extends both DB expiry and cookie max-age on each request, and precomputes all five capability flags. Unauthenticated access is allowed only to `/login` and `/robots.txt`; other requests redirect to login (or return 401 for autosave requests). Passwords use `pwdlib.PasswordHash.recommended()`.

Every non-GET form route uses `verify_csrf` except fetch endpoints that still submit a form token in `FormData`. No ownership concept exists beyond principal identity, store scope, and limited employee visibility.

## Roles and defaults

| Role | Structural rule | Default permissions | Additional hard-coded behavior |
|---|---|---|---|
| `ADMIN` | `store_id` must be null | `management.access`, `management.admin`, `management.groups`, `management.users` | Only role accepted by several Square push/report routes via `require_role(Role.ADMIN)` |
| `MANAGER` | `store_id` must be null; retained as legacy admin role | `management.access`, `management.admin`, `management.groups` | `is_admin_role` treats it as admin, but literal ADMIN routes reject it even if permission overrides allow admin capability |
| `LEAD` | `store_id` must be null | `management.access` | Employee entries/read allowed; employee choices can be filtered by `visible_to_leads` |
| `STORE` | `store_id` required | `store.access` | Store services use the principal’s `store_id`; may technically receive capability overrides, but several service assumptions still expect a store ID |

The database constraint enforces the role/store relationship. `custom_role_label` is display metadata only and grants nothing.

## Capability resolution

Resolution order in `principal_has_permission` is:

1. Principal-specific override (`principal_permission_overrides`), if present.
2. Role override (`role_permission_overrides`), if present.
3. Hard-coded role fallback.

A principal override therefore wins over a role override. Unknown/blank keys fall back to the dependency’s boolean argument. Overrides are evaluated on every request by middleware and again by route dependencies, producing multiple DB reads per request.

| Permission | Default roles | Backend-protected areas/actions | Navigation use |
|---|---|---|---|
| `management.access` | ADMIN, MANAGER, LEAD | Management home; sessions read/detail/export/unlock; chores/opening/change forms/exchanges/non-sellable/customer requests; reports hub and COGS; employee logs through wrapper | Shows management top navigation and most non-admin dashboard cards |
| `management.admin` | ADMIN, MANAGER | Dashboard layout; full store count; cash; store par; ordering; chore templates/delete; change-box delete/audit; master safe; non-sellable item catalog; recount report; stock value; force recount | Marks many dashboard cards `requires_admin`; report hub does not use this flag consistently |
| `management.groups` | ADMIN, MANAGER | Group/campaign/rotation pages; store credentials; manager password reset; Square campaign sync; coverage audit | Shows Manage Groups link/card |
| `management.users` | ADMIN | Management users, access-control role/principal overrides, role dashboard-category access | Shows Users and Access Controls links/cards |
| `store.access` | STORE | All `/store/*` workflows | Root redirect and store workflow access; store home links are not individually permissioned |

## Hard-coded checks outside capabilities

| Check | Where | Effect/risk |
|---|---|---|
| `require_role(Role.ADMIN)` | Count Square sync report; session full/recount push; sales transactions, gross-store, vendor, employee, targeted-demand, inventory-velocity, stock-coverage routes and CSVs; stock-coverage create-order | Ignores `management.admin` role/principal overrides and rejects legacy MANAGER. An ADMIN denied `management.admin` can still reach these routes if it retains literal role and `management.access` middleware authentication |
| `is_admin_role` | Dashboard card visibility; employee-log admin wrapper | Treats ADMIN and MANAGER as admin regardless of fine-grained permission in dashboard visibility unless filtered separately |
| `employee_logs_access` | Employee log page/entry | Requires `management.access`, then rejects STORE even if STORE was granted that permission |
| `employee_logs_admin_access` | Employee/category mutations | Depends on `management.admin`, then also requires ADMIN/MANAGER role; principal overrides cannot elevate LEAD to these actions |
| `principal.role == 'ADMIN'` in report template | Reports hub | Hides most report links from MANAGER even when backend uses `management.admin`; string/enum comparison relies on the role being a string enum |
| `assert_store_scope` / service lookup | Store routes and session service | Prevents store principals reading another store’s session. Most store routes do not accept a store ID at all and use `principal.store_id`, which is stronger |
| `visible_to_leads` | Employee list service | LEAD sees only allowed employees; ADMIN/MANAGER see all. It is row visibility, not a permission key |

## Route protection by functional area

| Area | Read protection | Write protection | Store/ownership behavior |
|---|---|---|---|
| Auth | Public login; authenticated logout | CSRF on login/logout | Session belongs to principal; no concurrent-session limit |
| Store workflows | `store.access` | `store.access` + CSRF | Principal’s fixed `store_id`; count session helper verifies session owner/store |
| Management dashboard/sessions | `management.access` | Session unlock uses only `management.access`; force recount `management.admin`; Square push literal ADMIN | No creator ownership restriction |
| Dashboard settings | `management.admin` | `management.admin` | Global configuration |
| Full store count, cash, store par, ordering, safe/change audit | `management.admin` | `management.admin` | Chosen active store; no per-manager store scope |
| Chore/checklist/forms/exchanges/non-sellable audits | `management.access` | Delete/catalog mutations vary; non-sellable unlock uses only `management.access` | Global read across stores via filter |
| Customer request administration | `management.access` | Item creation and aggregate-count overwrite also only `management.access` | Global catalog, no creator ownership |
| Employee logs | Management access plus role wrapper | Entries for ADMIN/MANAGER/LEAD; taxonomy/employees admin-role only | Lead employee visibility filter |
| Reports | Mixed `management.access`, `management.admin`, literal ADMIN | Stock-coverage order creation literal ADMIN | Store/location filters are not limited per management principal |
| Users/access | `management.users` | `management.users` | Global; users can modify other principals. No explicit self-protection was found |
| Groups/store credentials | `management.groups` | `management.groups` | Global; includes password operations and Square sync |

## Navigation visibility versus backend enforcement

- `base.html` exposes only Dashboard, Counts, Manage Groups, Users, and Access Controls. Most functional navigation is via dashboard cards.
- Dashboard cards combine `requires_admin`, selected permission keys, optional role lists, and role-category visibility. `requires_admin` uses role (`ADMIN`/`MANAGER`), not the resolved `management.admin` flag.
- Role dashboard-category access hides whole dashboard sections but does not block direct route access.
- The reports hub uses literal `ADMIN` visibility for almost all reports, while COGS is visible to all management. Direct backend permissions differ per report.
- Several routes have no dashboard card or only indirect links (emergency editor, PDF templates, detailed report routes), but remain directly accessible when authorized.

## Potentially insufficient or inconsistent protection

| Severity | Finding | Evidence and consequence |
|---|---|---|
| High | Literal-role routes bypass capability denies | `require_role(Role.ADMIN)` does not consult role/principal overrides. Denying `management.admin` does not deny these report and Square-write endpoints to an ADMIN |
| High | Session unlock is only `management.access` | Any management-access principal, including LEAD, can reopen submitted count sessions |
| Medium | Non-sellable unlock is only `management.access` | A LEAD can reopen a submitted stock take |
| Medium | Customer request catalog/count mutation is only `management.access` | Any management principal can change global catalog state and overwrite request counts |
| Medium | Dashboard admin card visibility is role-based | A MANAGER denied `management.admin` may still see admin cards; the route denies after click. An elevated LEAD may have route capability but card remains hidden |
| Medium | Report navigation and backend checks disagree | MANAGER is hidden from routes that accept `management.admin`; some literal ADMIN routes remain accessible after admin capability deny |
| Medium | Group permission includes credential administration | `management.groups` grants store username/password management and manager self-password reset, broader than its label suggests |
| Low | `/robots.txt` and `/login` are exact-path exemptions | Static/public additions would be redirected unless explicitly exempted; current behavior is intentional but brittle |
| Low | No object-level management scope | Management roles can select all stores and records; there is no regional/store assignment model |

These are discovery findings, not a redesign. V2 should reproduce current effective access until a separately approved permission decision changes it.
