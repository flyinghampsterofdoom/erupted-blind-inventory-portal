"""Direct administrative company-wide Lead designation; no scheduling evaluation."""
from datetime import datetime, timezone
from sqlalchemy import select, text
from app import models as m
from app.services.scheduling_shift_lead_service import require_owner
from app.services.v2_scheduling_service import (
    SchedulingValidationError, SchedulingConflict, lock_scheduling, clone_published_revision,
)
from app.services.v2_scheduling_rules_service import _audit


def assign_daily_lead(db, *, principal, period_id, shift_id, expected_version,
                      assigned, allowed_store_ids, selected_store_ids=None):
    require_owner(db, principal)
    lock_scheduling(db, wait=False)
    db.execute(text("SET LOCAL lock_timeout = '2s'"))
    with db.begin_nested():
        period = db.scalar(select(m.SchedulePeriod).where(m.SchedulePeriod.id==period_id)
            .with_for_update().execution_options(populate_existing=True))
        if not period or period.version!=expected_version:
            raise SchedulingConflict('The schedule changed. Refresh before changing Lead.')
        if (period.is_historical or period.cleared_at is not None
                or period.status not in (m.SchedulePeriodStatus.DRAFT,m.SchedulePeriodStatus.PUBLISHED)):
            raise SchedulingValidationError('Choose a current editable schedule.')
        selected = db.scalar(select(m.ScheduleShift).where(
            m.ScheduleShift.id==shift_id,m.ScheduleShift.schedule_period_id==period.id)
            .with_for_update().execution_options(populate_existing=True))
        if not selected or not selected.employee_id or selected.is_training:
            raise SchedulingValidationError('Choose a scheduled employee outside training.')
        if (selected.store_id not in allowed_store_ids
                or selected_store_ids is not None and selected.store_id not in selected_store_ids):
            raise PermissionError('This assignment is outside your store scope.')
        from app.services.v2_scheduling_lead_duty_service import business_today
        if selected.shift_date<business_today(db):
            raise SchedulingValidationError('Use Attendance to correct historical Lead responsibility.')
        if assigned:
            from app.services.v2_scheduling_lifecycle_service import eligible_on
            person=db.scalar(select(m.Employee).where(m.Employee.id==selected.employee_id)
                .with_for_update().execution_options(populate_existing=True))
            store=db.get(m.Store,selected.store_id)
            if not person or not person.scheduling_lead_capable or not eligible_on(person,selected.shift_date):
                raise SchedulingValidationError('Choose a scheduled, eligible Lead-capable employee.')
            if not store or not store.active:
                raise SchedulingValidationError('Choose an active store.')
        revised=False
        if period.status==m.SchedulePeriodStatus.PUBLISHED:
            from app.services.access_control_service import principal_has_permission
            if not principal_has_permission(db,principal=principal,
                    permission_key='scheduling.modify_published',fallback_allowed=True):
                raise PermissionError('Published-schedule modification permission is required.')
            if db.scalar(select(m.SchedulePeriod.id).where(
                    m.SchedulePeriod.week_start_date==period.week_start_date,
                    m.SchedulePeriod.status==m.SchedulePeriodStatus.DRAFT,
                    m.SchedulePeriod.cleared_at.is_(None))):
                raise SchedulingConflict('Open the existing unpublished draft before changing Lead.')
            source_id=selected.id
            period=clone_published_revision(db,principal=principal,published_period_id=period.id,
                allowed_store_ids=allowed_store_ids,_refresh_warnings=False)
            selected=db.scalar(select(m.ScheduleShift).where(
                m.ScheduleShift.schedule_period_id==period.id,m.ScheduleShift.source_shift_id==source_id))
            revised=True
        # One company-wide Lead per date, including Leads at other stores.
        previous=db.scalar(select(m.ScheduleShift).where(
            m.ScheduleShift.schedule_period_id==period.id,
            m.ScheduleShift.shift_date==selected.shift_date,
            m.ScheduleShift.is_lead_of_day.is_(True)).with_for_update()
            .execution_options(populate_existing=True))
        if previous and previous.store_id not in allowed_store_ids:
            raise PermissionError('The existing Lead is outside your store scope.')
        changed=[]
        now=datetime.now(timezone.utc)
        def change(row,value):
            row.is_lead_of_day=value;row.lead_of_day_manually_assigned=value
            row.updated_by_principal_id=principal.id;row.updated_at=now
            changed.append(dict(id=row.id,is_lead_of_day=value,lead_of_day_manually_assigned=value))
        if assigned and previous is not None and previous.id!=selected.id:
            change(previous,False)
            db.flush()  # Satisfy the existing unique company-wide date constraint.
        if selected.is_lead_of_day!=assigned:
            change(selected,assigned)
        if changed:
            period.version+=1;period.updated_by_principal_id=principal.id;period.updated_at=now
            _audit(db,principal=principal,action='LEAD_OF_DAY_CHANGED',
                entity_type='schedule_shift',entity_id=selected.id,
                before={'shift_id':previous.id if previous else None},
                after={'shift_id':selected.id if assigned else (previous.id if previous and previous.id!=selected.id else None)},
                metadata={'schedule_period_id':period.id,'version':period.version,
                          'date':str(selected.shift_date),'direct_lead_assignment':True})
        db.flush()
        return dict(period_id=period.id,version=period.version,base_version=expected_version,
            shift_id=selected.id,date=str(selected.shift_date),store_id=selected.store_id,
            changes=changed,lead_gap=not assigned and (previous is None or previous.id==selected.id),
            revised=revised,week_start=str(period.week_start_date))
