# Canonical local Admin V2 development baseline

Established September 30, 2026 on `codex/admin-v2-canonical-integration` in
`/Users/justinrawlinson/Desktop/Erupted-Admin-V2-Canonical`.
This is the starting point for subsequent local Admin development. It is not a
production release, a main-branch update, or authorization to change Render.

## Incorporated source

| Source | Treatment |
| --- | --- |
| `35d6c7f3683a1503dbab65a32fabddfdb1486828` | Exact integration parent: deployed Scheduling cleanup plus HR/profile and all prior application work. |
| `6586f88797b4ecb6e98a592156fc126ba3706b2e` | Complete Employee Access patch relative to `adbe5ea68c920914b53fcccba08a9b61ccfb45bf`, applied onto the production source. |
| `1c443b1dece9fa74035ce41b5daeb78295c5768a` | Complete A1 immutable generator-snapshot patch relative to `35d6c7f`, applied onto the combined source. |

No rollback-bridge code from `1affccd` was used. Original feature, HR, Scheduling,
A1 and rollback branches/worktrees remain evidence; none was rewritten. This
single integration commit preserves source attribution here instead of merging
historical sibling topology. The original feature readiness documents are
historical records; their separate heads and validation counts do not describe
this consolidated line.

## Migration graph

Before integration:

```text
20260927_0027
├── 20260928_0028  A1 immutable generator snapshots
└── 20260929_0028  Employee Access
```

After integration:

```text
20260927_0027
├── 20260928_0028 ──┐
└── 20260929_0028 ──┴── 20260930_0029
```

The single head is `20260930_0029`. Its two-parent Alembic merge has no DDL.
All established migrations, including both feature revisions, are byte-for-byte
unchanged. A fresh database has 170 application tables. The merge requires both
feature schemas; it does not rename or recreate employees or accounts.

Disposable PostgreSQL checks cover upgrading from production `20260927_0027`
and from either feature revision; preservation of employee identity, account
links, contact values and existing password hashes; coexistence of all feature
tables; and rejection of every incomplete schema by the strict startup gate.
The integrated application supports only the merged head and never migrates on
startup. A database still equivalent to production is rejected without mutation.

Downgrading the merge to an explicit parent leaves both parent version rows and
both schemas; the runtime deliberately rejects that intermediate state. Relative
`downgrade -1` is ambiguous at this merge point. Use explicit reviewed revision
targets. An empty-schema downgrade to `20260927_0027` and upgrade back to head are
tested. Existing parent policies remain: snapshot evidence refuses destructive
downgrade; passwordless accounts refuse downgrade that would invent passwords.
Employee Access downgrade can remove integration configuration, so these empty
local roundtrips are not a production rollback plan or permission to discard data.

## Conflict and semantic review

Four Git conflicts concerned only the required head in `app/schema_contract.py`,
two head assertions, and the migration table-count assertion. These now require
the merged head and combined count, without broadening supported revisions.
`app/models.py` merged both additive model changes. Scheduling regression tests
retain production's cleanup and its deterministic planning clock, plus Employee
Access's own-schedule permission expectation.

Employee routes, profile/account services, authentication, encryption, CSRF and
My Schedule code remain identical to the Employee Access source. A1 policy and
snapshot services remain identical to the approved A1 source; production cleanup
files remain unchanged. There is one canonical Employee model linked through
`Employee.principal_id`. No identity backfill or duplicate employee store was added.

My Schedule reads published ScheduleShift/SchedulePeriod rows for the linked
employee, without snapshot/audit retrieval or Admin directory data. Its existing
Monday-based current/next-week presentation remains intact; the management board
retains Sunday-start scheduling periods. A1 captures existing generator output
and provides active-Admin-only internal retrieval; no employee HTTP endpoint
exposes evidence payloads. Role overrides and Staff/Lead/Manager/Admin boundaries
remain those of the approved features.

## Reproducible local validation

A worktree-local ignored `.venv` was prepared using Python 3.10.13 and the exact
validated dependencies recorded in `canonical-validation-constraints.txt`.
FastAPI 0.129.0, Starlette 0.52.1, pytest 9.0.2, cryptography 46.0.7 and Alembic
1.20.0 are included. `pip check` passes. The dependency snapshot is for local
validation; production requirements/deployment behavior were not repinned.

To recreate the runtime, install `requirements.txt` with
`-c docs/development/canonical-validation-constraints.txt` into a local virtualenv.
The test command below deliberately makes the default application DB unreachable;
fixtures create and remove uniquely named disposable databases on loopback.
Set the local PostgreSQL administrator user for your workstation explicitly.

```sh
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 \
DATABASE_URL=postgresql+psycopg://invalid:invalid@127.0.0.1:1/unreachable \
TEST_POSTGRES_ADMIN_URL=postgresql+psycopg://justinrawlinson@localhost/postgres \
SQUARE_ACCESS_TOKEN= SNAPSHOT_PROVIDER=mock \
.venv/bin/python -m pytest -q -rs tests
```

Do not copy a production `.env` into this worktree. Normal local app startup also
requires an explicitly configured disposable/local database at the merged head.
No operational database was used for validation. Real email delivery was mocked;
no real employee identities or provider credentials were used.

## Browser acceptance

A loopback-only preview on port 8772 used a newly migrated disposable database and
synthetic identities. The temporary harness was outside the application tree and
mocked both password delivery and the Admin test-email transport.

- Admin login and dashboard: passed.
- HR employee profile and Account & Access: passed; created a synthetic individual
  Lead account, verified all four role choices, saved Lead display, pending-password
  state and Admin reset action.
- Password setup/reset: browser verified mock-inbox link, fragment removal, form
  rendering and required-field validation. Local HTTP exercised successful setup,
  token replay rejection and reset redemption. Browser then verified login and
  session invalidation following reset. No real credentials were changed.
- Forgot password: known and unknown synthetic addresses produced the same public
  response; delivery stayed within the mock.
- Staff My Schedule: passed; own published current-week opening shift and next-week
  closing shift rendered, without management navigation or edit controls.
- Newly created Lead login: passed; neutral empty schedule and correct role display.
- Admin Integrations: passed; saved ordinary configuration and exercised mocked test
  email. Encryption, replacement-key handling, secret redaction and CSRF were also
  covered by automated HTTP/database tests.
- Scheduling Readiness and Board: passed; existing readiness gates, published
  read-only state, week navigation and quiet shift-card layout rendered correctly.

The first Board attempt exposed a preview-fixture error: periods had Monday start
instead of the Board's Sunday-start contract. Only synthetic fixture dates were
corrected; no application change was made. Staff attempting the Admin settings
page was blocked; automated HTTP tests assert its 403 and role boundaries.

## Production and Render boundary

No push, main merge, deployment, production migration/data change, real email,
Square call or Render read/write was performed during integration. The quarantined
cron service and web service were not accessed. No cron state was refreshed or
changed; the September 30 reconciliation remains the infrastructure evidence.

Keep Render services, their branch/env/command/schedule/autodeploy/suspension and
build/deployment state untouched. Do not run historical release scripts. The web
and cron share a production DB, so local work must not use those credentials or
invoke the production automation endpoint. The permanent Square boundary in
AGENTS.md and the centralized request policy remain unchanged.

## Final validation results

| Run | Result |
| --- | --- |
| Focused access/profile/A1/schema/Square-boundary/Ordering suites | 91 passed, 0 failed, 0 skipped |
| Focused Scheduling base-pattern, capability and owner-workflow selection | 140 passed, 0 failed, 0 skipped; 173 outside the selection deselected |
| Migration suite plus new canonical graph/path tests | 12 passed, 0 failed, 0 skipped |
| Complete repository suite | 1,066 passed, 0 failed, 1 skipped; 7 subtests passed; 363.12 seconds |

Focused runs total 231 passing cases; these and the migration cases are also
included in the complete suite, not additional unique tests. The sole skip is
`tests/test_v2_digital_signage.py::test_real_r2_private_delivery_and_authentication_separation`
(the optional real-R2 integration guarded by `RUN_REAL_R2_TESTS=1`); no private
object-store credentials or live integration were enabled. Three full-suite
warnings are non-failing deprecations, not skipped validation.

The original Employee Access report's failing owner-workflow assertion now passes
with the production Scheduling cleanup's already-existing deterministic planning
clock. No assertion was removed or weakened to achieve the result. The full suite
used the original validated runtime; the 231 focused cases were additionally run
in the matching worktree-local `.venv`, whose `pip check` passes.

The first new migration-test attempt used ambiguous relative downgrade `-1` and
had 3 failures/1 pass. Correcting only that test to use an explicit parent revision
produced the final passing migration run above. No application or migration gate
was weakened.

Validation logs on the workstation: `/tmp/canonical-full.log`,
`/tmp/canonical-focused-final.log`, `/tmp/canonical-scheduling-focused.log`, and
`/tmp/canonical-migration-final.log`. The loopback preview and its disposable database
were removed after acceptance. Original worktrees were rechecked clean with their
original branch tips before this integration was committed.
