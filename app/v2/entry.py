"""Human entry policy. Exposure off preserves each historical entry contract."""
from fastapi import HTTPException
from sqlalchemy import select
from app.auth import Principal, Role
from app.models import Employee
from app.services.access_control_service import effective_permission_flags
from app.v2.workspaces import primary_enabled, authorized_return, visible_destinations


def bind_entry_context(request, db, principal):
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id))
    role = Role(getattr(principal.role, 'value', principal.role))
    request.state.principal = Principal(principal.id, principal.username, role,
        principal.store_id, principal.active and (employee is None or employee.active))
    request.state.employee_id = employee.id if employee and employee.active else None
    request.state.permission_flags = effective_permission_flags(db, principal=request.state.principal)
    request.state.v2_primary = primary_enabled(request.state.principal)


def has_operational_workspace(request):
    return any(d.group not in {'My Schedule', 'Store Operations'} for d in visible_destinations(request)) or bool(
        request.state.permission_flags.get('management.access'))


def landing_destination(request, *, source='root', return_to=None, db=None):
    principal = getattr(request.state, 'principal', None)
    if not principal:
        return '/login'
    if not principal.active:
        raise HTTPException(403)
    flags = getattr(request.state, 'permission_flags', {}) or {}
    linked = bool(getattr(request.state, 'employee_id', None))
    if not primary_enabled(principal):
        if source == 'login':
            return '/v2/scheduling/my-schedule' if linked and principal.role in {Role.STORE, Role.LEAD} else '/'
        if source == 'v2':
            if not flags.get('management.access'):
                raise HTTPException(403)
            return '/v2/overview'
        if flags.get('management.access'):
            return '/management/home'
        if flags.get('store.access') or principal.role == Role.STORE:
            return '/store/home'
        return '/login'
    restored = authorized_return(request, return_to, db)
    if restored:
        return restored
    destinations = visible_destinations(request)
    own_schedule = any(d.path == '/v2/scheduling/my-schedule' for d in destinations)
    operational = has_operational_workspace(request)
    # Retain administrators' established operational priority; a Lead can acquire
    # that priority through management.admin without changing authentication.
    prefer_operations = flags.get('management.admin') or principal.role in {Role.ADMIN, Role.MANAGER}
    if own_schedule and not (prefer_operations and operational):
        return '/v2/scheduling/my-schedule'
    if operational:
        return '/v2/overview'
    if flags.get('store.access'):
        return '/v2/store-operations'
    return '/v2/access-help'
