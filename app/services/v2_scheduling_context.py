"""Scoped revision context shared by Scheduling evaluation and repair.

Adjacent weeks retain their authoritative published revision (or latest draft).
The week being edited uses the explicit target revision, even when it is empty.
"""
from functools import wraps
from datetime import timedelta
from sqlalchemy import select
from app.models import SchedulePeriod, SchedulePeriodStatus, ScheduleShift

CONTEXT_KEY = 'scheduling_assignment_period_id'


def assignment_context(function):
    @wraps(function)
    def contextual(db, *args, **kwargs):
        previous = db.info.get(CONTEXT_KEY)
        period_id = kwargs.get('schedule_period_id')
        if period_id is None and kwargs.get('period') is not None:
            period_id = kwargs['period'].id
        if period_id is None and kwargs.get('shift') is not None:
            period_id = kwargs['shift'].schedule_period_id
        if period_id is None:
            period_id = previous
        if period_id is None and kwargs.get('exclude_shift_id') is not None:
            shift = db.get(ScheduleShift, kwargs['exclude_shift_id'])
            period_id = shift.schedule_period_id if shift else None
        if period_id is None and kwargs.get('shift_date') is not None:
            day = kwargs['shift_date']
            week = day - timedelta(days=(day.weekday() + 1) % 7)
            period_id = db.scalar(select(SchedulePeriod.id).where(
                SchedulePeriod.week_start_date == week,
                SchedulePeriod.status == SchedulePeriodStatus.DRAFT))
        db.info[CONTEXT_KEY] = period_id
        try:
            return function(db, *args, **kwargs)
        finally:
            if previous is None:
                db.info.pop(CONTEXT_KEY, None)
            else:
                db.info[CONTEXT_KEY] = previous
    return contextual
