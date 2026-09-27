# Shared employee profiles — release readiness

Prepared 2026-09-27. Local implementation only; no push, deployment, production migration, employee-data correction, or Square write was performed.

## Baseline and release identity

- Starting checkout: `main`, clean, `5bb06c046fe8fdb51efc63bbc39ecf6bb33087d2`.
- Origin: `git ls-remote origin HEAD refs/heads/main` returned that same SHA during this task.
- Deployed production SHA/schema: **unverified**. Origin equality is not evidence of deployment. Verify the actual deployed revision and schema before approving a release.
- Implementation branch: `codex/shared-employee-profiles`.
- Implementation commit and exact proposed range: recorded in the final release entry below.

## Audit and ownership decisions

| Concern | Existing owner and decision |
| --- | --- |
| Person identity | `Employee` / `employees` is shared by V1 employee logs and V2 Scheduling. Keep every existing employee ID. No new employee/person table. |
| Imported identity | `Employee.full_name`, normalized name, unique Square team-member ID, Square status/location snapshot. No Square mutations. Square-linked full names remain read-only in this profile; local preferred name is independent. |
| Personal information | No existing phone, contact email, postal address, or preferred-name columns. Add nullable typed columns to `employees`, not metadata. Existing full name remains the supported name field; no guessed legal-name split. |
| Authentication | `Principal` / `principals` owns username, password hash, role, active status, store, role label, and permission overrides. `Employee.principal_id` is already a unique nullable FK. Reuse it; never create an Employee during account setup. |
| Roles | UI Staff maps to existing `STORE`; Lead/Manager/Admin map to existing enum values. Scheduling Lead capability remains an independent Employee boolean. The database requires STORE to have a store and management roles to have none. |
| Stores | Home store remains `EmployeeSchedulingProfile.home_store_id`. Store preferences/NEVER remain `EmployeeSchedulingStorePreference`; special-store pools remain `SpecialStoreRotationState`/`SpecialStorePolicy`. Square location snapshots and application store scope are separate existing concepts. |
| Scheduling | `EmployeeSchedulingProfile`, `EmployeeSchedulingWindow`, `TimeOffRequest`, reason categories, organization/store defaults, rotations and compensation remain Scheduling-owned. Existing services validate and audit their changes. |
| History | `ScheduleShift`, schedule revisions, attendance facts, transfers, point entries and notifications retain their existing employee FKs. No history rewrite. |
| Points | Existing `AttendancePointEntry`, reasons and attendance incidents appear under Points / Discipline using existing authorization and mutation routes. No new discipline system. |
| HR records | Existing `EmployeeLogEntry` records appear under HR / Records (latest 50, with a filtered link to the complete V1 logs). Preserve V1 read authorization: Admin/Manager role plus management access. No document store. |
| Lifecycle | `Employee.active` (V1 logs/general availability), `scheduling_active` (local Scheduling archive), Square status, and `last_effective_date` are distinct. Show their meaning; retain existing archive/reactivation and inclusive cutoff behavior. No new hire dates or invented historical values. |
| V1/V2 duplication | Both already use `Employee`. V1 management/log routes and legacy shared/store principals still exist. This change neither merges identities by name nor silently converts shared credentials. |

## Navigation and profile

HR is a primary navigation section with Employees as its only child. Scheduling → Employees stays available. `/v2/hr/employees` and `/v2/scheduling/employees` register the **same callable directory and profile handlers**, using the same templates, records and services. Canonical profile links/save destinations use `/v2/hr/employees/{id}`; old URLs and Scheduling-owned mutation endpoints remain compatible. Keeping the existing handler implementations in `v2_scheduling.py` avoids duplicating or rewriting the generator-adjacent form logic.

The compact identity summary is followed by Personal Information, Employment, Scheduling, Account & Access, Points / Discipline (when authorized), and HR / Records. Personal Information opens initially; other domains collapse. Existing scheduling controls, fairness/operational diagnostics, availability windows and requested dates live in Scheduling. Time-off links use employee ID and preserve that exact filter through status/sort navigation.

Profile access and HR exposure deliberately retain the existing `staff_scheduling_v2` feature gate and `scheduling.manage_preferences` permission. Navigation does not bypass backend permissions. This does not introduce a new, broader HR permission grant or a speculative HR suite.

Personal saves and account saves have separate explicit actions on the same profile. They cannot overwrite scheduling settings. Application-account changes require both ADMIN role and the existing `management.users` capability. New accounts default to Staff; role/store selection is explicit. Association requires an existing active email login, an explicit individual-identity confirmation, matching selected role/store, and an unclaimed unique link. Existing overrides/password/store/role remain unchanged during association. Self-access changes and link replacement are rejected. Account operations are audited without passwords; personal audits record changed field names without copying contact data into audit metadata.

## Schema and employee migration

Revision `20260927_0027`, parent `20260923_0026`, adds seven nullable columns to `employees`:

- `preferred_name VARCHAR(200)`
- `phone VARCHAR(50)`
- `email VARCHAR(254)`
- `street_address VARCHAR(300)`
- `city VARCHAR(100)`
- `state VARCHAR(100)`
- `postal_code VARCHAR(30)`

No backfill, identity matching, account creation, role change, store reassignment, or employee-data update occurs in the migration. All seven fields start NULL. Existing employee/account IDs, names, lifecycle values and scheduling rows are unchanged. Contact email and authentication username are distinct concepts: a contact edit does not rename a login. Existing logins may continue using their current usernames; new/associated logins in this workflow require email format.

The schema contract advances to `20260927_0027`. Migration must precede serving the new code. Downgrade drops the new contact columns and would discard contact values entered after release; preserve those values before any approved rollback. No rollback was performed on production.

## Verification

Results and exact changed-file list are recorded in the final release entry below. All database tests use generated disposable databases on local PostgreSQL 16.12, not the configured application or production database. Browser checks use rendered synthetic test fixtures served on loopback.

## Preserved behavior and remaining release considerations

- Existing Scheduling saves retain targets, A/B days, recurring lockouts, approval thresholds, home store, NEVER restrictions, special-store participation, Lead and double-coverage capability, private notes, and cutoff dates through the original services.
- Archive/reactivation retains settings and published history. Generator and coverage algorithms are unchanged.
- Points mutation permissions, time-off decisions, historical attribution, V1 employee-log identity and Square read-only policy remain intact.
- Existing individual-account self-service permissions remain default-off where the current policy disables them. Creating a login does not silently enable self-service scheduling or grant Scheduling Lead capability.
- The V1 shared-store credential directory and upsert exclude employee-linked principals. Explicit attempts to edit an individual login through that shared-account editor are rejected.
- Legacy management roles retain their current authorization breadth. Admin must review role/permission overrides before linking an existing login, and confirm it is individual rather than shared. No automatic matching/backfill attempts to infer this.
- HR currently shares the Scheduling feature/employee-management permission gate. A future independent HR rollout can introduce deliberately scoped permissions; it is not included here.
- Production revision/schema remain unverified. No claim of deployment readiness supersedes that release check.

## Final verification record

| Run | Result |
| --- | --- |
| Full repository regression suite (`PYTHONPATH=. .venv/bin/pytest -q`, isolated local PostgreSQL configured) | **865 passed, 1 failed, 1 skipped; 7 subtests passed**, 292.14 seconds. |
| Scheduling foundation/repair, HR, shell, migration/schema, Square boundary targeted run | **235 passed, 1 failed**, 218.45 seconds. |
| Final HR, shell, migration and schema component run after the V1 account guard | **49 passed**, 30.63 seconds. |
| Final employee-profile tests, including direct shared-login overwrite rejection and application-Lead independence | **8 passed**, 11.00 seconds. |
| Final HR and navigation rerun after page identity/UI wording cleanup | **28 passed**, 11.02 seconds. |
| JavaScript syntax / patch whitespace | `node --check app/static/v2/v2.js` and `git diff --check` passed. |
| Browser | Synthetic Admin and Manager profiles rendered; verified HR and Scheduling Employees links, collapsible domains, preserved Scheduling controls, Staff default, and explicit association confirmation. |

The full-suite run preceded the last small V1 shared-login guard/UI refinements; the final targeted runs cover those changes. The one failure in both broad runs is `test_automation_page_renders_owner_workflow_and_draft_query_requires_management`, which expects the pre-existing automation page to contain “Review Schedule.” The same test was run from a clean `git archive` of baseline `5bb06c0` and failed at the same assertion (3.26 seconds). Its page, test, and generator are unchanged. The optional real-R2 private delivery/authentication test remains skipped because `RUN_REAL_R2_TESTS` is not enabled. Existing FastAPI startup deprecation warnings remain.

Migration tests exercise clean installation/schema comparison and upgrading a populated `20260923_0026` database without changing employee IDs or lifecycle state; the contact migration is also downgraded in that disposable database. Existing generation, requested-day-off, store restrictions, Lead coverage, lifecycle, cutoff, published-history and employee-editor atomicity regressions pass. New HTTP tests exercise both entry paths, contact persistence/rollback, CSRF, feature gates, authorization denials, exact-ID time-off filtering, records permission checks, account creation/association and uniqueness, and separate role/Scheduling behavior.

**Release assessment:** implementation is ready for owner review, not deployed. The suite is not wholly green because of the reproduced baseline failure. Production revision/schema verification and the release decision remain outstanding. No production data migration/backfill is proposed beyond adding nullable columns.

## Exact changed files

- `app/main.py`
- `app/models.py`
- `app/routers/v2_employees.py`
- `app/routers/v2_scheduling.py`
- `app/schema_contract.py`
- `app/services/employee_profile_service.py`
- `app/services/session_service.py`
- `app/static/v2/scheduling.css`
- `app/static/v2/v2.js`
- `app/templates/v2/employees/account.html`
- `app/templates/v2/employees/personal.html`
- `app/templates/v2/employees/records.html`
- `app/templates/v2/employees/time_off.html`
- `app/templates/v2/scheduling/employee_policy.html`
- `app/templates/v2/scheduling/employees.html`
- `app/templates/v2/scheduling/time_off_queue.html`
- `app/v2/navigation.py`
- `docs/v2/shared-employee-profile-release-readiness.md`
- `migrations/versions/20260927_0027_employee_contact.py`
- `tests/test_schema_migration_postgres.py`
- `tests/test_square_cost_workflow_boundaries.py`
- `tests/test_v2_digital_signage.py`
- `tests/test_v2_employee_profiles.py`
- `tests/test_v2_shell.py`
