from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from sqlalchemy import select, update, delete
from sqlalchemy.dialects.postgresql import insert
from app.models import Principal, Employee, PasswordResetToken, WebSession, AuthThrottle
from app.security.passwords import hash_password
from app.services.audit_service import log_audit
from app.services.transactional_email_service import send_password_email, EmailResult


def now():
    return datetime.now(timezone.utc)


def throttle(db, key, *, limit=3, seconds=3600):
    """Bounded fixed window, serialized across processes; never stores raw email/IP."""
    digest = hashlib.sha256(key.encode()).hexdigest()
    db.execute(delete(AuthThrottle).where(AuthThrottle.window_start < now() - timedelta(days=1)))
    db.execute(insert(AuthThrottle).values(key=digest, window_start=now(), attempts=0).on_conflict_do_nothing())
    row = db.execute(select(AuthThrottle).where(AuthThrottle.key == digest).with_for_update()).scalar_one()
    if row.window_start + timedelta(seconds=seconds) <= now():
        row.window_start, row.attempts = now(), 0
    if row.attempts >= limit:
        return False
    row.attempts += 1
    db.flush()
    return True


def eligible(db, account):
    if not account or not account.active or not account.recovery_email_confirmed:
        return None
    return db.execute(select(Employee).where(Employee.principal_id == account.id, Employee.active.is_(True))).scalar_one_or_none()


def invalidate_credentials(db, principal_id):
    instant = now()
    db.execute(update(PasswordResetToken).where(PasswordResetToken.principal_id == principal_id,
        PasswordResetToken.consumed_at.is_(None), PasswordResetToken.revoked_at.is_(None)).values(revoked_at=instant))
    db.execute(update(WebSession).where(WebSession.principal_id == principal_id,
        WebSession.revoked_at.is_(None)).values(revoked_at=instant))


def issue(db, principal_id):
    account = db.execute(select(Principal).where(Principal.id == principal_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    employee = eligible(db, account)
    if not employee:
        raise ValueError('Account needs an active employee and an explicitly confirmed email login.')
    db.execute(update(PasswordResetToken).where(PasswordResetToken.principal_id == principal_id,
        PasswordResetToken.consumed_at.is_(None), PasswordResetToken.revoked_at.is_(None)).values(revoked_at=now()))
    raw = secrets.token_urlsafe(32)
    row = PasswordResetToken(principal_id=principal_id, employee_id=employee.id,
        login_email=account.username, digest=hashlib.sha256(raw.encode()).hexdigest(),
        created_at=now(), expires_at=now() + timedelta(hours=1))
    db.add(row); db.flush()
    return row, raw, not bool(account.password_hash)


def request_password_email(db, *, principal_id, actor_id=None):
    if not throttle(db, f'password-send:{principal_id}'):
        db.commit()
        return EmailResult(False, 'Email rate limit reached. Please try again later.')
    try:
        row, raw, setup = issue(db, principal_id)
    except ValueError as exc:
        log_audit(db, actor_principal_id=actor_id, action='EMPLOYEE_PASSWORD_EMAIL_INELIGIBLE',
            session_id=None, ip=None, metadata={'principal_id': principal_id})
        db.commit()
        return EmailResult(False, str(exc))
    # Commit before sending: no employee ever receives a token rolled back with
    # account creation. Provider failure leaves a valid account ready for resend.
    recipient, token_id = row.login_email, row.id
    db.commit()
    result = send_password_email(db, recipient=recipient, token=raw, setup=setup, token_id=token_id)
    log_audit(db, actor_principal_id=actor_id, action='EMPLOYEE_PASSWORD_EMAIL', session_id=None, ip=None,
        metadata={'principal_id': principal_id, 'accepted': result.accepted, 'purpose': 'setup' if setup else 'reset'})
    db.commit()
    return result


def redeem(db, raw, password, confirmation):
    if password != confirmation or not 12 <= len(password) <= 1024:
        raise ValueError('Passwords must match and contain 12 to 1024 characters.')
    if not raw or len(raw) > 128:
        raise ValueError('This password link is invalid, expired or already used.')
    digest = hashlib.sha256(raw.encode()).hexdigest()
    principal_id = db.scalar(select(PasswordResetToken.principal_id).where(PasswordResetToken.digest == digest))
    # Principal first everywhere: serializes replacement, redemption and login.
    account = db.execute(select(Principal).where(Principal.id == principal_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    row = db.execute(select(PasswordResetToken).where(PasswordResetToken.digest == digest).execution_options(populate_existing=True)).scalar_one_or_none()
    employee = eligible(db, account)
    if (not row or not employee or row.employee_id != employee.id or row.login_email != account.username
            or row.consumed_at or row.revoked_at or row.expires_at <= now()):
        raise ValueError('This password link is invalid, expired or already used.')
    account.password_hash = hash_password(password)
    row.consumed_at = now()
    db.flush()
    invalidate_credentials(db, account.id)
    log_audit(db, actor_principal_id=account.id, action='EMPLOYEE_PASSWORD_ESTABLISHED', session_id=None, ip=None, metadata={})
    db.flush()
