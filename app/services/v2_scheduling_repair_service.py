"""Bounded vacancy-chain repair, not a global schedule optimizer.

A chain changes at most three existing positions. Each donor moves once, leaving
one vacancy for the next donor or a target/coverage endpoint. Donor eligibility can
therefore be checked immediately against its final dates; Lead preservation is
checked only on the completed arrangement. Trial changes are always restored.
"""
from collections import Counter
from datetime import datetime, timezone
from sqlalchemy import select
from app.models import ScheduleShift, SpecialStorePolicy
from app.services.v2_scheduling_context import assignment_context

MAX_CHANGED_POSITIONS = 3
MAX_SEARCH_EXTENSIONS = 256
MAX_REPAIR_ROOTS = 16
MAX_ACCEPTED_REPAIRS = 8


@assignment_context
def repair_required_assignments(db, *, principal, period, diagnostics=None):
    from app.services.v2_scheduling_policy_service import (
        assignment_score, evaluate_assignment, weekly_work_pattern,
    )
    from app.services.v2_scheduling_roster_service import list_scheduling_candidates
    rows = list(db.scalars(select(ScheduleShift).where(
        ScheduleShift.schedule_period_id == period.id).order_by(
        ScheduleShift.shift_date, ScheduleShift.store_id, ScheduleShift.id)))
    movable = [row for row in rows if row.generated_from_coverage_requirement
               and not row.manually_locked and not row.is_double_coverage
               and not row.lead_of_day_manually_assigned]
    employees = list_scheduling_candidates(db)
    lead_ids = {e.id for e in employees if e.scheduling_lead_capable}
    special = set(db.scalars(select(SpecialStorePolicy.store_id).where(
        SpecialStorePolicy.active.is_(True))))
    extensions = 0
    rejected = Counter()
    accepted = 0
    attempted_roots = 0
    root_limit_reached = False

    eligibility_cache = {}

    def eligibility(row):
        # Other employees do not affect an employee's policy eligibility. Cache
        # only while that employee's complete proposed position set is identical.
        key = (row.employee_id, row.id,
               tuple(r.id for r in rows if r.employee_id == row.employee_id))
        if key not in eligibility_cache:
            eligibility_cache[key] = evaluate_assignment(
                db, employee_id=row.employee_id, store_id=row.store_id,
                shift_date=row.shift_date, start_time=row.start_time,
                end_time=row.end_time, unpaid_break_minutes=row.unpaid_break_minutes,
                exclude_shift_id=row.id)
        return eligibility_cache[key]

    def legal_employee(employee_id):
        for row in rows:
            if row.employee_id != employee_id:
                continue
            result = eligibility(row)
            if not result.eligible or result.requires_hour_approval:
                rejected.update(reason.code for reason in result.reasons)
                if result.requires_hour_approval:
                    rejected['WEEKLY_HOURS_APPROVAL_REQUIRED'] += 1
                return False
        return True

    def lead_days():
        return {row.shift_date for row in rows if row.employee_id in lead_ids
                and eligibility(row).eligible}

    while accepted < MAX_ACCEPTED_REPAIRS and extensions < MAX_SEARCH_EXTENSIONS:
        patterns = {e.id: weekly_work_pattern(db, employee_id=e.id,
                    shift_date=period.week_start_date) for e in employees}
        deficits = [e.id for e in employees if patterns[e.id].target_shifts is not None
                    and patterns[e.id].worked_shifts < patterns[e.id].target_shifts]
        if not deficits and not any(row.employee_id is None for row in movable):
            break
        # Existing above-target fallback remains legal for required coverage.
        # When targets are already met, a chain may expose a different legal
        # endpoint than the initial direct-coverage pass could find.
        endpoints = deficits or [e.id for e in employees]
        original = {row.id: row.employee_id for row in rows}
        original_days = {e.id: {r.shift_date for r in rows if r.employee_id == e.id}
                         for e in employees}
        roots = [row for row in movable if row.employee_id is None]
        roots += [row for row in movable if deficits and row.employee_id in patterns
                  and patterns[row.employee_id].target_shifts is not None
                  and patterns[row.employee_id].worked_shifts > patterns[row.employee_id].target_shifts]
        root_limit_reached = root_limit_reached or len(roots) > MAX_REPAIR_ROOTS
        roots = roots[:MAX_REPAIR_ROOTS]
        attempted_roots += len(roots)
        if not roots:
            break
        protected_lead_days = lead_days()
        best = None
        for depth in range(1, MAX_CHANGED_POSITIONS + 1):
            def search(vacancy, path, used_employees):
                nonlocal extensions, best
                if extensions >= MAX_SEARCH_EXTENSIONS:
                    return
                for employee_id in endpoints:
                    if employee_id in used_employees or extensions >= MAX_SEARCH_EXTENSIONS:
                        continue
                    extensions += 1
                    vacancy.employee_id = employee_id
                    db.flush()
                    try:
                        if not legal_employee(employee_id):
                            continue
                        final_lead_days = lead_days()
                        if not protected_lead_days <= final_lead_days:
                            rejected['GLOBAL_LEAD_LOSS'] += 1
                            continue
                        changes = [(row, row.employee_id) for row in path]
                        # Targets cannot decline: donors keep a shift; only an
                        # above-target root occupant may lose one. The terminal
                        # employee gains one (above-target only for uncovered
                        # required coverage). Recheck through the
                        # shared effective-revision target accounting as well.
                        affected = used_employees | {employee_id}
                        if any(weekly_work_pattern(db, employee_id=e, shift_date=period.week_start_date).worked_shifts
                               < min(patterns[e].worked_shifts, patterns[e].target_shifts or 0)
                               for e in affected):
                            continue
                        if not all(legal_employee(e) for e in affected):
                            continue
                        missing_leads = len({row.shift_date for row in rows
                                             if row.employee_id is not None} - final_lead_days)
                        special_changes = sum(row.store_id in special for row, _ in changes)
                        preference = sum(assignment_score(db, employee_id=e, store_id=row.store_id,
                                            shift_date=row.shift_date)[0] for row, e in changes)
                        changed_days = sum(row.shift_date not in original_days[e]
                                           for row, e in changes if e in used_employees)
                        rank = (missing_leads, len(changes), original[path[0].id] is not None,
                                special_changes, changed_days, -preference,
                                tuple((row.shift_date, row.store_id, row.id, e) for row, e in changes))
                        if best is None or rank < best[0]:
                            best = (rank, changes)
                    finally:
                        vacancy.employee_id = None
                        db.flush()
                if len(path) >= depth:
                    return
                for source in sorted(movable, key=lambda row: (
                        row.shift_date != vacancy.shift_date, row.store_id != vacancy.store_id,
                        row.shift_date, row.store_id, row.id)):
                    employee_id = source.employee_id
                    if (source in path or employee_id is None or employee_id in used_employees
                            or employee_id not in patterns or extensions >= MAX_SEARCH_EXTENSIONS):
                        continue
                    extensions += 1
                    source.employee_id = None
                    vacancy.employee_id = employee_id
                    db.flush()
                    try:
                        if legal_employee(employee_id):
                            search(source, path + [source], used_employees | {employee_id})
                    finally:
                        source.employee_id = employee_id
                        vacancy.employee_id = None
                        db.flush()

            for root in roots:
                occupant = root.employee_id
                root.employee_id = None
                db.flush()
                try:
                    search(root, [root], {occupant} if occupant is not None else set())
                finally:
                    root.employee_id = occupant
                    db.flush()
            if (best is not None and best[0][0] == 0) or extensions >= MAX_SEARCH_EXTENSIONS:
                break
        if best is None:
            break
        changes = best[1]
        for row, employee_id in changes:
            row.employee_id = employee_id
            row.updated_by_principal_id = principal.id
            row.updated_at = datetime.now(timezone.utc)
        db.flush()
        accepted += 1
        if diagnostics is not None:
            diagnostics.append({
                'action': 'BOUNDED_REASSIGNMENT_SUCCEEDED',
                'reason': 'REQUIRED_COVERAGE' if original[changes[0][0].id] is None else 'TARGET_WITHOUT_OVERLAP',
                'changes': [{'shift_id': row.id, 'from_employee_id': original[row.id],
                             'to_employee_id': employee_id} for row, employee_id in changes],
                'special_store_changes': best[0][3], 'remaining_global_lead_days': best[0][0], 'extensions': extensions,
            })
    if diagnostics is not None:
        diagnostics.append({
            'action': 'BOUNDED_REASSIGNMENT_SEARCH', 'accepted': accepted,
            'outcome': 'REPAIRED' if accepted else ('NO_REPAIR_WITHIN_BOUNDS' if attempted_roots else 'NO_SEARCH_NEEDED'),
            'extensions': extensions, 'bound_reached': extensions >= MAX_SEARCH_EXTENSIONS,
            'max_changed_positions': MAX_CHANGED_POSITIONS,
            'max_extensions': MAX_SEARCH_EXTENSIONS, 'max_roots': MAX_REPAIR_ROOTS,
            'max_accepted_repairs': MAX_ACCEPTED_REPAIRS,
            'attempted_roots': attempted_roots, 'root_limit_reached': root_limit_reached,
            'repair_limit_reached': accepted >= MAX_ACCEPTED_REPAIRS,
            'rejected_constraints': dict(sorted(rejected.items())),
        })
    return accepted
