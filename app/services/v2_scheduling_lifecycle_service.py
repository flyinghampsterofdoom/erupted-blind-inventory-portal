"""Date-specific employee eligibility, independent of roster participation."""
from datetime import date

from app.models import Employee

POST_CUTOFF = 'AFTER_LAST_EFFECTIVE_DATE'


def within_employment_dates(employee: Employee | None, shift_date: date) -> bool:
    """The final effective date is inclusive; missing employees fail closed."""
    return employee is not None and (
        employee.last_effective_date is None or shift_date <= employee.last_effective_date)


def cutoff_message(employee: Employee) -> str:
    return (f'{employee.full_name} is assigned after their Last Effective Date '
            f'({employee.last_effective_date}). Correct this assignment before publication; '
            'this conflict cannot be overridden.')
