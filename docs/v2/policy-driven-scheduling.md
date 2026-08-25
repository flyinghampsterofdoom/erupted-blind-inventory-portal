# Policy-driven scheduling

Status date: 2026-08-25. Policy-driven scheduling `0021` is deployed; the employee-roster extension `0022` is implemented locally and not deployed.

## Architecture

Revision `20260824_0021` extends the existing employee, store, weekly schedule revision, shift,
approved-time-off, permission, warning, and V2 audit domains. It does not create competing identity,
location, PTO, or authorization models. `v2_scheduling_policy_service` is the shared hard-constraint,
generation, special-store rotation, automation, manual-lock, and transfer service.

## Scheduling semantics

- Recurring `HARD_UNAVAILABLE` windows remain the weekday/partial-day lockout source. The policy
  evaluator treats overlaps as hard exclusions. The existing authorized manager override remains
  narrowly limited to recurring availability; it does not waive PTO, overlap, store-never,
  consecutive-day, or maximum-hour rules.
- Approved PTO is authoritative. Generation and transfer acceptance exclude it. Existing draft and
  published assignments remain intact and receive the existing serious warning, so published work is
  never silently removed.
- Profiles add `max_consecutive_work_days` and `minimum_days_off_after_max_block`. The validator reads
  work dates across schedule-period boundaries.
- Organization approval hours default to 40. An optional employee override may replace that value.
  `maximum_weekly_hours` remains a hard ceiling. Transfer approval uses scheduled paid hours, not
  worked/payroll hours.
- Store preferences use Preferred, Acceptable, Avoid, or Never plus the existing optional numeric rank.
  Never is hard; other levels score only after eligibility.
- Normal-store weekend fairness is explicit and precedes store-preference scoring. Saturday and Sunday
  histories are calculated independently from effective persisted assignments over the trailing twelve
  weeks. The least-assigned employee is preferred, then the employee whose equivalent weekend-day
  assignment is oldest, then ordinary preference/target-hour scoring. Special-store shifts are excluded.
  A hard-conflict skip creates no assignment and therefore does not consume the employee's fair turn.
- Consecutive-day validation resolves one effective revision per week (published when present, otherwise
  the newest draft) across all adjacent periods. It expands the contiguous work block both backward and
  forward around the proposal. It also measures the actual off-day gap before and after a block that
  reaches the configured maximum, so `minimum_days_off_after_max_block` cannot reset at a period boundary.
  Locked assignments are ordinary authoritative assignments for this calculation.
- Special stores are explicitly configured. Per-store membership/state records identify primary and
  rotation staff, persist queue position/count/last assignment, and record temporary skips. Primary
  staff resolve first. Rotation uses queue order rather than ordinary preference scoring. A hard-conflict
  skip swaps only one place, preserving the skipped obligation near the front.
- Regeneration clears only unlocked automatic assignments. Every manager-created assigned shift and
  every manual edit is locked and audited; explicit lock/unlock APIs are also available. Locked rows are
  never silently removed or reassigned.
- Uncovered generated shifts return structured exclusion codes and human-readable reasons. This is the
same `ConstraintReason` representation used by manual validation and transfers.

## Square-linked employee roster

**Scheduling → Employees** is the operational roster used by automatic scheduling. Square Team Members
is the source for stable Square identity, display name, Square status, and assigned-location metadata.
The sync is read-only toward Square and reconciles into the existing `employees` row by stable Square ID;
an exact unique normalized-name match bridges a previously unlinked internal employee without duplicating
their record. It never deletes an employee or resets policies, history, principal linkage, global employee
status, or the local Scheduling status.

`employees.scheduling_active` is an independent, Scheduling-only participation decision. Existing rows
retain their prior eligibility during migration; a newly imported Square Team Member starts inactive for
Scheduling until an authorized manager reviews policy and deliberately activates them. Square status is
stored and displayed separately. A Square-inactive employee remains intact but is safely excluded even if
their local Scheduling status is still Active, so the mismatch stays visible rather than silently changing
the manager's decision.

The autoscheduler root set is explicitly `employees` with global employee activity and
`scheduling_active = true`, excluding Square-inactive rows. It does not require a linked principal. PTO,
weekday lockouts, store preference/eligibility, Longview rotation, consecutive-day rules, weekly-hour
limits, overlap detection, and weekend fairness are applied only after this root candidate gate. Transfer
recipient selection uses the same gate. Historical assignments remain renderable after deactivation.

## Transfers and concurrency

A linked employee can offer a future owned shift to another active employee. The recipient may accept
or decline. Acceptance locks the transfer, shift, and recipient employee row, then reruns all hard
validation and scheduled-hour calculation in the same transaction. The partial unique index permits
only one active request per shift. A valid under-threshold transfer changes ownership immediately.

An over-threshold acceptance enters `PENDING_MANAGER` and records existing hours, shift hours,
resulting hours, threshold, and amount over. The original assignment remains until an authorized
manager explicitly approves. Rejection preserves it. Each transition and final ownership change writes
a V2 audit event; scheduling notifications give recipient, giver, and managers the required visibility.

## Lifecycle and automation

The compatibility status (`DRAFT`, `PUBLISHED`, `ARCHIVED`) remains authoritative for existing callers.
The added lifecycle is `PLANNED`, `GENERATED`, `REVIEW`, `PUBLISHED`, `CLOSED`. Automation configuration
separates schedule length, generation offset, publication offset, publication local time, and IANA
business timezone. The job entry point is idempotent: it creates the next weekly periods, uses prior
published shift shapes as open staffing demand, runs policy assignment, and later attempts publication.
Automatic publication never overrides serious warnings and respects the explicit visible hold flag.

## API and UI exposure

All mutations remain feature-gated, capability-gated, CSRF-protected, and transaction-owned by the
route. Management APIs cover employee policies, store preferences, special-store membership, automation,
generation, holds, and locks. Employee APIs cover own published assignments and transfer initiation or
response. Overtime review has a separate management capability. The weekly board serializes lifecycle,
automation/hold state, and manual-lock state; locked cards display a lock marker.

Employee policy configuration is never returned by the own-schedule endpoint and employees have no
policy mutation capability. Employee-facing permissions remain explicit grants because the application
currently has no generic EMPLOYEE role; linkage is through `employees.principal_id`.

## Operations

The application or an external scheduler must invoke `run_schedule_automation` at the desired cadence.
No production job, migration, or feature exposure is changed by this local implementation. Before
release, run the PostgreSQL migration suite with `TEST_POSTGRES_ADMIN_URL`, configure the organization
policy and special-store membership, grant employee self-service permissions to linked principals, and
wire the recurring job in the approved deployment environment.

## Owner and manager workflow

Open **Scheduling → Scheduling Rules** to choose an employee. The employee editor controls target
hours, an optional approval-threshold override (blank inherits the organization value), full-day
weekday lockouts, maximum consecutive days and required recovery days, normal-store preferences, and
special-store participation. Saturday and Sunday history is read-only because the scheduler—not the
manager—owns fairness state. Longview/special-store position and last-assignment information is also
read-only during ordinary employee editing.

Open **Schedule Automation** from Scheduling Rules to set schedule length, generation and publication
offsets, local publication time, and the IANA business timezone. Generated drafts list their planned
publication time and any hold. Adding a hold requires a reason; releasing it permits the next automation
tick to publish only when its configured time is due. The code-side cron entry point is
`PYTHONPATH=. .venv/bin/python scripts/run_schedule_automation.py`. Deployments should invoke it through
their existing platform cron facility. A PostgreSQL transaction advisory lock serializes overlapping
runs, and period/status checks make retries idempotent.

The weekly board shows lifecycle, planned publication, holds, serious coverage warnings, and lock icons.
Use **Regenerate unlocked shifts** to rerun policy assignment; structured exclusion reasons are displayed
for uncovered shifts. Any successful manager creation/edit locks the assignment. A locked card provides
**Unlock for regeneration**; normal edits still run the central hard validator and return its specific
PTO, lockout, overlap, hour, or consecutive-day explanation.

Linked employees with explicit `scheduling.view_own` and `scheduling.transfer_own` grants use **My
Schedule**. They can offer a future published shift, respond to incoming offers, and see status and
notifications. Under-threshold acceptance completes immediately. Over-threshold acceptance remains
assigned to the giver and displays pending manager approval. Authorized managers use **Transfer
Approvals** to see the original/receiving employees, shift/store, existing hours, shift hours, resulting
hours, and amount over threshold. Approval revalidates under row locks immediately before reassignment.
