# V1 authentication and permission characterization matrix

## Purpose and evidence

This records current compatibility behavior; it does not normalize it. Evidence: `v1-permission-map.md` §Authentication model through §Potentially insufficient or inconsistent protection, `app/security/sessions.py`, `app/auth.py`, and `app/services/access_control_service.py`. Tests: `tests/test_permission_characterization.py`, `tests/test_v2_shell.py`.

## Individual-account product decision

V2 authenticates every employee with an individual principal. Roles remain `STORE`, `LEAD`, `MANAGER`, and `ADMIN`; capabilities remain unchanged. Store assignment is an attribute of the employee principal, not an identity shared by a store. Every V2 operational event must record the authenticated principal ID.

Confirmed V1 compatibility: the data model supports many principals with the same `store_id`, but the seed, UI copy, and operating assumptions use a shared `store1` “store login.” Existing rows attributed to that principal cannot be re-attributed to a person with evidence the system never captured. V1 identities and behavior remain unchanged.

Migration strategy, not executed here:

1. Inventory active principals and determine which are shared without exposing credentials.
2. Provision one principal per employee with the existing role/capability system and current single-store assignment.
3. Keep historical `actor_principal_id` immutable; label known shared principals as legacy in reporting rather than guessing an employee.
4. Expose V2 only to individual tester accounts, then cut over store-by-store with session revocation/rollback planning.
5. Retire shared credentials only after explicit owner approval and observation; never rewrite historical actor IDs.
6. Design multi-store employee assignment separately; `principals.store_id` remains the effective one-store behavior now.

## Default capability matrix

| Role | `management.access` | `management.admin` | `management.groups` | `management.users` | `store.access` |
|---|---:|---:|---:|---:|---:|
| ADMIN | Allow | Allow | Allow | Allow | Deny |
| MANAGER | Allow | Allow | Allow | Deny | Deny |
| LEAD | Allow | Deny | Deny | Deny | Deny |
| STORE | Deny | Deny | Deny | Deny | Allow |

Resolution is principal override, then role override, then this fallback. Either override can allow or deny. A principal override wins even when it contradicts a role override.

## Access mechanisms

| Mechanism | Current behavior | Compatibility consequence |
|---|---|---|
| Session middleware | Loads active principal, slides expiry, computes all permission flags | Navigation consumes flags; backend dependencies re-evaluate capabilities |
| `require_capability` | Applies principal → role → fallback | An override may grant or deny the capability |
| `require_role(Role.ADMIN)` | Exact enum membership | MANAGER fails even though `is_admin_role(MANAGER)` is true |
| `is_admin_role` | ADMIN and legacy MANAGER | Used by selected service/wrapper paths only |
| `employee_logs_access` | Starts with `management.access`, rejects STORE | LEAD is admitted when management access resolves true |
| `employee_logs_admin_access` | Starts with `management.admin`, then requires ADMIN/MANAGER legacy-admin role | A LEAD capability allow still cannot elevate through this wrapper |
| `visible_to_leads` | Non-admin employee list filters hidden employees | Admin-like callers may explicitly include hidden employees |
| `assert_store_scope` | Enforces matching `store_id` only for STORE role | Management-like roles are not store-limited by this helper |

## Visibility versus page/action access

- V1/V2 navigation visibility is derived from permission flags and may differ from route enforcement.
- A visible management page can contain an action guarded by a stricter capability or literal role.
- A hidden link does not prevent direct access if the backend dependency is broader.
- Literal ADMIN endpoints ignore capability grants to MANAGER/LEAD.
- Record ownership and store scope are separate from capability access.

## Test coverage

Automated tests cover all role fallbacks, role allow/deny, principal allow/deny, principal precedence, literal ADMIN versus legacy MANAGER, employee-log wrappers, `visible_to_leads`, STORE ownership, and permission-driven navigation. Route inventory identifies literal ADMIN endpoints; individual business actions still require their module characterization tests before cutover.

## Unresolved decisions

- Which literal ADMIN actions remain owner/admin-only versus named capabilities?
- How are LEAD/MANAGER authorized store sets represented when multi-store assignments arrive?
- Does MANAGER remain a legacy admin synonym indefinitely?
- How will shared V1 principals be identified and phased out without historical re-attribution?
- Which sensitive employee-log events require narrower capabilities or retention rules?
