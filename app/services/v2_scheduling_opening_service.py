"""One source of truth for trading hours and the required preparation interval.

Opening times are existing StoreOperatingHour / StoreSpecialHour configuration.
Shift templates and global shift defaults are not opening-time authorities.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app.models import StoreOperatingHour, StoreSpecialHour


PREPARATION_MINUTES = 15


class OpeningConfigurationError(ValueError):
    pass


def preparation_start(day: date, opening: time) -> time:
    instant = datetime.combine(day, opening) - timedelta(minutes=PREPARATION_MINUTES)
    if instant.date() != day:
        raise OpeningConfigurationError('Pre-opening preparation cannot cross the scheduling date.')
    return instant.time()


@dataclass(frozen=True)
class OpeningCalendar:
    ordinary: dict
    specials: dict

    def interval(self, store_id: int, day: date) -> tuple[time, time] | None:
        special = self.specials.get((store_id, day))
        if special is not None:
            if special.closed_all_day:
                return None
            intervals = [(special.opening_time, special.closing_time)]
        else:
            intervals = self.ordinary.get((store_id, (day.weekday() + 1) % 7), [])
        if len(intervals) != 1:
            raise OpeningConfigurationError(
                f'Store #{store_id} on {day}: configure one unambiguous opening/closing interval '
                'or an explicit closed-date exception.')
        opening, closing = intervals[0]
        preparation_start(day, opening)
        return opening, closing


def load_opening_calendar(db, *, store_ids, start_date: date, end_date: date) -> OpeningCalendar:
    ordinary = {}
    for row in db.scalars(select(StoreOperatingHour).where(
            StoreOperatingHour.store_id.in_(tuple(store_ids)),
            StoreOperatingHour.active.is_(True))):
        ordinary.setdefault((row.store_id, row.day_of_week), []).append(
            (row.opening_time, row.closing_time))
    specials = {(row.store_id, row.calendar_date): row for row in db.scalars(
        select(StoreSpecialHour).where(
            StoreSpecialHour.store_id.in_(tuple(store_ids)),
            StoreSpecialHour.active.is_(True),
            StoreSpecialHour.calendar_date.between(start_date, end_date)))}
    return OpeningCalendar(ordinary, specials)


def special_hours_violation(db, *, store_id, day, start_time, end_time):
    """Dated exceptions are hard rules, including for manual/unassigned shifts."""
    row=db.scalar(select(StoreSpecialHour).where(StoreSpecialHour.store_id==store_id,
        StoreSpecialHour.calendar_date==day,StoreSpecialHour.active.is_(True)))
    if row is None:return None
    if row.closed_all_day:return ('BUSINESS_CLOSED',f'{row.event_name}: business closed on {day}.')
    try:earliest=preparation_start(day,row.opening_time)
    except OpeningConfigurationError as exc:return ('SPECIAL_HOURS_INVALID',str(exc))
    if start_time < earliest or end_time > row.closing_time:
        return ('OUTSIDE_SPECIAL_HOURS',f'{row.event_name}: shift must fit the special business hours, including the 15-minute opening preparation interval.')
    return None
