from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import settings
from app.models import DailyStoreLog, DailyStoreLogAction, Principal as PrincipalModel, Store
from app.v2.audit import V2AuditEvent, write_v2_audit_event
from app.v2.store_scope import ScopedStore


FEATURE_KEY = 'daily_store_logs_v2'
PORTAL_TIMEZONE = ZoneInfo('America/Los_Angeles')
TOKEN_MAX_AGE = timedelta(hours=4)
SUBMISSION_INTENT = 'v2_daily_store_log_submission'
ACTION_TYPES = frozenset({'ACKNOWLEDGED', 'MARKED_FOLLOW_UP', 'RESOLVED'})
LIFECYCLE_STATUSES = frozenset({'SUBMITTED', 'ACKNOWLEDGED', 'RESOLVED'})
TEXT_FIELDS = (
    'general_summary',
    'customer_incidents',
    'inventory_concerns',
    'facility_equipment_issues',
    'staffing_coverage_notes',
    'follow_up_items',
)
ISSUE_FIELDS = (
    'customer_incidents',
    'inventory_concerns',
    'facility_equipment_issues',
    'follow_up_items',
)
MAX_TEXT_LENGTH = 4000
MIN_SUBSTANTIVE_CHARACTERS = 10


class DailyLogValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str]):
        super().__init__('Check the highlighted Daily Store Log fields.')
        self.field_errors = field_errors


class DailyLogConflict(RuntimeError):
    def __init__(self, *, store_id: int, log_date: date, own_record_id: int | None):
        super().__init__('A Daily Store Log already exists for the selected store and date.')
        self.store_id = store_id
        self.log_date = log_date
        self.own_record_id = own_record_id


@dataclass(frozen=True)
class DailyLogInput:
    general_summary: str = ''
    customer_incidents: str = ''
    inventory_concerns: str = ''
    facility_equipment_issues: str = ''
    staffing_coverage_notes: str = ''
    follow_up_items: str = ''
    no_issues_reported: bool = False
    follow_up_required: bool = False


@dataclass(frozen=True)
class SubmissionOutcome:
    record_id: int
    correlation_id: str
    duplicate: bool


@dataclass(frozen=True)
class ActionOutcome:
    action_id: int
    record_id: int
    correlation_id: str
    duplicate: bool


@dataclass(frozen=True)
class HistoryFilters:
    store_ids: tuple[int, ...]
    from_date: date | None = None
    to_date: date | None = None
    actor: str = ''
    lifecycle_status: str = ''
    follow_up_required: bool | None = None
    search: str = ''
    page: int = 1
    page_size: int = 50


def portal_today(now: datetime | None = None) -> date:
    current = now or datetime.now(tz=timezone.utc)
    return current.astimezone(PORTAL_TIMEZONE).date()


def list_submission_stores(db: Session) -> list[ScopedStore]:
    rows = db.execute(
        select(Store.id, Store.name)
        .where(Store.active.is_(True))
        .order_by(Store.name.asc(), Store.id.asc())
    ).all()
    return [ScopedStore(id=int(row.id), name=str(row.name)) for row in rows]


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _issue_token(*, principal_id: int, intent: str, now: datetime | None = None) -> str:
    issued_at = int((now or datetime.now(tz=timezone.utc)).timestamp())
    payload = json.dumps(
        {
            'v': 1,
            'principal_id': principal_id,
            'intent': intent,
            'issued_at': issued_at,
            'nonce': secrets.token_urlsafe(24),
        },
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    encoded = _b64encode(payload)
    signature = hmac.new(
        settings.app_secret_key.encode('utf-8'),
        encoded.encode('ascii'),
        hashlib.sha256,
    ).digest()
    return f'{encoded}.{_b64encode(signature)}'


def _verify_token(
    token: str,
    *,
    principal_id: int,
    intent: str,
    now: datetime | None = None,
) -> str:
    try:
        encoded, signature = token.split('.', 1)
        expected = hmac.new(
            settings.app_secret_key.encode('utf-8'),
            encoded.encode('ascii'),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64decode(signature), expected):
            raise ValueError
        payload = json.loads(_b64decode(encoded))
        issued_at = datetime.fromtimestamp(int(payload['issued_at']), tz=timezone.utc)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError('This form token is invalid. Refresh the page and try again.') from exc
    if (
        payload.get('v') != 1
        or payload.get('principal_id') != principal_id
        or payload.get('intent') != intent
    ):
        raise ValueError('This form token does not belong to this employee or action.')
    current = now or datetime.now(tz=timezone.utc)
    if issued_at > current + timedelta(minutes=5) or current - issued_at > TOKEN_MAX_AGE:
        raise ValueError('This form has expired. Refresh the page and try again.')
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def issue_submission_token(*, principal_id: int) -> str:
    return _issue_token(principal_id=principal_id, intent=SUBMISSION_INTENT)


def issue_action_token(*, principal_id: int, record_id: int, action_type: str) -> str:
    clean_action = action_type.strip().upper()
    if clean_action not in ACTION_TYPES:
        raise ValueError('Unsupported Daily Store Log action')
    return _issue_token(
        principal_id=principal_id,
        intent=f'v2_daily_store_log_action:{record_id}:{clean_action}',
    )


def _advisory_key(value: str) -> int:
    raw = int(hashlib.sha256(value.encode('utf-8')).hexdigest()[:16], 16)
    return raw - (1 << 64) if raw >= (1 << 63) else raw


def _lock(db: Session, value: str) -> None:
    db.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'), {'lock_key': _advisory_key(value)})


def _clean_text(value: str) -> str | None:
    clean = str(value or '').strip()
    return clean or None


def _substantive_length(values: list[str | None]) -> int:
    return sum(len(''.join(str(value or '').split())) for value in values)


def validate_daily_log_input(
    db: Session,
    *,
    store_id: int,
    values: DailyLogInput,
) -> DailyLogInput:
    errors: dict[str, str] = {}
    store = db.execute(
        select(Store.id).where(Store.id == store_id, Store.active.is_(True))
    ).scalar_one_or_none()
    if store is None:
        errors['submission'] = 'Your current store is no longer active. Choose a current store and try again.'

    cleaned = {field: _clean_text(getattr(values, field)) for field in TEXT_FIELDS}
    for field, value in cleaned.items():
        if value and len(value) > MAX_TEXT_LENGTH:
            errors[field] = f'Keep this section to {MAX_TEXT_LENGTH:,} characters or fewer.'

    if values.no_issues_reported:
        if values.follow_up_required:
            errors['follow_up_required'] = 'A no-issues log cannot require follow-up.'
        if any(cleaned[field] for field in ISSUE_FIELDS):
            errors['no_issues_reported'] = (
                'Remove incident, concern, issue, and follow-up content or clear “No issues to report.”'
            )
    elif _substantive_length(list(cleaned.values())) < MIN_SUBSTANTIVE_CHARACTERS:
        errors['general_summary'] = (
            'Enter substantive operational content or confirm “No issues to report.”'
        )

    if values.follow_up_required and _substantive_length([cleaned['follow_up_items']]) < MIN_SUBSTANTIVE_CHARACTERS:
        errors['follow_up_items'] = 'Describe the items that require follow-up.'

    if errors:
        raise DailyLogValidationError(errors)

    return DailyLogInput(
        general_summary=cleaned['general_summary'] or '',
        customer_incidents=cleaned['customer_incidents'] or '',
        inventory_concerns=cleaned['inventory_concerns'] or '',
        facility_equipment_issues=cleaned['facility_equipment_issues'] or '',
        staffing_coverage_notes=cleaned['staffing_coverage_notes'] or '',
        follow_up_items=cleaned['follow_up_items'] or '',
        no_issues_reported=values.no_issues_reported,
        follow_up_required=values.follow_up_required,
    )


def submit_daily_log(
    db: Session,
    *,
    principal: Principal,
    submission_token: str,
    current_store_id: int,
    values: DailyLogInput,
    ip: str | None,
    now: datetime | None = None,
) -> SubmissionOutcome:
    fingerprint = _verify_token(
        submission_token,
        principal_id=principal.id,
        intent=SUBMISSION_INTENT,
    )
    _lock(db, f'daily-log-token:{fingerprint}')
    existing_token = db.execute(
        select(DailyStoreLog.id).where(
            DailyStoreLog.submission_fingerprint == fingerprint,
            DailyStoreLog.submitted_by_principal_id == principal.id,
        )
    ).scalar_one_or_none()
    if existing_token is not None:
        return SubmissionOutcome(int(existing_token), '', True)

    submitted_at = now or datetime.now(tz=timezone.utc)
    log_date = portal_today(submitted_at)
    normalized = validate_daily_log_input(db, store_id=current_store_id, values=values)
    _lock(db, f'daily-log-business:{current_store_id}:{log_date.isoformat()}')
    existing = db.execute(
        select(DailyStoreLog.id, DailyStoreLog.submitted_by_principal_id).where(
            DailyStoreLog.store_id == current_store_id,
            DailyStoreLog.log_date == log_date,
        )
    ).one_or_none()
    if existing is not None:
        raise DailyLogConflict(
            store_id=current_store_id,
            log_date=log_date,
            own_record_id=int(existing.id) if int(existing.submitted_by_principal_id) == principal.id else None,
        )

    row = DailyStoreLog(
        store_id=current_store_id,
        log_date=log_date,
        general_summary=_clean_text(normalized.general_summary),
        customer_incidents=_clean_text(normalized.customer_incidents),
        inventory_concerns=_clean_text(normalized.inventory_concerns),
        facility_equipment_issues=_clean_text(normalized.facility_equipment_issues),
        staffing_coverage_notes=_clean_text(normalized.staffing_coverage_notes),
        follow_up_items=_clean_text(normalized.follow_up_items),
        no_issues_reported=normalized.no_issues_reported,
        follow_up_required=normalized.follow_up_required,
        lifecycle_status='SUBMITTED',
        submitted_by_principal_id=principal.id,
        submission_fingerprint=fingerprint,
        store_selection_source='CURRENT_STORE',
        store_confirmed_at=submitted_at,
        submitted_at=submitted_at,
        created_at=submitted_at,
        updated_at=submitted_at,
    )
    db.add(row)
    db.flush()
    correlation_id = str(uuid.uuid4())
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='DAILY_LOG_SUBMITTED',
            domain='STORE_OPERATIONS',
            entity_type='daily_store_log',
            entity_id=row.id,
            store_ids=(row.store_id,),
            timestamp=submitted_at,
            correlation_id=correlation_id,
            metadata={
                'business_date': row.log_date.isoformat(),
                'store_selection_source': row.store_selection_source,
                'current_store_revalidated': True,
                'follow_up_required': row.follow_up_required,
                'submission_fingerprint': fingerprint,
            },
        ),
        ip=ip,
    )
    return SubmissionOutcome(int(row.id), correlation_id, False)


def get_own_receipt(db: Session, *, record_id: int, principal_id: int) -> dict | None:
    row = db.execute(
        select(
            DailyStoreLog.id,
            DailyStoreLog.log_date,
            DailyStoreLog.submitted_at,
            Store.name.label('store_name'),
        )
        .join(Store, Store.id == DailyStoreLog.store_id)
        .where(
            DailyStoreLog.id == record_id,
            DailyStoreLog.submitted_by_principal_id == principal_id,
        )
    ).one_or_none()
    if row is None:
        return None
    return {
        'id': int(row.id),
        'log_date': row.log_date,
        'submitted_at': row.submitted_at,
        'store_name': str(row.store_name),
    }


def list_daily_logs(db: Session, *, filters: HistoryFilters) -> tuple[list[dict], int]:
    conditions = [DailyStoreLog.store_id.in_(filters.store_ids)]
    if filters.from_date:
        conditions.append(DailyStoreLog.log_date >= filters.from_date)
    if filters.to_date:
        conditions.append(DailyStoreLog.log_date <= filters.to_date)
    if filters.actor:
        conditions.append(PrincipalModel.username.ilike(f'%{filters.actor.strip()}%'))
    if filters.lifecycle_status:
        conditions.append(DailyStoreLog.lifecycle_status == filters.lifecycle_status)
    if filters.follow_up_required is not None:
        conditions.append(DailyStoreLog.follow_up_required.is_(filters.follow_up_required))
    if filters.search:
        pattern = f'%{filters.search.strip()}%'
        action_match = select(DailyStoreLogAction.daily_store_log_id).where(
            DailyStoreLogAction.response_note.ilike(pattern)
        )
        conditions.append(
            or_(
                DailyStoreLog.general_summary.ilike(pattern),
                DailyStoreLog.customer_incidents.ilike(pattern),
                DailyStoreLog.inventory_concerns.ilike(pattern),
                DailyStoreLog.facility_equipment_issues.ilike(pattern),
                DailyStoreLog.staffing_coverage_notes.ilike(pattern),
                DailyStoreLog.follow_up_items.ilike(pattern),
                DailyStoreLog.id.in_(action_match),
            )
        )

    where_clause = and_(*conditions)
    total = int(
        db.scalar(
            select(func.count(DailyStoreLog.id))
            .join(PrincipalModel, PrincipalModel.id == DailyStoreLog.submitted_by_principal_id)
            .where(where_clause)
        )
        or 0
    )
    page = max(1, filters.page)
    page_size = min(max(1, filters.page_size), 100)
    rows = db.execute(
        select(
            DailyStoreLog.id,
            DailyStoreLog.log_date,
            DailyStoreLog.lifecycle_status,
            DailyStoreLog.follow_up_required,
            DailyStoreLog.no_issues_reported,
            DailyStoreLog.submitted_at,
            Store.id.label('store_id'),
            Store.name.label('store_name'),
            PrincipalModel.username.label('actor_username'),
        )
        .join(Store, Store.id == DailyStoreLog.store_id)
        .join(PrincipalModel, PrincipalModel.id == DailyStoreLog.submitted_by_principal_id)
        .where(where_clause)
        .order_by(DailyStoreLog.log_date.desc(), DailyStoreLog.submitted_at.desc(), DailyStoreLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return (
        [
            {
                'id': int(row.id),
                'log_date': row.log_date,
                'lifecycle_status': str(row.lifecycle_status),
                'follow_up_required': bool(row.follow_up_required),
                'no_issues_reported': bool(row.no_issues_reported),
                'submitted_at': row.submitted_at,
                'store_id': int(row.store_id),
                'store_name': str(row.store_name),
                'actor_username': str(row.actor_username),
            }
            for row in rows
        ],
        total,
    )


def get_daily_log_detail(db: Session, *, record_id: int) -> dict | None:
    row = db.execute(
        select(
            DailyStoreLog,
            Store.name.label('store_name'),
            PrincipalModel.username.label('actor_username'),
        )
        .join(Store, Store.id == DailyStoreLog.store_id)
        .join(PrincipalModel, PrincipalModel.id == DailyStoreLog.submitted_by_principal_id)
        .where(DailyStoreLog.id == record_id)
    ).one_or_none()
    if row is None:
        return None
    log, store_name, actor_username = row
    action_actor = PrincipalModel
    actions = db.execute(
        select(
            DailyStoreLogAction,
            action_actor.username.label('actor_username'),
        )
        .join(action_actor, action_actor.id == DailyStoreLogAction.actor_principal_id)
        .where(DailyStoreLogAction.daily_store_log_id == record_id)
        .order_by(DailyStoreLogAction.created_at.asc(), DailyStoreLogAction.id.asc())
    ).all()
    return {
        'id': int(log.id),
        'store_id': int(log.store_id),
        'store_name': str(store_name),
        'log_date': log.log_date,
        'general_summary': log.general_summary,
        'customer_incidents': log.customer_incidents,
        'inventory_concerns': log.inventory_concerns,
        'facility_equipment_issues': log.facility_equipment_issues,
        'staffing_coverage_notes': log.staffing_coverage_notes,
        'follow_up_items': log.follow_up_items,
        'no_issues_reported': bool(log.no_issues_reported),
        'follow_up_required': bool(log.follow_up_required),
        'lifecycle_status': str(log.lifecycle_status),
        'submitted_by_principal_id': int(log.submitted_by_principal_id),
        'actor_username': str(actor_username),
        'store_selection_source': str(log.store_selection_source),
        'store_confirmed_at': log.store_confirmed_at,
        'submitted_at': log.submitted_at,
        'created_at': log.created_at,
        'updated_at': log.updated_at,
        'actions': [
            {
                'id': int(action.id),
                'action_type': str(action.action_type),
                'from_status': str(action.from_status),
                'to_status': str(action.to_status),
                'follow_up_required_after': bool(action.follow_up_required_after),
                'response_note': action.response_note,
                'actor_principal_id': int(action.actor_principal_id),
                'actor_username': str(actor_name),
                'created_at': action.created_at,
            }
            for action, actor_name in actions
        ],
    }


def perform_management_action(
    db: Session,
    *,
    principal: Principal,
    record_id: int,
    action_type: str,
    action_token: str,
    response_note: str,
    authorized_store_ids: tuple[int, ...],
    ip: str | None,
) -> ActionOutcome:
    clean_action = action_type.strip().upper()
    if clean_action not in ACTION_TYPES:
        raise ValueError('Unsupported Daily Store Log action.')
    intent = f'v2_daily_store_log_action:{record_id}:{clean_action}'
    fingerprint = _verify_token(
        action_token,
        principal_id=principal.id,
        intent=intent,
    )
    _lock(db, f'daily-log-action-token:{fingerprint}')
    existing_action = db.execute(
        select(DailyStoreLogAction.id).where(
            DailyStoreLogAction.action_fingerprint == fingerprint,
            DailyStoreLogAction.actor_principal_id == principal.id,
        )
    ).scalar_one_or_none()
    if existing_action is not None:
        return ActionOutcome(int(existing_action), record_id, '', True)

    log = db.execute(
        select(DailyStoreLog)
        .where(
            DailyStoreLog.id == record_id,
            DailyStoreLog.store_id.in_(authorized_store_ids),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if log is None:
        raise PermissionError('Daily Store Log not found.')

    note = _clean_text(response_note)
    if note and len(note) > MAX_TEXT_LENGTH:
        raise ValueError(f'Keep the management note to {MAX_TEXT_LENGTH:,} characters or fewer.')
    from_status = str(log.lifecycle_status)
    before_follow_up = bool(log.follow_up_required)
    if clean_action == 'ACKNOWLEDGED':
        if from_status != 'SUBMITTED':
            raise ValueError('Only a submitted Daily Store Log can be acknowledged.')
        to_status = 'ACKNOWLEDGED'
        follow_up_after = bool(log.follow_up_required)
    elif clean_action == 'MARKED_FOLLOW_UP':
        if from_status == 'RESOLVED':
            raise ValueError('Resolved Daily Store Logs cannot be reopened in this milestone.')
        if log.follow_up_required:
            raise ValueError('This Daily Store Log already requires follow-up.')
        if not note:
            raise ValueError('A management note is required when marking follow-up.')
        to_status = from_status
        follow_up_after = True
    else:
        if from_status == 'RESOLVED':
            raise ValueError('This Daily Store Log is already resolved.')
        if not log.follow_up_required:
            raise ValueError('Mark the Daily Store Log for follow-up before resolving it.')
        if not note:
            raise ValueError('A resolution note is required.')
        to_status = 'RESOLVED'
        follow_up_after = False

    now = datetime.now(tz=timezone.utc)
    action = DailyStoreLogAction(
        daily_store_log_id=log.id,
        action_type=clean_action,
        from_status=from_status,
        to_status=to_status,
        follow_up_required_after=follow_up_after,
        response_note=note,
        actor_principal_id=principal.id,
        action_fingerprint=fingerprint,
        created_at=now,
    )
    db.add(action)
    log.lifecycle_status = to_status
    log.follow_up_required = follow_up_after
    log.updated_at = now
    db.flush()
    correlation_id = str(uuid.uuid4())
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action=f'DAILY_LOG_{clean_action}',
            domain='STORE_OPERATIONS',
            entity_type='daily_store_log',
            entity_id=log.id,
            store_ids=(int(log.store_id),),
            timestamp=now,
            correlation_id=correlation_id,
            before={
                'lifecycle_status': from_status,
                'follow_up_required': before_follow_up,
            },
            after={
                'lifecycle_status': to_status,
                'follow_up_required': follow_up_after,
            },
            metadata={
                'action_fingerprint': fingerprint,
                'management_note_recorded': bool(note),
            },
        ),
        ip=ip,
    )
    return ActionOutcome(int(action.id), int(log.id), correlation_id, False)
