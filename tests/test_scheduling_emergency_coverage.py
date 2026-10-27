"""Lead departure response, Owner payroll isolation and retained published evidence."""
from datetime import date, time, timedelta
from dataclasses import replace
import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services import v2_scheduling_service as svc
from app.services.scheduling_completion_service import approve_and_publish_schedule
from app.services.scheduling_emergency_coverage_service import preview_departure, prepare_departure
from app.services.employee_schedule_service import company_published_assignments
from app.services.v2_scheduling_policy_service import evaluate_assignment
from app.services.v2_scheduling_board_service import serialize_week_board
from app.services.v2_scheduling_lifecycle_service import within_employment_dates
from test_scheduling_lifecycle_integrity import scheduling_db, complete, values, args, DAY
from test_v2_primary import primary, login, post


def lead(db, manager):
    account = db.get(m.Principal, manager.id); account.role=m.PrincipalRole.LEAD; db.flush()
    return Principal(account.id, account.username, Role.LEAD, None, True)


def owner(db):
    account=m.Principal(username='Existing Owner', role=m.PrincipalRole.ADMIN, active=True)
    db.add(account);db.flush();return account


def test_lead_departure_owner_open_publication_and_republication(scheduling_db):
    Session, manager, ids, _ = scheduling_db
    with Session() as db:
        period, rows=complete(db,manager,ids)
        actor=lead(db,manager); account=owner(db)
        # Existing published intent is immutable after preparation.
        approve_and_publish_schedule(db, **args(period,actor,ids));db.commit()
        source=rows[0];original=(source.employee_id,source.store_id,source.start_time)
        db.add(m.SchedulingNotification(principal_id=actor.id,event_type='SHIFT_TRANSFER_COMPLETED',message='Existing scheduling notification',entity_type='shift_transfer',entity_id=source.id));db.flush()
        total_employees=db.scalar(select(func.count()).select_from(m.Employee))
        kw=dict(principal=actor, employee_id=source.employee_id,final_day=DAY-timedelta(days=1),
                allowed_store_ids=(ids['north'],ids['south']))
        preview=preview_departure(db,**kw)
        assert source.id in {s.id for s in preview['shifts']}
        revisions=prepare_departure(db,**kw,fingerprint=preview['fingerprint'],reason='Resignation; cover future work')
        revision,=revisions
        vacancy=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==revision.id,
            m.ScheduleShift.source_shift_id==source.id))
        assert vacancy.employee_id is None and vacancy.manually_locked
        assert not within_employment_dates(db.get(m.Employee,source.employee_id),DAY)
        assert original==(source.employee_id,source.store_id,source.start_time)
        # Publish a partial schedule independently and retain warning evidence.
        approve_and_publish_schedule(db,**args(revision,actor,ids));db.commit()
        assert revision.status==m.SchedulePeriodStatus.PUBLISHED
        second=svc.clone_published_revision(db,principal=actor,published_period_id=revision.id,
            allowed_store_ids=(ids['north'],ids['south']))
        replacement=db.scalar(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==second.id,
            m.ScheduleShift.source_shift_id==vacancy.id))
        svc.update_shift(db,**args(second,actor,ids),shift_id=replacement.id,
            values=values(replacement,owner_principal_id=account.id),manual_edit=True)
        approve_and_publish_schedule(db,**args(second,actor,ids));db.commit()
        cards=company_published_assignments(db,start_date=period.week_start_date,end_date=period.week_end_date)
        card=next(c for c in cards if c.id==replacement.id)
        assert card.owner_name=='Existing Owner' and card.employee_id is None
        assert card.effective_scheduled_hours==0 and not card.transfer_eligible
        board=serialize_week_board(db,week_start=period.week_start_date,
            selected_store_ids=(ids['north'],ids['south']),all_authorized_store_ids=(ids['north'],ids['south']),permission_flags={})
        shift=next(s for s in board['shifts'] if s['id']==replacement.id)
        assert shift['owner_principal_id']==account.id and not shift['is_open']
        assert db.scalar(select(func.count()).select_from(m.Employee))==total_employees
        assert db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:EMERGENCY_COVERAGE_PREPARED'))
        assert db.scalar(select(m.SchedulingNotification)) is not None
        evidence=db.scalar(select(m.ScheduleGeneratorSnapshot).where(m.ScheduleGeneratorSnapshot.schedule_period_id==second.id,
            m.ScheduleGeneratorSnapshot.generation_kind=='PUBLICATION'))
        assert any(s['owner_principal_id']==account.id for s in evidence.payload['result']['shifts'])


def test_owner_multiple_assignments_exclusive_identity_overlap_and_revision(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,manager,ids);actor=lead(db,manager);account=owner(db);s=rows[0]
        from test_v2_scheduling_foundation import date_hours
        date_hours(db,actor,ids['south'],DAY)
        result=svc.create_shift(db,**args(period,actor,ids),values=svc.ShiftInput(None,ids['south'],DAY,time(12),time(17)),manual_edit=True)
        rows.append(db.get(m.ScheduleShift,result.shift_id))
        for store,start,end in [(ids['north'],time(9),time(12)),(ids['south'],time(12),time(17))]:
            # Reuse existing slots, with sequential Owner coverage across stores.
            target=s if store==ids['north'] else rows[1]
            svc.update_shift(db,**args(period,actor,ids),shift_id=target.id,
                values=values(target,employee_id=None,owner_principal_id=account.id,start_time=start,end_time=end),manual_edit=True)
        with pytest.raises(svc.SchedulingValidationError,match='overlaps'):
            svc.update_shift(db,**args(period,actor,ids),shift_id=s.id,
                values=values(s,employee_id=None,owner_principal_id=account.id,end_time=time(13)),manual_edit=True)
        with pytest.raises(svc.SchedulingValidationError,match='active Owner'):
            svc.update_shift(db,**args(period,actor,ids),shift_id=s.id,
                values=values(s,owner_principal_id=actor.id),manual_edit=True)
        assert db.scalar(select(func.count()).select_from(m.ScheduleShift).where(m.ScheduleShift.owner_principal_id==account.id))==2


@pytest.mark.parametrize('restriction',['availability','hours','consecutive'])
def test_lead_manual_exceptions_keep_generator_rules_and_warning_evidence(scheduling_db,restriction):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,manager,ids);actor=lead(db,manager);s=rows[0]
        profile=db.scalar(select(m.EmployeeSchedulingProfile).where(m.EmployeeSchedulingProfile.employee_id==s.employee_id))
        if restriction=='availability':
            db.add(m.EmployeeSchedulingWindow(employee_id=s.employee_id,day_of_week=2,kind=m.SchedulingWindowKind.HARD_UNAVAILABLE,
                start_time=time(0),end_time=time(23,59),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
            code='HARD_WEEKDAY_LOCKOUT'
        elif restriction=='hours':profile.maximum_weekly_hours=1;profile.minimum_weekly_hours=None;code='MAX_WEEKLY_HOURS'
        else:
            from test_v2_scheduling_foundation import date_hours
            profile.max_consecutive_work_days=1;profile.minimum_days_off_after_max_block=0
            previous_period=svc.create_draft_period(db,principal=actor,week_start=period.week_start_date-timedelta(days=7))
            for offset in (1,2,3):
                previous=DAY-timedelta(days=offset);date_hours(db,actor,s.store_id,previous)
                db.add(m.ScheduleShift(schedule_period_id=period.id if previous>=period.week_start_date else previous_period.id,employee_id=s.employee_id,store_id=s.store_id,
                    shift_date=previous,start_time=time(9),end_time=time(17),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
            code='MAX_CONSECUTIVE_DAYS'
        db.flush()
        def verdict():return evaluate_assignment(db,employee_id=s.employee_id,store_id=s.store_id,shift_date=s.shift_date,
            start_time=s.start_time,end_time=s.end_time,exclude_shift_id=s.id,schedule_period_id=period.id)
        assert code in {r.code for r in verdict().reasons}
        svc.update_shift(db,**args(period,actor,ids),shift_id=s.id,values=values(s),manual_edit=True,
            operational_override=True,override_reason='Emergency staffing decision')
        assert code in {r.code for r in verdict().reasons} # The calculator is unchanged.
        if restriction=='consecutive':
            for prior in db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.employee_id==s.employee_id,m.ScheduleShift.id!=s.id)).all():
                per=db.get(m.SchedulePeriod,prior.schedule_period_id)
                svc.update_shift(db,**args(per,actor,ids),shift_id=prior.id,values=values(prior),manual_edit=True,
                    operational_override=True,override_reason='Emergency staffing decision')
        approve_and_publish_schedule(db,**args(period,actor,ids));db.commit()
        event=db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:SHIFT_CHANGED').order_by(m.AuditLog.id.desc()))
        assert code in {r['code'] for r in event.meta['metadata']['operational_exceptions']}
        assert db.scalar(select(m.ScheduleWarning).where(m.ScheduleWarning.schedule_period_id==period.id))


def test_departure_preview_stale_and_denial_are_atomic(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,manager,ids);actor=lead(db,manager)
        kw=dict(principal=actor,employee_id=rows[0].employee_id,final_day=DAY-timedelta(days=1),
            allowed_store_ids=(ids['north'],ids['south']))
        preview=preview_departure(db,**kw);period.version+=1;db.flush()
        with pytest.raises(svc.SchedulingConflict):
            prepare_departure(db,**kw,fingerprint=preview['fingerprint'],reason='Departure')
        assert rows[0].employee_id is not None and not db.get(m.Employee,rows[0].employee_id).eligibility_periods
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id,permission_key='scheduling.modify_published',allowed=False));db.flush()
        with pytest.raises(PermissionError):preview_departure(db,**kw)


def test_lead_emergency_http_and_owner_editor(primary):
    primary.account('LEAD');assert login(primary).status_code==303
    with primary.Session() as db:
        period,rows=complete(db,primary.manager,primary.ids);owner(db);db.commit()
    response=primary.client.get('/v2/scheduling/emergency-coverage',params={'employee_id':primary.ids['alex'],'final_day':str(DAY-timedelta(days=1))})
    assert response.status_code==200,response.text
    assert 'Record departure and prepare coverage revisions' in response.text
    page=primary.client.get('/v2/scheduling/week',params={'start':str(DAY-timedelta(days=2))})
    assert page.status_code==200,page.text
    assert 'Owner Coverage · Existing Owner' in page.text and 'Emergency coverage' in page.text


def test_lead_response_covers_all_four_stores(scheduling_db):
    Session,manager,ids,_=scheduling_db
    from test_v2_scheduling_foundation import date_hours
    with Session() as db:
        period,rows=complete(db,manager,ids);actor=lead(db,manager);account=owner(db)
        extra=[m.Store(name=name,square_location_id='TEST-'+name,active=True) for name in ('Third','Fourth')]
        db.add_all(extra);db.flush()
        stores=(ids['north'],ids['south'],extra[0].id,extra[1].id)
        for n,store_id in enumerate(stores[1:],1):
            day=DAY+timedelta(days=n);date_hours(db,actor,store_id,day)
            db.add(m.ScheduleShift(schedule_period_id=period.id,employee_id=ids['alex'],store_id=store_id,
                shift_date=day,start_time=time(9),end_time=time(17),created_by_principal_id=actor.id,updated_by_principal_id=actor.id))
        db.flush()
        kw=dict(principal=actor,employee_id=ids['alex'],final_day=DAY-timedelta(days=1),allowed_store_ids=stores)
        preview=preview_departure(db,**kw)
        assert {s.store_id for s in preview['shifts']}==set(stores)
        revision,=prepare_departure(db,**kw,fingerprint=preview['fingerprint'],reason='Emergency resignation across all stores')
        shifts=list(db.scalars(select(m.ScheduleShift).where(m.ScheduleShift.schedule_period_id==revision.id)))
        for s in shifts:
            if s.store_id==ids['south']:
                svc.update_shift(db,principal=actor,schedule_period_id=revision.id,shift_id=s.id,expected_version=revision.version,
                    values=values(s,employee_id=ids['blair']),allowed_store_ids=stores,manual_edit=True)
            elif s.store_id==extra[0].id:
                svc.update_shift(db,principal=actor,schedule_period_id=revision.id,shift_id=s.id,expected_version=revision.version,
                    values=values(s,owner_principal_id=account.id),allowed_store_ids=stores,manual_edit=True)
        approve_and_publish_schedule(db,principal=actor,schedule_period_id=revision.id,expected_version=revision.version,allowed_store_ids=stores)
        assert revision.status==m.SchedulePeriodStatus.PUBLISHED
        assert len([s for s in shifts if s.employee_id is None and s.owner_principal_id is None])==2
        assert any(s.owner_principal_id==account.id for s in shifts)
        assert any(s.employee_id==ids['blair'] for s in shifts)


def test_additive_owner_migration_retains_records_and_blocks_destructive_rollback(scheduling_db):
    from alembic import command
    from sqlalchemy import text
    from app.schema_contract import _alembic_config, assert_supported_schema
    Session,manager,ids,engine=scheduling_db
    with Session() as db:
        period,rows=complete(db,manager,ids)
        svc.approve_schedule(db,**args(period,manager,ids));db.commit()
        tables=('principals','employees','schedule_periods','schedule_shifts','schedule_approvals',
                'schedule_generator_snapshots','audit_logs')
        # Audit table spelling is owned by the model, not inferred from its class name.
        tables=(*tables[:-1],m.AuditLog.__tablename__)
        def state():
            with engine.connect() as conn:
                return {table:list(conn.execute(text(f'SELECT (to_jsonb(t) - \'owner_principal_id\')::text FROM {table} t ORDER BY id')).scalars()) for table in tables}
        before=state();config=_alembic_config(str(engine.url))
        command.downgrade(config,'20261026_0052');command.upgrade(config,'20261027_0053')
        assert state()==before
        assert_supported_schema(engine)
        actor=lead(db,manager);account=owner(db)
        svc.update_shift(db,**args(period,actor,ids),shift_id=rows[0].id,
            values=values(rows[0],employee_id=None,owner_principal_id=account.id),manual_edit=True)
        db.commit()
        with pytest.raises(RuntimeError,match='Preserve Owner Coverage'):
            command.downgrade(config,'20261026_0052')
        with engine.connect() as conn:
            assert conn.execute(text('SELECT owner_principal_id FROM schedule_shifts WHERE id=:id'),{'id':rows[0].id}).scalar_one()==account.id


def test_lead_partial_publication_honors_explicit_warning_permission_denial(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,manager,ids);actor=lead(db,manager)
        svc.update_shift(db,**args(period,actor,ids),shift_id=rows[0].id,
            values=values(rows[0],employee_id=None),manual_edit=True)
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id,permission_key='scheduling.publish_with_warnings',allowed=False));db.flush()
        with pytest.raises(svc.SchedulingValidationError):
            approve_and_publish_schedule(db,**args(period,actor,ids))
        assert period.status==m.SchedulePeriodStatus.DRAFT
        assert db.scalar(select(m.ScheduleApproval)) is None


def test_emergency_preparation_reuses_audited_unlock_for_authorized_manager(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        period,rows=complete(db,manager,ids)
        kw=dict(principal=manager,employee_id=rows[0].employee_id,final_day=DAY-timedelta(days=1),allowed_store_ids=(ids['north'],ids['south']))
        preview=preview_departure(db,**kw)
        prepare_departure(db,**kw,fingerprint=preview['fingerprint'],reason='Resignation')
        assert rows[0].employee_id is None and rows[0].manually_locked
        assert db.scalar(select(m.AuditLog).where(m.AuditLog.action=='V2:SCHEDULING:MANUAL_ASSIGNMENT_LOCK_CHANGED'))
