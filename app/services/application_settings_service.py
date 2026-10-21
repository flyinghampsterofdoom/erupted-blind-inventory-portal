"""Typed operational configuration. Only this module accesses settings persistence."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from app.auth import Role
from app.config import settings
from app.models import ApplicationSetting
from app.services.employee_profile_service import can_manage_employee_accounts
from app.services.audit_service import log_audit


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ResendConfiguration:
    enabled: bool = False
    from_email: str = ''
    from_name: str = ''
    reply_to: str = ''
    portal_url: str = ''
    api_key: str = field(default='', repr=False)
    key_configured: bool = False
    status: str = 'Not configured'


def email_address(value):
    value = str(value).strip()
    if len(value) > 254 or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', value):
        raise ConfigurationError('Enter a valid email address.')
    return value


def _cipher():
    try:
        return Fernet(settings.integration_encryption_key or '')
    except (ValueError, TypeError):
        raise ConfigurationError('Integration encryption key is unavailable or invalid.') from None


def resend_configuration(db, *, decrypt=False):
    row = db.get(ApplicationSetting, 'integration.resend', populate_existing=True)
    if not row:
        return ResendConfiguration()
    values = row.values
    key = ''
    status = 'Disabled' if not values.get('enabled') else 'Configuration incomplete'
    if row.encrypted_secrets and decrypt:
        try:
            key = json.loads(_cipher().decrypt(row.encrypted_secrets.encode()))['api_key']
        except (InvalidToken, ValueError, KeyError, TypeError):
            return ResendConfiguration(**values, key_configured=True, status='Unable to decrypt API key; check bootstrap key or replace API key.')
    if values.get('enabled') and row.encrypted_secrets and values.get('from_email') and values.get('portal_url'):
        status = 'Configured' if not decrypt or key else status
    return ResendConfiguration(**values, api_key=key, key_configured=bool(row.encrypted_secrets), status=status)


def save_resend_configuration(db, *, actor, values, replacement_key=''):
    if actor.role != Role.ADMIN or not can_manage_employee_accounts(db, actor):
        raise PermissionError('Admin user-management permission required.')
    clean = dict(enabled=values.get('enabled') == 'true',
        from_email=email_address(values['from_email']) if values.get('from_email') else '',
        from_name=str(values.get('from_name', '')).strip(),
        reply_to=email_address(values['reply_to']) if values.get('reply_to') else '',
        portal_url=str(values.get('portal_url', '')).strip().rstrip('/'))
    if len(clean['from_name']) > 100 or any(c in clean['from_name'] for c in '\r\n<>'):
        raise ConfigurationError('Sender display name is invalid.')
    try:
        url = urlsplit(clean['portal_url'])
        _ = url.port
    except ValueError:
        raise ConfigurationError('Portal URL is invalid.') from None
    if len(clean['portal_url']) > 2048 or any(c.isspace() for c in clean['portal_url']):
        raise ConfigurationError('Portal URL is invalid.')
    if clean['portal_url'] and (url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path):
        raise ConfigurationError('Portal URL must be an HTTPS origin without a path, credentials, query or fragment.')
    db.execute(text("SELECT pg_advisory_xact_lock(782113)"))
    row = db.get(ApplicationSetting, 'integration.resend', populate_existing=True)
    if row is None:
        row = ApplicationSetting(key='integration.resend', values={})
        db.add(row)
    changed = [key for key in clean if row.values.get(key) != clean[key]]
    if replacement_key:
        if len(replacement_key) > 1000 or any(c.isspace() for c in replacement_key):
            raise ConfigurationError('API key format is invalid.')
        row.encrypted_secrets = _cipher().encrypt(json.dumps({'api_key': replacement_key}).encode()).decode()
        changed.append('api_key_replaced')
    row.values = clean
    row.updated_at = datetime.now(timezone.utc)
    log_audit(db, actor_principal_id=actor.id, action='INTEGRATION_SETTINGS_UPDATED',
        session_id=None, ip=None, metadata={'integration': 'resend', 'changed_fields': changed})
    db.flush()


def employee_recovery_address(db, *, account, employee):
    """Resolve a confirmed address bound to this login, person and current contact."""
    if not account or not account.active or not employee or not employee.active or not account.recovery_email_confirmed:
        return None
    row = db.get(ApplicationSetting, f'employee.recovery.{account.id}', populate_existing=True)
    if row:
        values = row.values
        if (values.get('employee_id') != employee.id
                or values.get('username') != account.username.casefold()
                or values.get('email') != str(employee.email or '').strip().casefold()):
            return None
        address = values.get('email', '')
    else:
        address = account.username
    try:
        return email_address(address).casefold()
    except ConfigurationError:
        return None


def confirm_employee_recovery(db, *, actor, employee_id, confirmed):
    """Admin confirmation reuses the existing employee contact; no login rename."""
    from sqlalchemy import select, func
    from app.models import Employee, Principal
    from app.services.password_reset_service import invalidate_credentials
    if not can_manage_employee_accounts(db, actor):
        raise PermissionError('Admin user-management permission required.')
    if not confirmed:
        raise ConfigurationError('Confirm the employee recovery address before saving.')
    # Serialize confirmations, including uniqueness checks across employee contacts.
    db.execute(text('SELECT pg_advisory_xact_lock(782114)'))
    employee = db.scalar(select(Employee).where(Employee.id == employee_id).with_for_update())
    if not employee or not employee.active or not employee.principal_id:
        raise ConfigurationError('An active linked employee is required.')
    account = db.scalar(select(Principal).where(Principal.id == employee.principal_id).with_for_update())
    if not account.active:
        raise ConfigurationError('An active account is required.')
    # Email logins keep their established address; short logins use the explicitly
    # reviewed employee contact. Never infer a recipient from a similar name.
    try:
        email_address(account.username)
        address = account.username.casefold()
        bind_contact = False
    except ConfigurationError:
        address = email_address(employee.email or '').casefold()
        bind_contact = True
    if bind_contact and db.scalar(select(Employee.id).join(Principal, Principal.id == Employee.principal_id).where(
            Employee.id != employee.id, Employee.active.is_(True), Principal.active.is_(True),
            func.lower(Employee.email) == address)):
        raise ConfigurationError('Recovery email is shared by another active employee account. Review ownership first.')
    key = f'employee.recovery.{account.id}'
    row = db.get(ApplicationSetting, key, populate_existing=True)
    values = {'employee_id': employee.id, 'username': account.username.casefold(), 'email': address}
    # Preserve legacy email recovery behavior unless a contact-bound mapping exists.
    if bind_contact:
        if row is None:
            row = ApplicationSetting(key=key, values=values); db.add(row)
        else:
            row.values = values
        row.updated_at = datetime.now(timezone.utc)
    elif row is not None:
        db.delete(row)
    account.recovery_email_confirmed = True
    invalidate_credentials(db, account.id)
    log_audit(db, actor_principal_id=actor.id, action='EMPLOYEE_RECOVERY_ADDRESS_CONFIRMED',
        session_id=None, ip=None, metadata={'principal_id': account.id, 'employee_id': employee.id,
                                         'employee_contact_bound': bind_contact})
    db.flush()
    return address


def invalidate_employee_contact_recovery(db, *, employee):
    """A changed contact never silently inherits a prior ownership confirmation."""
    from sqlalchemy import select
    from app.models import Principal
    from app.services.password_reset_service import invalidate_credentials
    if employee.principal_id and db.get(ApplicationSetting, f'employee.recovery.{employee.principal_id}'):
        account = db.scalar(select(Principal).where(Principal.id == employee.principal_id)
                            .with_for_update().execution_options(populate_existing=True))
        account.recovery_email_confirmed = False
        invalidate_credentials(db, account.id)
