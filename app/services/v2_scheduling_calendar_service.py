"""Read-only calendar metadata. Publication is atomic for an entire period/week."""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.v2_scheduling_board_service import normalize_week_start


def calendar_week_statuses(db: Session, *, start: date, end: date) -> list[dict]:
    if end < start or (end - start).days > 62:
        raise ValueError('Choose a calendar range of at most 63 days.')
    first, last = normalize_week_start(start), normalize_week_start(end)
    # Project metadata and one indexed existence check; never load assignments.
    rows = db.execute(select(
        SchedulePeriod.week_start_date, SchedulePeriod.status, SchedulePeriod.generated_at,
        select(ScheduleShift.id).where(
            ScheduleShift.schedule_period_id == SchedulePeriod.id,
        ).exists().label('has_shifts'),
    ).where(
        SchedulePeriod.week_start_date.between(first, last),
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
        SchedulePeriod.cleared_at.is_(None),
        SchedulePeriod.is_historical.is_(False),
    )).all()
    states: dict[date, set] = {}
    populated_drafts = set()
    for week, status, generated_at, has_shifts in rows:
        states.setdefault(week, set()).add(status)
        if status == SchedulePeriodStatus.DRAFT and (generated_at or has_shifts):
            populated_drafts.add(week)
    result = []
    week = first
    while week <= last:
        statuses = states.get(week, set())
        published = SchedulePeriodStatus.PUBLISHED in statuses
        draft = SchedulePeriodStatus.DRAFT in statuses
        if published and draft:
            status = 'mixed'
        elif published:
            status = 'published'
        elif week in populated_drafts:
            status = 'unpublished'
        else:
            status = 'not-generated'
        label = {
            'mixed': 'Published schedule with unpublished revision',
            'published': 'Published',
            'unpublished': 'Unpublished draft',
            'not-generated': 'Not generated',
        }[status]
        if draft and status == 'not-generated':
            label = 'Not generated (empty draft)'
        result.append({'start': week.isoformat(), 'status': status, 'label': label})
        week += timedelta(days=7)
    return result
