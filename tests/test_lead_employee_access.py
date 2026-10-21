"""Lead employee duties retain identity, published-store scope and blind output."""
import re
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services.access_control_service import effective_permission_flags, principal_has_permission
from app.services.inventory_working_store_service import inventory_principal, assert_inventory_store, WorkingStoreConflict
from app.services.employee_profile_service import save_employee_account
from app.services.session_service import create_management_user
from test_v2_primary import primary, login, post
from test_v2_scheduling_foundation import scheduling_db
from test_employee_operational_context import seeded_current
from test_v2_employee_profiles import _admin


@pytest.mark.parametrize('role', ['STORE', 'LEAD'])
def test_unscheduled_staff_can_login_and_use_personal_workflows(primary, role):
    primary.account(role)
    assert login(primary).status_code == 303
    for path in ('/v2/scheduling/my-schedule', '/v2/scheduling/my-time-off'):
        page = primary.client.get(path)
        assert page.status_code == 200
        assert 'Daily inventory count' in page.text
    # User-selected locations cannot grant an unscheduled working store.
    assert primary.client.get('/store/daily-count?store_id='+str(primary.ids['north'])).status_code == 409


@pytest.mark.parametrize('linked', [False, True])
def test_lead_defaults_and_no_administrative_escalation(primary, linked):
    pid = primary.account('LEAD', linked=linked)
    login(primary)
    with primary.Session() as db:
        p = Principal(pid, 'person@example.test', Role.LEAD, None, True)
        flags = effective_permission_flags(db, principal=p)
        assert flags['store.access'] is linked
        assert flags['management.access']
        assert not any(flags[k] for k in ('management.admin','management.users','management.groups'))
    page = primary.client.get('/v2/overview')
    assert page.status_code == 200 and '/management/sessions' in page.text
    assert ('href="/v2/scheduling/my-schedule"' in page.text) is linked
    assert primary.client.get('/management/users').status_code == 403
    assert primary.client.get('/store/daily-count').status_code == (409 if linked else 403)


@pytest.mark.parametrize('level', ['principal', 'role'])
def test_explicit_store_denials_override_linked_lead_default(primary, level):
    pid = primary.account('LEAD')
    with primary.Session() as db:
        row = (m.PrincipalPermissionOverride(principal_id=pid,permission_key='store.access',allowed=False)
               if level == 'principal' else m.RolePermissionOverride(role=m.PrincipalRole.LEAD,permission_key='store.access',allowed=False))
        db.add(row); db.commit()
        p=Principal(pid,'person@example.test',Role.LEAD,None,True)
        assert not effective_permission_flags(db,principal=p)['store.access']
        assert not principal_has_permission(db,principal=p,permission_key='store.access',fallback_allowed=False)
    login(primary)
    assert primary.client.get('/store/daily-count').status_code == 403


def test_lead_blind_count_complete_resume_scope_projection_locks_and_audit(primary, monkeypatch):
    from app.routers import store
    pid, _, _, _ = seeded_current(primary, two=False, role='LEAD')
    provider=Mock()
    provider.list_count_items.return_value=[SimpleNamespace(variation_id='LEAD-V1',sku='SKU',item_name='Blind product',variation_name='Test',source_catalog_version='test')]
    provider.fetch_current_on_hand.return_value={'LEAD-V1':Decimal('5')}
    monkeypatch.setattr(store,'snapshot_provider',provider)
    with primary.Session() as db:
        group=m.CountGroup(name='Lead test group');db.add(group);db.flush()
        campaign=db.scalar(select(m.Campaign))
        db.add(m.CountGroupCampaign(group_id=group.id,campaign_id=campaign.id));db.commit()
    context=primary.client.get('/session-status').json()['working_context']
    generated=post(primary.client,'/store/sessions/generate',working_context=context,employee_name='Entered display name')
    assert generated.status_code == 303, generated.text
    path=generated.headers['location']; sid=int(path.rsplit('/',1)[1])
    assert post(primary.client,'/store/sessions/generate',working_context=context,employee_name='Other name').headers['location']==path
    with primary.Session() as db:
        line=db.get(m.SnapshotLine,(sid,'LEAD-V1')); line.expected_on_hand=Decimal('928347');line.previous_recount_variance=Decimal('-123456');db.commit()
    page=primary.client.get(path)
    assert page.status_code==200 and 'Blind product' in page.text
    assert '928347' not in page.text and '-123456' not in page.text
    headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token'),'X-Working-Context':context}
    response=primary.client.post(path+'/draft',headers=headers,json={'revision':0,'operation_id':'lead-op','changes':{'LEAD-V1':{'front_qty':'2','back_qty':'3'}}})
    assert response.status_code==200,response.text
    revision=response.json()['revision']
    result=primary.client.post(path+'/submit',headers=headers,json={'revision':revision})
    assert result.status_code==200 and result.json()=={'submitted':True,'redirect':'/store/daily-count'}
    assert primary.client.get(path).status_code==403
    assert primary.client.post(path+'/draft',headers=headers,json={'revision':revision,'operation_id':'locked','changes':{}}).status_code==400
    with primary.Session() as db:
        count=db.get(m.CountSession,sid)
        assert count.created_by_principal_id==pid and count.store_id==primary.ids['north']
        assert db.scalar(select(m.CountObservation).where(m.CountObservation.session_id==sid)).counted_by_principal_id==pid
        assert all(a.actor_principal_id==pid for a in db.scalars(select(m.AuditLog).where(m.AuditLog.session_id==sid)))
        scoped,_=inventory_principal(db,principal=Principal(pid,'person@example.test',Role.LEAD,None,True))
        with pytest.raises(WorkingStoreConflict):assert_inventory_store(db,principal=scoped,store_id=primary.ids['south'])
        # Supervisory access is still intentionally separate and unscoped.
        assert assert_inventory_store(db,principal=replace(scoped,store_id=None),store_id=primary.ids['south'])['version']=='management'
    assert primary.client.get('/management/sessions').status_code==200


def test_lead_store_workflows_and_ordinary_employee_no_oversight(primary):
    pid,_,_,_=seeded_current(primary,two=False,role='LEAD')
    client=primary.client
    for path in ('/store/daily-count','/store/daily-chore-sheet','/store/opening-checklist',
                 '/store/non-sellable-stock-take','/store/change-box-count','/store/change-form',
                 '/store/customer-requests','/store/exchange-return-form'):
        assert client.get(path).status_code==200,path
    page=client.get('/store/daily-chore-sheet')
    sid=int(re.search(r'/daily-chore-sheet/(\d+)/save',page.text).group(1))
    context=client.get('/session-status').json()['working_context']
    with primary.Session() as db:
        tasks=list(db.scalars(select(m.DailyChoreEntry.task_id).where(m.DailyChoreEntry.sheet_id==sid)))
    assert post(client,f'/store/daily-chore-sheet/{sid}/submit',working_context=context,employee_name='Alex',completed_task_ids=[str(t) for t in tasks]).status_code==303
    with primary.Session() as db:
        assert db.get(m.DailyChoreSheet,sid).created_by_principal_id==pid
        assert db.scalar(select(m.AuditLog).where(m.AuditLog.actor_principal_id==pid,m.AuditLog.action=='DAILY_CHORE_SHEET_SUBMITTED')) is not None
        db.get(m.Principal,pid).role=m.PrincipalRole.STORE;db.commit()
    assert client.get('/store/daily-count').status_code==200
    assert client.get('/management/sessions').status_code==403
    assert client.get('/management/users').status_code==403


def test_duplicate_identity_does_not_inherit_employee_access(primary):
    linked=primary.account('LEAD')
    with primary.Session() as db:
        duplicate=m.Principal(username='unlinked-duplicate',role=m.PrincipalRole.LEAD,store_id=None,active=True)
        db.add(duplicate);db.flush()
        p=Principal(duplicate.id,duplicate.username,Role.LEAD,primary.ids['north'],True)
        assert not effective_permission_flags(db,principal=p)['store.access']
        with pytest.raises(WorkingStoreConflict):inventory_principal(db,principal=p)
        assert db.get(m.Employee,primary.ids['alex']).principal_id==linked


def test_provision_lead_through_existing_employee_and_reject_duplicate_links(scheduling_db):
    Session,manager,ids,_=scheduling_db
    with Session() as db:
        admin=_admin(db); before=db.scalar(select(func.count()).select_from(m.Employee))
        with pytest.raises(ValueError,match='employee profile'):
            create_management_user(db,actor=admin,username='legacy-lead',password='test-password',role='LEAD')
        lead=save_employee_account(db,actor=admin,employee_id=ids['alex'],action='create',role='LEAD',login='alex@example.test',confirmed=True)
        assert db.get(m.Employee,ids['alex']).principal_id==lead.id
        assert lead.role==m.PrincipalRole.LEAD and lead.store_id is None
        assert effective_permission_flags(db,principal=Principal(lead.id,lead.username,Role.LEAD,None,True))['store.access']
        with pytest.raises(ValueError,match='already has an account'):
            save_employee_account(db,actor=admin,employee_id=ids['alex'],action='create',role='LEAD',login='duplicate@example.test',confirmed=True)
        with pytest.raises(ValueError,match='already linked'):
            save_employee_account(db,actor=admin,employee_id=ids['blair'],action='associate',role='LEAD',login='alex@example.test',confirmed=True)
        with pytest.raises(PermissionError):
            save_employee_account(db,actor=manager,employee_id=ids['blair'],action='create',role='LEAD',login='other@example.test',confirmed=True)
        assert db.scalar(select(func.count()).select_from(m.Employee))==before


@pytest.mark.parametrize('role',['STORE','LEAD'])
def test_unscheduled_own_pto_uses_authenticated_employee(primary,role):
    pid=primary.account(role);assert login(primary).status_code==303
    values={'start_date':'2026-11-10','end_date':'2026-11-11','duration':'FULL',
            'reason_category_id':str(primary.ids['vacation']),'employee_note':'Personal request',
            'employee_id':str(primary.ids['blair'])}
    assert post(primary.client,'/v2/scheduling/my-time-off',**values).status_code==303
    with primary.Session() as db:
        row=db.scalar(select(m.TimeOffRequest))
        assert row.employee_id==primary.ids['alex'] and row.status==m.TimeOffRequestStatus.PENDING


def test_lead_opening_checklist_submission_and_audit(primary):
    pid,_,_,_=seeded_current(primary,two=False,role='LEAD')
    from app.services.opening_checklist_service import ensure_default_items
    with primary.Session() as db:
        items=ensure_default_items(db,store_id=primary.ids['north'])
        answers={f'answer__{item.id}':('Y' if item.item_type==m.OpeningChecklistItemType.PARENT else 'NA') for item in items}
        db.commit()
    context=primary.client.get('/session-status').json()['working_context']
    response=post(primary.client,'/store/opening-checklist/submit',working_context=context,
                  submitted_by_name='Alex',lead_name='Alex',previous_employee='Blair',
                  summary_notes_type='NONE',summary_notes='',**answers)
    assert response.status_code==303,response.text
    with primary.Session() as db:
        assert db.scalar(select(m.OpeningChecklistSubmission)).created_by_principal_id==pid
        assert db.scalar(select(m.AuditLog).where(m.AuditLog.action=='OPENING_CHECKLIST_SUBMITTED')).actor_principal_id==pid


@pytest.mark.parametrize('active,employee_active',[(False,True),(True,False)])
def test_inactive_lead_never_gets_employee_access(primary,active,employee_active):
    pid=primary.account('LEAD',active=active,employee_active=employee_active)
    assert login(primary).status_code==401
    with primary.Session() as db:
        assert not effective_permission_flags(db,principal=Principal(pid,'person@example.test',Role.LEAD,None,active))['store.access']


def test_optional_employee_exchange_form_retains_published_scope(primary,monkeypatch):
    from app.config import settings
    seeded_current(primary,two=False,role='LEAD')
    monkeypatch.setattr(settings,'v2_enabled_features','v2_primary,staff_scheduling_v2,exchanges_returns_v2')
    path='/v2/customer-forms/exchanges-returns'
    assert primary.client.get(path).status_code==200
    assert primary.client.get(path,params={'store_id':primary.ids['south']}).status_code==403


def test_concurrent_lead_count_generation_resumes_one_draft(primary):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.session_service import create_count_session
    pid,_,_,_=seeded_current(primary,two=False,role='LEAD')
    with primary.Session() as db:
        group=m.CountGroup(name='Concurrent Lead count');db.add(group);db.flush()
        db.add(m.CountGroupCampaign(group_id=group.id,campaign_id=db.scalar(select(m.Campaign.id))));db.commit()
    barrier=Barrier(2)
    provider=Mock();provider.list_count_items.return_value=[]
    def generate(_):
        with primary.Session() as db:
            barrier.wait(timeout=10)
            scoped,_=inventory_principal(db,principal=Principal(pid,'person@example.test',Role.LEAD,None,True))
            count,created=create_count_session(db,principal=scoped,employee_name='Alex',snapshot_provider=provider)
            result=(count.id,created);db.commit();return result
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(generate,range(2)))
    assert len({r[0] for r in results})==1 and sorted(r[1] for r in results)==[False,True]


def test_concurrent_lead_provisioning_cannot_create_two_accounts(scheduling_db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    Session,_,ids,_=scheduling_db
    with Session() as db:admin=_admin(db);db.commit()
    barrier=Barrier(2)
    def provision(_):
        with Session() as db:
            barrier.wait(timeout=10)
            try:
                account=save_employee_account(db,actor=admin,employee_id=ids['alex'],action='create',role='LEAD',login='one-lead@example.test',confirmed=True)
                db.commit();return account.id
            except ValueError:
                db.rollback();return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(provision,range(2)))
    assert sum(r is not None for r in results)==1
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(m.Principal).where(m.Principal.username=='one-lead@example.test'))==1


def test_optional_daily_log_lead_entry_and_existing_oversight(primary,monkeypatch):
    from app.config import settings
    from test_v2_daily_store_logs import _select_store, _valid_form
    pid=primary.account('LEAD');login(primary)
    monkeypatch.setattr(settings,'v2_enabled_features','v2_primary,staff_scheduling_v2,daily_store_logs_v2')
    # This optional module retains its existing explicit Current Store workflow.
    assert _select_store(primary.client,primary.ids['north']).status_code==303
    path='/v2/store-operations/daily-logs'
    page=primary.client.get(path);assert page.status_code==200
    response=primary.client.post(path,data=_valid_form(page.text))
    assert response.status_code==303,response.text
    with primary.Session() as db:
        row=db.scalar(select(m.DailyStoreLog))
        assert row.submitted_by_principal_id==pid and row.store_id==primary.ids['north']
    assert primary.client.get(path+'/history').status_code==200
