# Scheduling roster, Lead, and Double Coverage semantics

## Participation audit

Scheduling previously used two different meanings of “active.” The Employees page
read `Employee.scheduling_active`, while the Rules page selected every employee,
sorted on global `Employee.active`, and rendered that global field as simply
“Active.” This made historical Square-imported employees look active in a
Scheduling context even when their local Scheduling participation was inactive.

The authoritative local participation root is now `Employee.scheduling_active`.
The shared `is_scheduling_candidate` / `list_scheduling_candidates` service adds
global employee safety and Square-inactive safety for new assignments. Contextual
policy evaluation then adds PTO, availability, store, overlap, consecutive-day,
break, and hour constraints. Principal linkage is used only for self-service
identity and is not an autoscheduler eligibility input. Historical schedule rows
remain visible even after any current eligibility state changes.

The normal Rules workspace selects only Scheduling-active employees and labels
Square state explicitly when it matters. The Employees roster remains the place
to reactivate an inactive participant.

## Persisted roles

`scheduling_lead_capable` and `scheduling_double_coverage` are independent local
employee capabilities. Square synchronization does not update them.

Lead of the Day and Double Coverage are persisted on schedule shifts. A partial
unique index enforces one Lead designation per schedule period and date, while a
second partial unique index prevents more than one Double Coverage assignment for
an employee in the same weekly period. Manual designation flags preserve manager
choices across regeneration while they remain valid.

Generation assigns ordinary staffing, generates explicit extra Double Coverage at
the configured Store Default, repairs daily Lead staffing under hard constraints,
and then designates exactly one fair, deterministic Lead across all stores in the
weekly `SchedulePeriod`. Double Coverage does not satisfy ordinary coverage counts,
does not consume Longview rotation, and does remain part of weekend workload.

Published schedules are not silently rewritten when eligibility changes. Serious
warnings expose lost Lead coverage and unresolved Double Coverage requirements;
managers can make explicit audited designation changes.

## Owner draft workflow

Schedule Automation presents one prominent upcoming workflow: Generate Draft,
Review Schedule, then Regenerate when inputs change. Generate Draft takes the same
transaction-scoped PostgreSQL advisory lock as recurring automation, creates the
configured number of Sunday-through-Saturday periods, and invokes the canonical
generator used by automation. A duplicate or concurrent submission returns the
already-created upcoming draft block instead of creating overlapping periods.

Review links carry the immutable `SchedulePeriod.id`; the board therefore opens
the exact selected revision instead of guessing from a week date. Draft review is
restricted to Scheduling generation/automation managers, and every generation or
regeneration mutation remains feature-gated, capability-gated, and CSRF-protected.
The board displays ordinary uncovered shifts plus Lead and Double Coverage
diagnostics produced by the canonical generator.

Unpublished drafts whose week has ended are retained as historical schedules.
They remain reviewable with their persisted lifecycle stage, shift count, and
warning counts, but are visually separated from the upcoming draft and never
drive the primary owner action. Publication holds remain secondary controls and
do not delete, rewrite, or relabel historical data.
