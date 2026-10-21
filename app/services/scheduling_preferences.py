"""Personal preferences do not change organizational scheduling limits."""
from datetime import timedelta

MAX_CONSECUTIVE_DAYS = 3
CONSECUTIVE_PREFERENCE_COST = 100


def preferred_consecutive_days(profile):
    # Retain the existing storage/API field; no employee settings are rewritten.
    return min(profile.max_consecutive_work_days or MAX_CONSECUTIVE_DAYS,
               MAX_CONSECUTIVE_DAYS) if profile else MAX_CONSECUTIVE_DAYS


def consecutive_preference_windows(work_dates, preferred):
    """Count fully worked windows exceeding the personal preferred block size."""
    days = set(work_dates)
    return sum(all(day + timedelta(days=n) in days for n in range(preferred + 1))
               for day in days)
