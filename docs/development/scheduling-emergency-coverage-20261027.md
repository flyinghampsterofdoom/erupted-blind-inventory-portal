# Scheduling emergency coverage candidate — October 27, 2026

Authorized scope: compact scheduling board, account-based Owner Coverage,
company-wide Lead manual exceptions and a departure coverage workflow. Deployment
requires separate authorization after qualification. No production data is used
by the implementation or qualification fixtures.

## Architecture and behavior

The existing SchedulePeriod / ScheduleShift revision model, scheduling advisory
lock, optimistic version checks, immutable approval/publication snapshots,
AuditLog and employee schedule projection remain authoritative. The automatic
assignment evaluator, optimizer and its constraints are unchanged.

Owner Coverage is a nullable principal foreign key on a shift, mutually exclusive
with employee_id. Only an active Owner account can be assigned. Scheduling editors
can choose Owner Coverage in the existing shift editor or accessible move dialog.
The same Owner can cover multiple non-overlapping assignments on a day; overlapping
assignments are rejected. Coverage follows published revision cloning, appears in
employee-facing schedules, and is captured in reports and immutable snapshots.
It produces no employee effective hours, overtime or payroll costs. Only the day
header is light yellow; its native details control lists store, hours and Owner.

The compact board retains the existing inspector, three-dot menu, attendance
controls and original scheduling facts. It removes the duplicated card-level
Call-out / coverage button. Employee rows show name, effective hours and shifts;
original hours, targets and preferences remain in Scheduling details.

Leads retain company-wide editing, approval and publication capabilities. Their
existing permission overrides remain authoritative. Leads can explicitly authorize
an operational assignment exception with a reason; warnings remain visible and
audit evidence remains tied to the exact assignment. Inactive employees, employment
cutoffs, overlapping employee assignments and mandatory rest safeguards are not
made overrideable. Approved PTO requires separate explicit confirmation. Automatic
scheduling continues evaluating the same constraints even after a manual exception.

Leads can publish partial schedules, including missing company-wide Lead coverage,
with the existing publication confirmation. Staffing warnings are retained in the
approval and publication evidence. An explicit denial of publish-with-warnings
still blocks partial publication. Assignment violations require their own exact
authorized exception; a staffing gap does not waive unrelated validation.

Emergency coverage previews affected future assignments across all authorized
stores from the selected employee's final available day. A fingerprint detects
stale previews. The atomic save reuses existing editable drafts or clones current
publications, removes affected assignments into locked open positions, and records
the scheduling eligibility end and audited reason. It does not alter prior published
assignments, employee identity, account activation, prior eligibility or notifications.
The resulting week links open the standard board for reassignment, Owner Coverage,
publication and subsequent republication. A recorded future rehire must be reviewed
before ending eligibility; the workflow never silently deletes a return date.

## Qualification

`tests/test_scheduling_emergency_coverage.py` covers the four-store acceptance
scenario, Owner identity and overlap, payroll isolation, exact manual exceptions,
partial publication, revision history, stale previews, permission denials and an
additive migration preservation / rollback guard rehearsal on disposable PostgreSQL.
Existing scheduling, access, snapshot and publication regressions must also pass.

`tests/browser/scheduling_emergency_server.py` creates only synthetic data against
loopback PostgreSQL and blocks external sockets after setup.
`tests/browser/scheduling_emergency.js` checks desktop 1440×1000 and mobile 390×844,
header-only highlighting, multiple assignments, three-dot controls, no duplicated
call-out buttons, departure preview and absence of page-level horizontal overflow.
Evidence is saved outside the repository in the task output directory.

Run all eight required CI groups and all JavaScript tests using locked Python
3.10.13 dependencies. The configured shared web/cron build and exact-revision CI
qualification are required before deployment. A passing PR merge-ref check alone
does not qualify a different candidate revision.

## Deployment preparation

Migration `20261027_0053` adds one nullable column, a restrictive principal foreign
key and an employee/Owner exclusivity constraint. Existing rows are untouched; no
backfill or employee creation occurs. Apply migrations only during the separately
authorized deployment workflow with maintenance, cron fencing and a verified
backup/restore rehearsal. Both web and cron must use the same qualified source.

Do not downgrade a database containing Owner Coverage evidence: the migration
rejects that operation. Earlier application builds reject schema 0053 at startup;
recovery must use a schema-compatible build or a verified pre-migration restore
before any new production writes. No production migration, merge or deployment
is authorized by this candidate preparation.
