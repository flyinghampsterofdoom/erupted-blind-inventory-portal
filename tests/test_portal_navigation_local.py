"""Reviewed role visibility, unchanged entry policy, and safe employee presentation."""
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import date,datetime,timedelta
from html.parser import HTMLParser
import pytest
from sqlalchemy import select
from starlette.requests import Request
from app.auth import Principal,Role
from app.config import settings
from app import models as m
from app.services.access_control_service import fallback_allowed_for_role
from app.v2.navigation import build_primary_navigation
from app.v2.entry import portal_home_destination,landing_destination
from test_v2_scheduling_foundation import scheduling_db
from test_v2_primary import primary,login
from test_company_schedule_visibility import seed_company,WEEK

class PortalPage(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.links=[]; self.week_json=''; self.in_nav=False; self.collect=False; self.current=None; self.feed(text)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='nav' and 'v2-nav' in attrs.get('class','').split(): self.in_nav=True
        if tag=='a':
            self.current=dict(attrs, in_nav=self.in_nav, text=''); self.links.append(self.current)
        if tag=='script': self.collect='data-employee-week' in attrs
    def handle_endtag(self, tag):
        if tag=='a': self.current=None
        if tag=='nav': self.in_nav=False
        if tag=='script': self.collect=False
    def handle_data(self, data):
        if self.current is not None: self.current['text']+=data
        if self.collect: self.week_json+=data
    @property
    def nav_links(self): return [a for a in self.links if a['in_nav']]

PROFILES=json.loads((Path(__file__).parent/'fixtures/reviewed_portal_navigation.json').read_text())['profiles']


def test_compact_week_json_preserves_content_and_script_boundary():
    from app.v2.employee_schedule_presentation import employee_week_json
    value = {'name': 'Two  spaces: </script><script>alert(1)</script> & "quote"',
             'flags': [True, False, None], 'hours': 7.5}
    encoded = employee_week_json(value)
    assert json.loads(encoded) == value
    assert '<' not in encoded and '>' not in encoded and '&' not in encoded
    assert len(encoded) < len(__import__('jinja2').utils.htmlsafe_json_dumps(value))

@pytest.mark.parametrize('profile',PROFILES,ids=lambda p:p['key'])
def test_every_reviewed_production_destination_remains_available(monkeypatch,profile):
    features='v2_primary,staff_scheduling_v2'+(',daily_store_logs_v2'if profile['key']=='store-2'else'')
    monkeypatch.setattr(settings,'v2_enabled_features',features);monkeypatch.setattr(settings,'v2_principal_features','')
    p=Principal(999,'synthetic',Role(profile['role']),1 if profile['has_assigned_legacy_store']else None,True)
    r=Request({'type':'http','path':'/v2/scheduling/my-schedule','headers':[],'query_string':b''})
    r.state.principal=p;r.state.employee_id=1 if profile['linked_employee']else None
    from app.services.access_control_service import permission_defs
    r.state.permission_flags={d.key:fallback_allowed_for_role(role=p.role,permission_key=d.key)for d in permission_defs()}
    if p.role==Role.LEAD and profile['linked_employee']:r.state.permission_flags['store.access']=True
    paths={c.href for s in build_primary_navigation(r)for c in s.children}
    expected={d['path']for d in profile['destinations']+profile['dashboard_cards']+profile['existing_contextual_links']if d['path']}
    assert expected <= paths
    assert portal_home_destination(r)==profile['proposed_home']
    assert landing_destination(r)==profile['current_root_landing']
    if not profile['linked_employee']:assert '/v2/scheduling/my-schedule' not in paths

@pytest.mark.parametrize('role,target',[('STORE','/v2/store-operations'),('LEAD','/v2/overview')])
def test_home_brand_login_and_real_launcher(primary,role,target):
    primary.account(role);assert login(primary).headers['location']=='/v2/scheduling/my-schedule'
    page=primary.client.get('/v2/scheduling/my-schedule')
    soup=PortalPage(page.text)
    assert {a['href']for a in [a for a in soup.links if a.get('aria-label')=='Erupted Operations Home']}=={target}
    assert next(a['href']for a in soup.links if 'v2-topbar-home'in a.get('class',''))==target
    assert any(a['text'].strip()=='Home'and a['href']==target for a in soup.nav_links)
    assert primary.client.get(target).status_code==200
    assert primary.client.get('/').headers['location']=='/v2/scheduling/my-schedule'
    if role=='LEAD':assert '/management/home'in{a['href']for a in soup.nav_links}


def test_permissions_feature_exposure_and_linkage_are_not_granted_by_navigation(primary,monkeypatch):
    pid=primary.account();login(primary)
    with primary.Session()as db:
        for key in ['scheduling.view_own','scheduling.time_off.submit_own','scheduling.preferences.request']:
            db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key=key,allowed=False))
        db.commit()
    soup=PortalPage(primary.client.get('/v2/store-operations').text)
    links={a['href']for a in soup.nav_links}
    assert '/v2/scheduling/my-schedule'not in links
    assert '/v2/scheduling/my-time-off'not in links
    assert '/v2/scheduling/preference-requests'not in links
    assert primary.client.get('/v2/scheduling/my-schedule').status_code==403
    assert primary.client.get('/management/home').status_code==403
    monkeypatch.setattr(settings,'v2_enabled_features','v2_primary')
    soup=PortalPage(primary.client.get('/v2/store-operations').text)
    assert not any('/v2/scheduling/'in a['href']for a in soup.nav_links)


def test_presentation_reuses_exact_published_cards_without_private_data(primary):
    primary.account();_,ids,_,draft=seed_company(primary);login(primary)
    page=primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    assert page.status_code==200
    data=json.loads(PortalPage(page.text).week_json)
    from app.services.employee_schedule_service import company_published_assignments
    with primary.Session()as db:
        authority=company_published_assignments(db,start_date=WEEK,end_date=WEEK+timedelta(days=6))
        assert [(s['id'],s['employeeId'],s['paidHours'])for s in data['shifts']]==[(s.id,s.employee_id,s.effective_scheduled_hours)for s in authority]
    assert draft not in [s['id']for s in data['shifts']]
    assert 'PRIVATE EMPLOYEE NOTE'not in page.text
    assert {s['tone']for s in data['stores']}=={'andresen','hwy99','longview','sr503'}
    assert any('Closed'in n['label']for n in data['notices'])
    assert page.headers['cache-control']=='no-store'


def test_request_and_transfer_denial_is_not_frontend_only(primary):
    pid=primary.account();_,ids,_,_=seed_company(primary);login(primary)
    with primary.Session()as db:
        db.add(m.PrincipalPermissionOverride(principal_id=pid,permission_key='scheduling.transfer_own',allowed=False));db.commit()
    page=primary.client.get('/v2/scheduling/my-schedule?start='+str(WEEK))
    data=json.loads(PortalPage(page.text).week_json)
    assert all(not s['canRequest']for s in data['shifts'])
    assert primary.client.post('/v2/scheduling/api/transfers',json={'shift_id':ids[0],'to_employee_id':primary.ids['blair']},headers={'X-CSRF-Token':primary.client.cookies.get('csrf_token')}).status_code==403


def test_default_sunday_does_not_include_previous_monday(primary,monkeypatch):
    from app.routers import v2_scheduling
    class SundayClock(datetime):
        @classmethod
        def now(cls,tz=None): return datetime(2030,5,5,12,tzinfo=tz)
    monkeypatch.setattr(v2_scheduling,'datetime',SundayClock)
    primary.account();_,current_ids,_,_=seed_company(primary)
    with primary.Session()as db:
        prior=m.SchedulePeriod(week_start_date=WEEK-timedelta(days=7),week_end_date=WEEK-timedelta(days=1),
            revision_number=1,status=m.SchedulePeriodStatus.PUBLISHED,
            created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
        db.add(prior);db.flush()
        from datetime import time
        shift=m.ScheduleShift(schedule_period_id=prior.id,employee_id=primary.ids['alex'],store_id=primary.ids['north'],
            shift_date=WEEK-timedelta(days=6),start_time=time(20),end_time=time(21),
            created_by_principal_id=primary.manager.id,updated_by_principal_id=primary.manager.id)
        db.add(shift);db.commit();previous_id=shift.id
    login(primary)
    response=primary.client.get('/v2/scheduling/my-schedule')
    data=json.loads(PortalPage(response.text).week_json)
    assert data['today']==str(WEEK) and data['weekStart']==str(WEEK)
    assert set(current_ids)=={s['id']for s in data['shifts']}
    assert previous_id not in {s['id']for s in data['shifts']}
