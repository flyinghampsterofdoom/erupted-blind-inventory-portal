# V2 Staff Scheduling foundation

## Status and boundary

Milestone 2 implements the schema, permissions, services, feature-gated mutation API, warning cache, copying, templates, time off, and labor-estimate foundation. It does not expose a weekly or monthly scheduling page.

The module remains a single-business Erupted Admin feature. It does not introduce organizations, multi-tenancy, Sling, Square, payroll, automatic scheduling, recurrence, overtime, notifications, or employee self-service pages. Existing V1 behavior remains canonical and unchanged.

The feature key is `staff_scheduling_v2`. It is default-disabled through the existing V2 exposure mechanism. Exposure is independent of `scheduling.*` authorization and authorized store scope.

## Identity and scope

Every mutation records the authenticated principal. `employees.principal_id` is nullable and unique; it is not inferred from names and does not change authentication. Management scheduling works for employees without linked principals. Future personal schedule and time-off pages must return a controlled unavailable state unless a principal is explicitly linked.

Service callers pass server-resolved authorized store IDs. Stores referenced by shifts, templates, operating hours, special hours, coverage rules, and copy operations are checked against that scope. Schedule, shift, template, employee, and request IDs are reloaded server-side.

## Calendar and revision invariants

- Business timezone remains `America/Los_Angeles`.
- Periods use local calendar dates, begin Sunday, and end the following Saturday.
- `(week_start_date, revision_number)` is unique.
- PostgreSQL partial unique indexes allow at most one `DRAFT` and one current `PUBLISHED` period per week.
- Earlier published revisions transition to `ARCHIVED`; they are never deleted.
- Published and archived periods and shifts are immutable.
- Modifying published work clones it into a new draft with source and supersession references.
- Whole-period `version` is locked and checked on every shift mutation. Successful mutations increment it.
- Draft creation, copying, cloning, publication, and time-off review use row locking. The route owns commit or rollback.

## Shift and break invariants

- Employee is nullable for open shifts.
- Overnight shifts are not supported; end time must be later than start time.
- Shift date must be within the owning period.
- Break duration is nonnegative and shorter than the shift span.
- Open shifts contribute open scheduled hours but not employee count, staffed coverage, or labor cost.
- Coverage presence uses the complete start/end span because exact break position is unknown.
- Paid labor hours and estimated cost subtract unpaid break duration.

Shift role/type is configurable through `schedule_shift_types`. Opener and closer are explicit shift flags and are never inferred solely from a role label.

## Warnings and publication

`schedule_warnings` is a persisted, rebuildable cache. A rebuild deletes and deterministically recreates warnings for one schedule revision.

- `INFO`: preference and target indicators.
- `CONFLICT`: overlaps, maximum-hour excess, inactive copied employees, nonpreferred stores, and shifts outside hours.
- `SERIOUS`: approved time off, hard unavailability, closed-date shifts, uncovered open intervals, insufficient minimum staffing, absent required role, opener, or closer.

Drafts remain editable with any warnings. Publishing always runs a full rebuild. Unresolved `SERIOUS` warnings require `scheduling.publish_with_warnings`, explicit confirmation, and a nonempty reason. The audit event records warning IDs and types. `OVERRIDE_REQUIRED` is policy, not a stored severity.

Ordinary and special-hour records determine resolved open intervals. Special hours replace ordinary hours for the date. Coverage rules are clipped to open intervals. Open stores have a baseline minimum of one assigned active employee even when no explicit minimum rule exists.

## Copy and templates

Copy and template instantiation require explicit `MERGE` or `REPLACE` mode when a target draft may exist. They execute within the caller's transaction, create new period/shift IDs, preserve source references only as provenance, validate authorized stores, and rebuild warnings after insertion. Inactive employees remain assigned to copied drafts and receive a conflict warning.

Schedule templates store nonnegative relative day offsets. A multiweek instantiation creates one independent Sunday-through-Saturday draft for every declared template week. No recurrence or rotation engine exists.

## Time off and privacy

Management may enter time off for any active employee. Partial-day records are limited to one date. Approval locks overlapping periods and rebuilds warnings but never deletes or edits shifts.

Reason categories are configurable. Employee notes and management review notes are stored in the time-off domain and must not be serialized into a general schedule-board payload. Review notes require time-off review permission.

## Labor estimate

Compensation rates are effective-dated and may not overlap for one employee. The phase-one estimate is paid scheduled hours multiplied by the rate effective on each shift date. It excludes open shifts and overtime. Missing rates are reported as missing shift count and paid hours rather than treated as zero. Individual hourly rates must never appear in the board response. Aggregate results require `scheduling.view_labor_cost`.

## Permissions

ADMIN and MANAGER default to all management `scheduling.*` capabilities. LEAD and STORE default to none. `scheduling.view_own` and `scheduling.time_off.submit_own` are defined but default off for every role until explicit employee linkage and self-service pages exist. Existing role and principal overrides permit later grants without code changes.

Navigation permissions remain distinct from business permissions.

## Services and API

- `v2_scheduling_service`: period lifecycle, canonical shift validation/mutation, cloning, publication.
- `v2_scheduling_coverage_service`: operating-hour resolution and warning-cache rebuild.
- `v2_scheduling_rules_service`: profiles, availability, time off, operating/special hours, coverage rules, compensation, labor estimate.
- `v2_scheduling_template_service`: configurable references, shift/schedule templates, copies, instantiation.
- `v2_scheduling` router: feature-gated, capability-gated, CSRF-protected period and shift mutations, cloning, and publication.

JSON mutations use the same CSRF cookie token as forms and send it through `X-CSRF-Token`. Existing form-token behavior is unchanged. API conflicts return 409 and stale versions never mutate the period.

## Migration and rollback

Revision `20260718_0003` adds the nullable employee/principal link and sixteen scheduling tables. It neither updates existing rows nor seeds identity links. Downgrade removes only the scheduling tables, enums, indexes, and nullable linkage, returning to `20260716_0002`.

The application schema contract recognizes only `20260718_0003` as the current supported head.

## Milestone 3 dependencies

- Weekly board read/serialization service with privacy and labor redaction.
- Server-rendered weekly shell and warning panel.
- Pointer/keyboard/touch movement UX using the existing canonical APIs.
- Additional API routes for copy, template, profile, hours, coverage, and time-off management as their UIs are introduced.
- Controlled employee-link unavailable state before any self-service route is exposed.
