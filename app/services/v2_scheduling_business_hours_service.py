"""Admin entry for dated business hours; existing schedules remain evidence."""
from datetime import date, datetime, timezone
import uuid
from sqlalchemy import select
from app.auth import Role
from app.models import StoreSpecialHour, SchedulePeriod, SchedulePeriodStatus, ScheduleShift
from app.services.access_control_service import principal_has_permission
from app.services.v2_scheduling_service import SchedulingValidationError, SchedulingConflict, lock_scheduling
from app.services.v2_scheduling_rules_service import upsert_special_hour, _authorized_store, _audit
from app.services.v2_scheduling_coverage_service import rebuild_schedule_warnings
from app.services.v2_scheduling_opening_service import preparation_start, OpeningConfigurationError


def require_admin(db, principal):
    if (not principal.active or principal.role != Role.ADMIN or not principal_has_permission(
            db, principal=principal, permission_key='scheduling.manage_special_hours', fallback_allowed=True)):
        raise PermissionError('Business hours require Admin scheduling access.')


def _values(row):
    return dict(id=row.id, store_id=row.store_id, calendar_date=row.calendar_date.isoformat(),
                event_name=row.event_name, closed_all_day=row.closed_all_day,
                opening_time=row.opening_time.isoformat() if row.opening_time else None,
                closing_time=row.closing_time.isoformat() if row.closing_time else None,
                staffing_note=row.staffing_note, active=row.active)


def _refresh(db, day, principal):
    periods=list(db.scalars(select(SchedulePeriod).where(
        SchedulePeriod.week_start_date <= day, SchedulePeriod.week_end_date >= day,
        SchedulePeriod.is_historical.is_(False),
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT, SchedulePeriodStatus.PUBLISHED)),
    ).order_by(SchedulePeriod.id).with_for_update()))
    for period in periods:
        if period.status == SchedulePeriodStatus.DRAFT:
            # Preserve prior approval evidence, but require approval of the new
            # constraints. Never change assignments or published metadata.
            period.version += 1
            period.updated_by_principal_id=principal.id
            period.updated_at=datetime.now(timezone.utc)
        rebuild_schedule_warnings(db, schedule_period_id=period.id)
    db.flush()
    return [{'id':p.id,'status':p.status.value,'version':p.version} for p in periods]


def save_business_hours(db, *, principal, store_ids, allowed_store_ids, calendar_date,
                        event_name, closed_all_day, opening_time=None, closing_time=None,
                        staffing_note='', edit_id=None, expected_updated_at=None, ip=None):
    require_admin(db,principal)
    lock_scheduling(db)
    with db.begin_nested():
        ids=tuple(sorted(set(store_ids)))
        if not ids:raise SchedulingValidationError('Choose at least one store.')
        for sid in ids:_authorized_store(db,sid,allowed_store_ids)
        if not event_name.strip() or len(event_name.strip())>200:
            raise SchedulingValidationError('Enter an event name of 1–200 characters.')
        if len(staffing_note)>1000:raise SchedulingValidationError('Keep the note to 1,000 characters.')
        if not closed_all_day:
            if opening_time is None or closing_time is None or closing_time <= opening_time:
                raise SchedulingValidationError('Different hours require an opening time before the closing time.')
            try:preparation_start(calendar_date,opening_time)
            except OpeningConfigurationError as exc:raise SchedulingValidationError(str(exc)) from exc
        existing=list(db.scalars(select(StoreSpecialHour).where(
            StoreSpecialHour.store_id.in_(ids),StoreSpecialHour.calendar_date==calendar_date,
            StoreSpecialHour.active.is_(True)).with_for_update()))
        if edit_id:
            if (len(ids)!=1 or len(existing)!=1 or existing[0].id!=edit_id or
                    expected_updated_at is None or existing[0].updated_at!=expected_updated_at):
                raise SchedulingConflict('This exception changed. Reload it before saving.')
        elif existing:
            raise SchedulingConflict('An exception already exists for a selected store and date. Use Edit; no stores were changed.')
        before=[_values(r) for r in existing]
        batch=str(uuid.uuid4())
        rows=[upsert_special_hour(db,principal=principal,store_id=sid,calendar_date=calendar_date,
            event_name=event_name,closed_all_day=closed_all_day,opening_time=opening_time,
            closing_time=closing_time,staffing_note=staffing_note,allowed_store_ids=allowed_store_ids,
            batch_correlation_id=batch,ip=ip) for sid in ids]
        periods=_refresh(db,calendar_date,principal)
        _audit(db,principal=principal,action='BUSINESS_HOURS_SAVED',entity_type='store_special_hour',
            entity_id=rows[0].id,store_ids=ids,before={'exceptions':before},
            after={'exceptions':[_values(r) for r in rows]},
            metadata={'assignments_unchanged':True,'affected_periods':periods},
            correlation_id=batch,ip=ip)
        return rows


def restore_normal_hours(db, *, principal, exception_id, expected_updated_at, allowed_store_ids, ip=None):
    require_admin(db,principal)
    lock_scheduling(db)
    with db.begin_nested():
        row=db.scalar(select(StoreSpecialHour).where(StoreSpecialHour.id==exception_id).with_for_update())
        if row is None or not row.active:raise SchedulingConflict('Active exception not found.')
        _authorized_store(db,row.store_id,allowed_store_ids)
        if row.updated_at!=expected_updated_at:raise SchedulingConflict('This exception changed. Reload it before saving.')
        before=_values(row)
        row.active=False;row.updated_at=datetime.now(timezone.utc);row.updated_by_principal_id=principal.id
        db.flush();periods=_refresh(db,row.calendar_date,principal)
        _audit(db,principal=principal,action='BUSINESS_HOURS_RESTORED',entity_type='store_special_hour',
            entity_id=row.id,store_ids=(row.store_id,),before=before,after=_values(row),
            metadata={'assignments_unchanged':True,'affected_periods':periods},ip=ip)
        return row


def assignment_impacts(db, row):
    """Read-only preview of shifts requiring management review."""
    rows=db.execute(select(ScheduleShift,SchedulePeriod).join(SchedulePeriod,
        SchedulePeriod.id==ScheduleShift.schedule_period_id).where(
        ScheduleShift.store_id==row.store_id,ScheduleShift.shift_date==row.calendar_date,
        SchedulePeriod.is_historical.is_(False),
        SchedulePeriod.status.in_((SchedulePeriodStatus.DRAFT,SchedulePeriodStatus.PUBLISHED)),
    ).order_by(SchedulePeriod.id,ScheduleShift.id)).all()
    result=[]
    for shift,period in rows:
        if row.closed_all_day or shift.start_time!=preparation_start(row.calendar_date,row.opening_time) or shift.end_time>row.closing_time:
            result.append(dict(period_id=period.id,status=period.status.value,shift_id=shift.id))
    return result
