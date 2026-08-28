from __future__ import annotations

from datetime import date, timedelta


# Sunday 2026-01-04 is permanently Week A. Every following Sunday alternates.
# This calendar anchor is independent of generated schedules and exceptions.
ALTERNATING_WEEK_A_ANCHOR = date(2026, 1, 4)
SCHEDULING_WEEKDAY_NAMES = (
    'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')


def sunday(value: date) -> date:
    return value - timedelta(days=(value.weekday() + 1) % 7)


def alternating_week_for_date(value: date) -> str:
    weeks = (sunday(value) - ALTERNATING_WEEK_A_ANCHOR).days // 7
    return 'A' if weeks % 2 == 0 else 'B'


def scheduling_weekday(value: date) -> int:
    return (value.weekday() + 1) % 7


def weekdays_to_mask(days: tuple[int, ...] | list[int]) -> int:
    if any(day < 0 or day > 6 for day in days):
        raise ValueError('Scheduling weekdays must be between zero and six.')
    return sum(1 << day for day in set(days))


def mask_to_weekdays(mask: int | None) -> tuple[int, ...]:
    return tuple(day for day in range(7) if mask is not None and mask & (1 << day))


def mask_label(mask: int | None) -> str:
    if mask is None:
        return 'Not configured'
    days = mask_to_weekdays(mask)
    return ', '.join(SCHEDULING_WEEKDAY_NAMES[day][:3] for day in days) or 'No base days'


def base_pattern_mask(profile, week: str) -> int | None:
    return profile.week_a_workdays_mask if week == 'A' else profile.week_b_workdays_mask


def is_base_workday(profile, value: date) -> bool | None:
    mask = base_pattern_mask(profile, alternating_week_for_date(value))
    return None if mask is None else bool(mask & (1 << scheduling_weekday(value)))
