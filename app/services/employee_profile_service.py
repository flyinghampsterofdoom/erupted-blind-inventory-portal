"""Local person and account operations anchored to Employee.id.

Scheduling policy and imported Square metadata retain their existing owners.
"""
from datetime import datetime, timezone
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role
from app.models import Employee, Principal as PrincipalModel, PrincipalRole, Store
from app.services.access_control_service import principal_has_permission
from app.services.employee_log_service import save_employee
from app.v2.audit import V2AuditEvent, write_v2_audit_event

CONTACT_FIELDS = {'preferred_name': 200, 'phone': 50, 'email': 254,
                  'street_address': 300, 'city': 100, 'state': 100, 'postal_code': 30}
ROLE_LABELS = {'STORE': 'Staff', 'LEAD': 'Lead', 'MANAGER': 'Manager', 'ADMIN': 'Admin'}


def can_manage_employee_accounts(db: Session, actor: Principal) -> bool:
    return actor.role == Role.ADMIN and principal_has_permission(
        db, principal=actor, permission_key='management.users', fallback_allowed=True)


def _employee(db: Session, employee_id: int) -> Employee:
    row = db.execute(select(Employee).where(Employee.id == employee_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ValueError('Employee not found.')
    return row


def _email(value: str) -> str:
    if len(value) > 254 or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', value):
        raise ValueError('Enter a valid email address.')
    return value


def save_personal_information(db: Session, *, actor: Principal, employee_id: int, values) -> Employee:
    if not principal_has_permission(db, principal=actor, permission_key='scheduling.manage_preferences',
                                    fallback_allowed=actor.role in {Role.ADMIN, Role.MANAGER}):
        raise PermissionError('Employee management permission is required.')
    row = _employee(db, employee_id)
    changed = []
    for name, limit in CONTACT_FIELDS.items():
        if name not in values:
            continue
        value = str(values[name]).strip() or None
        if value and len(value) > limit:
            raise ValueError(f'{name.replace("_", " ").title()} must be {limit} characters or fewer.')
        if name == 'email' and value:
            _email(value)
        if getattr(row, name) != value:
            changed.append(name)
            setattr(row, name, value)
    if 'full_name' in values and str(values['full_name']).strip() != row.full_name:
        if row.square_team_member_id:
            raise ValueError('Square-sourced names are read only. Use preferred name for a local display preference.')
        save_employee(db, employee_id=row.id, full_name=str(values['full_name']),
                      visible_to_leads=row.visible_to_leads, active=row.active)
        changed.append('full_name')
    row.updated_at = datetime.now(timezone.utc)
    if changed:
        write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=actor.id,
            action='PERSONAL_INFORMATION_UPDATED', domain='HR', entity_type='employee', entity_id=row.id,
            metadata={'changed_fields': changed}), ip=None)
    db.flush()
    return row


def save_employee_account(db: Session, *, actor: Principal, employee_id: int, action: str,
                          role: str = 'STORE', store_id: int | None = None,
                          password: str = '', login: str = '', confirmed: bool = False) -> PrincipalModel:
    if not can_manage_employee_accounts(db, actor):
        raise PermissionError('Only an Admin with user-management permission can manage employee accounts.')
    employee = _employee(db, employee_id)
    if action not in {'create', 'associate', 'update'}:
        raise ValueError('Choose a valid account action.')
    if not confirmed:
        raise ValueError('Confirm the account identity and application access before saving.')
    if action == 'update':
        if not employee.principal_id:
            raise ValueError('This employee has no linked account.')
        account = db.execute(select(PrincipalModel).where(
            PrincipalModel.id == employee.principal_id).with_for_update()).scalar_one()
        if account.id == actor.id:
            raise ValueError('Use another administrator to change your own application access.')
    else:
        if employee.principal_id:
            raise ValueError('This employee already has an account. Existing links cannot be replaced here.')
        login = _email(login.strip())
        account = db.execute(select(PrincipalModel).where(
            PrincipalModel.username == login).with_for_update()).scalar_one_or_none()
        if action == 'create':
            if account:
                raise ValueError('This login already exists. Review and associate the existing individual account.')
            account = PrincipalModel(username=login, password_hash=None, active=True)
        else:
            if account is None or not account.active:
                raise ValueError('An active account with that exact email login was not found.')
            if account.role.value != role or account.store_id != store_id:
                raise ValueError('Selected role and store must match the existing account before association.')
            if account.id == actor.id:
                raise ValueError('Use another administrator to associate your own account.')
            if db.execute(select(Employee.id).where(Employee.principal_id == account.id)).first():
                raise ValueError('This account is already linked to another employee.')
    before = {'principal_id': employee.principal_id, 'role': account.role.value if account.role else None,
              'store_id': account.store_id}
    if action != 'associate':
        if role not in ROLE_LABELS:
            raise ValueError('Choose Staff, Lead, Manager, or Admin.')
        if role == 'STORE' and store_id is None:
            raise ValueError('Staff accounts require an application store assignment.')
        if role != 'STORE' and store_id is not None:
            raise ValueError('Lead, Manager, and Admin use the existing management access model; application store must be None.')
        if store_id is not None:
            store = db.get(Store, store_id)
            if store is None or not store.active:
                raise ValueError('Choose an active application store.')
        account.role = PrincipalRole(role)
        account.store_id = store_id
        account.updated_at = datetime.now(timezone.utc)
    if action in {'create', 'associate'}:
        account.recovery_email_confirmed = True
    db.add(account)
    db.flush()
    employee.principal_id = account.id
    write_v2_audit_event(db, event=V2AuditEvent(actor_principal_id=actor.id,
        action='EMPLOYEE_ACCOUNT_' + action.upper(), domain='HR', entity_type='employee', entity_id=employee.id,
        before=before, after={'principal_id': account.id, 'role': account.role.value, 'store_id': account.store_id}), ip=None)
    db.flush()
    return account
