"""Review the effects of an explicit owner edit within its scheduling transaction."""
from collections import defaultdict
import hashlib
import hmac
import json

from app.auth import Role
from app.config import settings
from app.services.v2_scheduling_service import SchedulingValidationError

LEAD = 'NO_LEAD_OF_DAY'
STAFFING = {'NO_ASSIGNED_EMPLOYEE', 'INSUFFICIENT_COVERAGE', 'DOUBLE_COVERAGE_UNFILLED'}
PREFERENCES = {'OUTSIDE_PREFERENCE', 'NONPREFERRED_STORE', 'ABOVE_PREFERRED_WORKDAYS'}
OTHER_COVERAGE = {'EXCESS_COVERAGE', 'LONGVIEW_WEEKLY_TRAVELER_UNSATISFIED'}
REVIEWED_WARNING_TYPES = STAFFING | PREFERENCES | OTHER_COVERAGE | {LEAD}
WEEKLY_WARNINGS = {'ABOVE_PREFERRED_WORKDAYS', 'LONGVIEW_WEEKLY_TRAVELER_UNSATISFIED'}
LABELS = {
    LEAD: ('without assigned lead coverage', 'dates'),
    'STAFFING': ('below minimum staffing', 'dates'),
    'EXCESS_COVERAGE': ('above configured staffing', 'dates'),
    'LONGVIEW_WEEKLY_TRAVELER_UNSATISFIED': ('without required Longview traveler coverage', 'weeks'),
    'ELIGIBILITY': ('with assignments outside scheduling eligibility', 'dates'),
    'OUTSIDE_PREFERENCE': ('outside preferred hours', 'dates'),
    'NONPREFERRED_STORE': ('with assignments at a nonpreferred store', 'dates'),
    'ABOVE_PREFERRED_WORKDAYS': ('with work above preferred days', 'weeks'),
    'ASSIGNMENT_POLICY': ('with scheduling policy exceptions', 'dates'),
}


def warning_snapshot(db, period):
    from app.services.v2_scheduling_coverage_service import evaluate_schedule_warnings
    if period is None or period.is_historical or period.cleared_at is not None:
        return []
    return [w for w in evaluate_schedule_warnings(db, schedule_period_id=period.id)
            if w.warning_type in REVIEWED_WARNING_TYPES]


def _staffing_increased(row, previous):
    deficit = row.required_count - row.actual_count
    baseline = [w for w in previous if w.warning_type in STAFFING
                and w.warning_date == row.warning_date and w.store_id == row.store_id
                and w.required_count - w.actual_count >= deficit]
    if row.start_time is None or row.end_time is None:
        return not any(w.start_time is None or w.end_time is None for w in baseline)
    cursor = row.start_time
    for w in sorted((w for w in baseline if w.start_time and w.end_time), key=lambda w: w.start_time):
        if w.start_time > cursor:
            break
        cursor = max(cursor, w.end_time)
        if cursor >= row.end_time:
            return False
    return True


def new_warnings(before, after, *, days=None, not_before=None):
    def key(w):
        return (w.warning_type, w.warning_date, w.store_id, w.employee_id,
                w.start_time, w.end_time)
    prior = {key(w): w for w in before}
    for w in after:
        if not_before and w.warning_date < not_before:
            continue
        if days is not None and w.warning_date not in days and w.warning_type not in WEEKLY_WARNINGS:
            continue
        earlier = prior.get(key(w))
        if w.warning_type in STAFFING:
            if not _staffing_increased(w, before):
                continue
            code = 'STAFFING'
        else:
            if earlier:
                worsened = (
                    w.warning_type in {'ABOVE_PREFERRED_WORKDAYS', 'EXCESS_COVERAGE'}
                    and w.actual_count - w.required_count > earlier.actual_count - earlier.required_count
                ) or (w.warning_type == LEAD and w.actual_count == 0 and earlier.actual_count > 0)
                if not worsened:
                    continue
            code = w.warning_type
        yield dict(code=code, date=w.warning_date.isoformat(), message=w.message,
                   store_id=w.store_id, employee_id=w.employee_id)


class OwnerReviewRequired(SchedulingValidationError):
    def __init__(self, *, groups, token):
        super().__init__('This change needs your review. Nothing has been saved.')
        self.groups = groups
        self.token = token


class OwnerScheduleReview:
    def __init__(self, principal, action, fields, confirmation):
        self.principal = principal
        self.action = action
        self.fields = fields
        self.confirmation = confirmation
        self.warnings = []

    def add(self, code, day, message, **details):
        self.warnings.append(dict(code=code, date=day.isoformat(), message=message, **details))

    def policy_exceptions(self, reasons, day, *, override_pto=False):
        from app.services.scheduling_operational_exception_service import OPERATIONAL_CODES, PTO_CODES
        allowed = OPERATIONAL_CODES | (PTO_CODES if override_pto else set())
        hard = [r for r in reasons if r.code not in allowed]
        if hard:
            raise SchedulingValidationError(hard[0].message)
        for r in reasons:
            self.add('ASSIGNMENT_POLICY', day, r.message, policy_code=r.code)
        return [{'code': r.code, 'message': r.message} for r in reasons]

    def compare(self, before, after, **scope):
        self.warnings.extend(new_warnings(before, after, **scope))

    def finish(self, db, *, entity_type, entity_id):
        if not self.warnings:
            return
        from app.services.v2_scheduling_rules_service import _audit
        entries = sorted({json.dumps(item, sort_keys=True) for item in self.warnings})
        payload = json.dumps([self.principal.id, self.action, self.fields, entries], sort_keys=True, default=str)
        token = hmac.new(settings.app_secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(token, self.confirmation or ''):
            grouped = defaultdict(lambda: defaultdict(set))
            for entry in map(json.loads, entries):
                grouped[entry['code']][entry['date']].add(entry['message'])
            groups = []
            for code, dates in sorted(grouped.items(), key=lambda item: list(LABELS).index(item[0])):
                label, unit = LABELS[code]
                count = len(dates)
                groups.append(dict(summary=f'{count} {unit if count != 1 else unit[:-1]} {label}',
                    dates=[dict(date=day, messages=sorted(messages)) for day, messages in sorted(dates.items())]))
            raise OwnerReviewRequired(groups=groups, token=token)
        _audit(db, principal=self.principal, action='OWNER_SCHEDULING_OVERRIDE', entity_type=entity_type,
               entity_id=entity_id, metadata={'action': self.action, 'warnings': list(map(json.loads, entries))})


def owner_schedule_review(db, principal, *, action, fields, confirmation=''):
    from app.services.scheduling_operational_exception_service import can_override
    if principal.role == Role.ADMIN and can_override(db, principal):
        return OwnerScheduleReview(principal, action, fields, confirmation)
    if confirmation:
        raise PermissionError('Only the authorized owner may confirm this scheduling override.')
    return None
