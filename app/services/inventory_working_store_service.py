"""Scheduling-derived inventory scope; session selection is never a grant."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.auth import Role
from app.models import (Employee, Store, SchedulePeriod, SchedulePeriodStatus, ScheduleShift,
                        InventoryStoreAccessGrant, WebSession)
from app.services.v2_scheduling_rules_service import _audit
from app.services.v2_scheduling_policy_service import organization_policy


class WorkingStoreConflict(PermissionError):
    pass


def lock_employee(db, employee_id):
    return db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update()
                     .execution_options(populate_existing=True))


def scheduled_store(db, employee, *, now=None):
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ZoneInfo(organization_policy(db).timezone_name)).date()
    shifts = list(db.scalars(select(ScheduleShift).join(SchedulePeriod).where(
        SchedulePeriod.status == SchedulePeriodStatus.PUBLISHED, ScheduleShift.shift_date == day)))
    from app.services.v2_scheduling_exception_service import expected_employee_id
    stores = {s.store_id for s in shifts if expected_employee_id(db, s) == employee.id}
    return next(iter(stores)) if len(stores) == 1 else None


def working_context(db, *, principal, selected_store_id=None, now=None, lock=False):
    now = now or datetime.now(timezone.utc)
    employee = db.scalar(select(Employee).where(Employee.principal_id == principal.id))
    if employee is None:
        # Preserve legacy shared-store accounts; they have no Scheduling identity.
        return {'store_id': principal.store_id, 'store_name': (db.get(Store, principal.store_id).name if principal.store_id and db.get(Store, principal.store_id) else 'Unassigned'), 'version': 'legacy', 'emergency_options': []}
    if lock:
        employee = lock_employee(db, employee.id)
    if not employee.active:
        return {'store_id': None, 'version': str(employee.working_context_version), 'emergency_options': []}
    normal = scheduled_store(db, employee, now=now) if employee.scheduling_active else None
    if normal is not None and not db.get(Store, normal).active:
        normal = None
    grants = list(db.scalars(select(InventoryStoreAccessGrant).join(Store).where(
        Store.active.is_(True),
        InventoryStoreAccessGrant.employee_id == employee.id,
        InventoryStoreAccessGrant.context_version == employee.working_context_version,
        InventoryStoreAccessGrant.created_at <= now, InventoryStoreAccessGrant.expires_at > now,
        InventoryStoreAccessGrant.revoked_at.is_(None)).order_by(InventoryStoreAccessGrant.id)))
    allowed = {g.store_id for g in grants}
    chosen = selected_store_id if selected_store_id in allowed else normal
    # Include date and grant identities so expiration and A→B→A invalidate browser context.
    local_date = now.astimezone(ZoneInfo(organization_policy(db).timezone_name)).date()
    version = f'{employee.working_context_version}:{local_date}:{chosen}:{",".join(str(g.id) for g in grants)}'
    return {'store_id': chosen, 'store_name': (db.get(Store, chosen).name if chosen else 'Unassigned'),
            'emergency_active': selected_store_id in allowed, 'scheduled_store_id': normal, 'version': version,
            'emergency_options': [{'id': g.store_id, 'name': db.get(Store, g.store_id).name,
                                   'expires_at': g.expires_at.isoformat()} for g in grants]}


def inventory_principal(db, *, principal, selected_store_id=None, now=None):
    context = working_context(db, principal=principal, selected_store_id=selected_store_id, now=now, lock=True)
    if context['store_id'] is None:
        raise WorkingStoreConflict('No unambiguous published working store. Ask a Lead for assistance.')
    return replace(principal, store_id=context['store_id']), context


def assert_inventory_store(db, *, principal, store_id, now=None):
    # Recheck inside the write transaction, before the count/store locks.
    context = working_context(db, principal=principal,
        selected_store_id=db.info.get('inventory_selected_store_id'), now=now, lock=True)
    if context['version'] != 'legacy' and context['store_id'] != store_id:
        raise WorkingStoreConflict('Working store changed. Pending count entries remain at their original store.')
    return context


def authorize_store(db, *, principal, employee_id, store_id, reason, expires_at,
                    expected_version, now=None):
    from app.services.access_control_service import principal_has_permission
    if not principal.active or not principal_has_permission(db, principal=principal,
        permission_key='inventory.emergency_store.authorize',
        fallback_allowed=principal.role in (Role.ADMIN, Role.MANAGER, Role.LEAD)):
        raise PermissionError('Emergency store authorization permission is required.')
    now = now or datetime.now(timezone.utc)
    if not reason.strip() or len(reason) > 2000:
        raise ValueError('Enter a reason of at most 2,000 characters.')
    if expires_at.tzinfo is None or not now < expires_at <= now + timedelta(hours=12):
        raise ValueError('Emergency access must expire within twelve hours.')
    employee = lock_employee(db, employee_id)
    store = db.get(Store, store_id)
    if employee is None or not employee.active or employee.principal_id is None or store is None or not store.active:
        raise ValueError('Choose a linked active employee and active store.')
    if employee.working_context_version != expected_version:
        raise WorkingStoreConflict('Working assignment changed. Review the current store swap before authorizing.')
    # A reasoned grant is explicit authority to work elsewhere, not a schedule edit.
    grant = InventoryStoreAccessGrant(employee_id=employee.id, store_id=store_id,
        authorizing_principal_id=principal.id, reason=reason.strip(),
        context_version=employee.working_context_version, created_at=now, expires_at=expires_at)
    db.add(grant); db.flush()
    _audit(db, principal=principal, action='EMERGENCY_STORE_GRANTED', entity_type='inventory_store_access_grant',
        entity_id=grant.id, reason=reason.strip(), after={'employee_id': employee.id, 'store_id': store_id,
        'scheduled_store_id': scheduled_store(db, employee, now=now), 'expires_at': expires_at.isoformat(),
        'context_version': employee.working_context_version})
    return grant


def select_emergency_store(db, *, principal, web_session_id, store_id, now=None):
    context = working_context(db, principal=principal, now=now, lock=True)
    if store_id not in {s['id'] for s in context['emergency_options']}:
        raise WorkingStoreConflict('No current authorization for this emergency store.')
    session = db.scalar(select(WebSession).where(WebSession.id == web_session_id,
        WebSession.principal_id == principal.id, WebSession.revoked_at.is_(None)).with_for_update())
    if session is None:
        raise WorkingStoreConflict('An active personal session is required.')
    from app.v2.current_store import set_current_store
    set_current_store(db, web_session_id=session.id, store_id=store_id, now=now)
    _audit(db, principal=principal, action='EMERGENCY_STORE_SELECTED', entity_type='web_session',
           entity_id=session.id, after={'store_id': store_id})
