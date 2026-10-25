"""Minimal employee-view data from the existing authoritative published cards.

No queries, assignment calculation, or authorization grants live here.
"""

def employee_week_view(*, employee, shifts, stores, employees, days, today,
                       requests_enabled, permission_flags, special_hours=(), now_time=None):
    store_rows = []
    for store in stores:
        key = ''.join(c for c in store.name.lower() if c.isalnum())
        tone = {'highway99': 'hwy99'}.get(key, key)
        store_rows.append({'id': store.id, 'name': store.name, 'tone': tone})
    return {
        'employeeId': employee.id, 'weekStart': days[0].isoformat(),
        'today': today.isoformat(), 'nowTime': now_time, 'days': [d.isoformat() for d in days],
        'notices': [{'storeId': h.store_id, 'date': h.calendar_date.isoformat(),
            'label': h.event_name + (' · Closed' if h.closed_all_day else ' · Modified hours: ' + h.opening_time.strftime('%-I:%M %p') + '–' + h.closing_time.strftime('%-I:%M %p'))} for h in special_hours],
        'stores': store_rows, 'timeOff': bool(permission_flags.get('scheduling.time_off.submit_own')),
        'shifts': [{
            'id': s.id, 'sourceId': s.source_shift_id, 'storeId': s.store_id,
            'date': s.shift_date.isoformat(), 'start': s.start_time.strftime('%H:%M'),
            'end': s.end_time.strftime('%H:%M'), 'employeeId': s.employee_id,
            'name': employees[s.employee_id].full_name if s.employee_id in employees else 'Coverage needed',
            'version': s.period_version, 'coverage': s.is_coverage,
            'covering': employees[s.covering_employee_id].full_name if s.covering_employee_id in employees else None,
            'exception': s.has_exception, 'statuses': s.attendance_statuses,
            'paidHours': s.effective_scheduled_hours, 'originalHours': s.original_scheduled_hours,
            'breakMinutes': s.unpaid_break_minutes,
            'lead': s.display_lead, 'opening': s.is_opener, 'closing': s.is_closer,
            'canRequest': bool(requests_enabled and permission_flags.get('scheduling.transfer_own')
                               and s.employee_id == employee.id and s.transfer_eligible
                               and s.shift_date > today),
        } for s in shifts],
    }
