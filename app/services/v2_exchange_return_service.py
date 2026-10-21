from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import settings
from app.models import AuditLog, ExchangeReturnForm, Principal as PrincipalModel, Store
from app.services.exchange_return_form_service import create_exchange_return_form
from app.v2.audit import V2AuditEvent, write_v2_audit_event


FEATURE_KEY = 'exchanges_returns_v2'
TOKEN_INTENT = 'v2_exchange_return_submission'
TOKEN_MAX_AGE = timedelta(hours=4)
PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')


@dataclass(frozen=True)
class ExchangeReturnInput:
    original_purchase_date: date
    original_ticket_number: str
    exchange_ticket_number: str
    items_text: str
    reason_text: str
    refund_given: bool
    refund_approved_by: str


@dataclass(frozen=True)
class SubmissionOutcome:
    record_id: int
    correlation_id: str
    duplicate: bool


@dataclass(frozen=True)
class HistoryFilters:
    store_ids: tuple[int, ...]
    from_date: date | None = None
    to_date: date | None = None
    actor: str = ''
    search: str = ''
    refund_given: bool | None = None


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def issue_submission_token(*, principal_id: int, now: datetime | None = None) -> str:
    issued_at = int((now or datetime.now(tz=timezone.utc)).timestamp())
    payload = json.dumps(
        {'v': 1, 'principal_id': principal_id, 'intent': TOKEN_INTENT, 'issued_at': issued_at, 'nonce': secrets.token_urlsafe(24)},
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    encoded = _b64encode(payload)
    signature = hmac.new(settings.app_secret_key.encode('utf-8'), encoded.encode('ascii'), hashlib.sha256).digest()
    return f'{encoded}.{_b64encode(signature)}'


def verify_submission_token(
    token: str,
    *,
    principal_id: int,
    now: datetime | None = None,
) -> str:
    try:
        encoded, signature = token.split('.', 1)
        expected = hmac.new(settings.app_secret_key.encode('utf-8'), encoded.encode('ascii'), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise ValueError
        payload = json.loads(_b64decode(encoded))
        issued_at = datetime.fromtimestamp(int(payload['issued_at']), tz=timezone.utc)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('This form submission token is invalid. Refresh the form and try again.') from exc
    if payload.get('v') != 1 or payload.get('intent') != TOKEN_INTENT or payload.get('principal_id') != principal_id:
        raise ValueError('This form submission token does not belong to this employee or form.')
    current = now or datetime.now(tz=timezone.utc)
    if issued_at > current + timedelta(minutes=5) or current - issued_at > TOKEN_MAX_AGE:
        raise ValueError('This form has expired. Refresh it and enter the submission again.')
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _advisory_lock_key(token_id: str) -> int:
    value = int(token_id[:16], 16)
    return value - (1 << 64) if value >= (1 << 63) else value


def _existing_submission(db: Session, *, principal_id: int, token_id: str) -> tuple[int, str] | None:
    metadata = db.execute(
        select(AuditLog.meta).where(
            AuditLog.actor_principal_id == principal_id,
            AuditLog.action == 'V2:CUSTOMER_FORMS:SUBMITTED',
            AuditLog.meta['metadata']['submission_fingerprint'].as_string() == token_id,
        )
    ).scalar_one_or_none()
    if not metadata:
        return None
    try:
        return int(metadata['entity_id']), str(metadata['correlation_id'])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError('Existing submission evidence is incomplete') from exc


def submit_exchange_return(
    db: Session,
    *,
    principal: Principal,
    submission_token: str,
    values: ExchangeReturnInput,
    ip: str | None,
) -> SubmissionOutcome:
    if principal.store_id is None:
        raise PermissionError('Your employee account does not have an assigned store.')
    token_id = verify_submission_token(submission_token, principal_id=principal.id)
    db.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'), {'lock_key': _advisory_lock_key(token_id)})
    existing = _existing_submission(db, principal_id=principal.id, token_id=token_id)
    if existing:
        record_id, correlation_id = existing
        return SubmissionOutcome(record_id, correlation_id, True)

    server_timestamp = datetime.now(tz=timezone.utc)
    form = create_exchange_return_form(
        db,
        store_id=principal.store_id,
        principal_id=principal.id,
        employee_name=principal.username,
        original_purchase_date=values.original_purchase_date,
        generated_at=server_timestamp,
        original_ticket_number=values.original_ticket_number,
        exchange_ticket_number=values.exchange_ticket_number,
        items_text=values.items_text,
        reason_text=values.reason_text,
        refund_given=values.refund_given,
        refund_approved_by=values.refund_approved_by,
    )
    correlation_id = str(uuid.uuid4())
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='SUBMITTED',
            domain='CUSTOMER_FORMS',
            entity_type='exchange_return_form',
            entity_id=form.id,
            store_ids=(principal.store_id,),
            timestamp=server_timestamp,
            correlation_id=correlation_id,
            metadata={'refund_given': values.refund_given, 'submission_fingerprint': token_id},
        ),
        ip=ip,
    )
    return SubmissionOutcome(int(form.id), correlation_id, False)


def _date_boundary(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=PORTAL_TIMEZONE).astimezone(timezone.utc)


def list_exchange_returns(db: Session, *, filters: HistoryFilters) -> list[dict]:
    conditions = [ExchangeReturnForm.store_id.in_(filters.store_ids)]
    if filters.from_date:
        conditions.append(ExchangeReturnForm.created_at >= _date_boundary(filters.from_date))
    if filters.to_date:
        conditions.append(ExchangeReturnForm.created_at < _date_boundary(filters.to_date + timedelta(days=1)))
    if filters.refund_given is not None:
        conditions.append(ExchangeReturnForm.refund_given.is_(filters.refund_given))
    if filters.actor:
        conditions.append(PrincipalModel.username.ilike(f'%{filters.actor.strip()}%'))
    if filters.search:
        pattern = f'%{filters.search.strip()}%'
        conditions.append(
            or_(
                ExchangeReturnForm.employee_name.ilike(pattern),
                PrincipalModel.username.ilike(pattern),
                ExchangeReturnForm.original_ticket_number.ilike(pattern),
                ExchangeReturnForm.exchange_ticket_number.ilike(pattern),
                ExchangeReturnForm.items_text.ilike(pattern),
                ExchangeReturnForm.reason_text.ilike(pattern),
                ExchangeReturnForm.refund_approved_by.ilike(pattern),
            )
        )
    rows = db.execute(
        select(
            ExchangeReturnForm.id,
            ExchangeReturnForm.created_at,
            ExchangeReturnForm.employee_name,
            ExchangeReturnForm.original_ticket_number,
            ExchangeReturnForm.exchange_ticket_number,
            ExchangeReturnForm.refund_given,
            Store.id.label('store_id'),
            Store.name.label('store_name'),
            PrincipalModel.username.label('actor_username'),
            PrincipalModel.custom_role_label.label('actor_label'),
        )
        .join(Store, Store.id == ExchangeReturnForm.store_id)
        .join(PrincipalModel, PrincipalModel.id == ExchangeReturnForm.created_by_principal_id)
        .where(and_(*conditions))
        .order_by(ExchangeReturnForm.created_at.desc(), ExchangeReturnForm.id.desc())
    ).all()
    return [
        {
            'id': int(row.id),
            'created_at': row.created_at,
            'employee_name': str(row.employee_name),
            'original_ticket_number': str(row.original_ticket_number),
            'exchange_ticket_number': str(row.exchange_ticket_number),
            'refund_given': bool(row.refund_given),
            'store_id': int(row.store_id),
            'store_name': str(row.store_name),
            'actor_username': str(row.actor_username),
            'legacy_shared_actor': 'legacy' in str(row.actor_label or '').lower() and 'shared' in str(row.actor_label or '').lower(),
        }
        for row in rows
    ]


def get_exchange_return_detail(db: Session, *, record_id: int, authorized_store_ids: tuple[int, ...]) -> dict | None:
    row = db.execute(
        select(
            ExchangeReturnForm,
            Store.name.label('store_name'),
            PrincipalModel.username.label('actor_username'),
            PrincipalModel.custom_role_label.label('actor_label'),
        )
        .join(Store, Store.id == ExchangeReturnForm.store_id)
        .join(PrincipalModel, PrincipalModel.id == ExchangeReturnForm.created_by_principal_id)
        .where(
            ExchangeReturnForm.id == record_id,
            ExchangeReturnForm.store_id.in_(authorized_store_ids),
        )
    ).one_or_none()
    if not row:
        return None
    form, store_name, actor_username, actor_label = row
    audit_rows = db.execute(
        select(AuditLog.action, AuditLog.created_at, PrincipalModel.username.label('actor_username'))
        .outerjoin(PrincipalModel, PrincipalModel.id == AuditLog.actor_principal_id)
        .where(
            or_(
                AuditLog.meta['exchange_return_form_id'].as_string() == str(record_id),
                and_(
                    AuditLog.meta['entity_type'].as_string() == 'exchange_return_form',
                    AuditLog.meta['entity_id'].as_string() == str(record_id),
                ),
            )
        )
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    ).all()
    return {
        'id': int(form.id),
        'store_id': int(form.store_id),
        'store_name': str(store_name),
        'actor_principal_id': int(form.created_by_principal_id),
        'actor_username': str(actor_username),
        'legacy_shared_actor': 'legacy' in str(actor_label or '').lower() and 'shared' in str(actor_label or '').lower(),
        'employee_name': str(form.employee_name),
        'original_purchase_date': form.original_purchase_date,
        'generated_at': form.generated_at,
        'created_at': form.created_at,
        'original_ticket_number': str(form.original_ticket_number),
        'exchange_ticket_number': str(form.exchange_ticket_number),
        'items_text': str(form.items_text),
        'reason_text': str(form.reason_text),
        'refund_given': bool(form.refund_given),
        'refund_approved_by': str(form.refund_approved_by),
        'audit_history': [
            {'action': str(item.action), 'created_at': item.created_at, 'actor_username': item.actor_username}
            for item in audit_rows
        ],
    }
