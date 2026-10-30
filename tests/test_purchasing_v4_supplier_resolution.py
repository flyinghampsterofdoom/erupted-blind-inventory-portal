"""Supplier setup is explicit, atomic, pinned and compatible with paused writes."""
from copy import deepcopy
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,pytestmark
from test_purchasing_v4_controls import execute,change,profile
from app.purchasing_v4 import worksheets,controls,supplier_resolution as resolution
from app.purchasing_v4.reconciliation import public_fingerprints

@pytest.fixture
def unresolved(sheet):
    e,original=sheet;vid=original['rows'][0]['variation_id'];first=original['rows'][0]['supplier_id']
    with e.connect() as c:other=str(c.scalar(text('SELECT id FROM purchasing_v4.authority_supplier WHERE id<>CAST(:id AS uuid) LIMIT 1'),{'id':first}))
    execute(e,[change('SUPPLIER',other,dict(profile('Explicit alternative'),supplier_id=other)),change('ASSIGNMENT',vid,dict(variation_id=vid,supplier_id=None),1)])
    with e.begin() as c:
        row=deepcopy(original['rows'][0]['original']);row.update(id=str(uuid4()),offers={},supplier_id=None,pack_size=None,unit_cost=None,packs=[2,None,None,None],touched=[True,False,False,False],advice_units=['5']*4)
        state=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Unresolved supplier fixture',rows=[row])
    return e,state,other


def save(e,state,supplier,**kw):
    request=dict(actor_id=1,session_id=state['session']['id'],command_id=str(uuid4()),expected_version=state['session']['version'],row_id=state['rows'][0]['id'],supplier_id=supplier,expected_offer_version=0,expected_supplier_version=1,expected_assignment_version=2,scope='order',value=dict(pack_size=None,expected_unit_cost=None,vendor_sku=None,approve=False));request.update(kw)
    with e.begin() as c:return resolution.resolve(c,**request)


def test_pending_link_is_not_zero_or_an_approved_default_and_manual_packs_survive(unresolved):
    e,s,sid=unresolved;vid=s['rows'][0]['variation_id']
    with reading(e) as c:before=public_fingerprints(c)
    result=save(e,s,sid);assert not result['setup_complete'] and not result['default_changed']
    with reading(e) as c:
        row=worksheets.snapshot(c,s['session']['id'],1)['rows'][0]
        assert row['supplier_id']==sid and row['pack_size'] is None and row['unit_cost'] is None
        assert row['packs']==[2,None,None,None] and row['touched']==[True,False,False,False]
        assert row['original']==s['rows'][0]['original'] and row['supplier_setup_complete'] is False
        assert all(x['state']=='UNRESOLVED' for x in row['recommendation_status'])
        offer=controls.heads(c,{('OFFER',vid+':'+sid)})[('OFFER',vid+':'+sid)]['payload'];assert not offer['approved'] and offer['expected_unit_cost'] is None
        assert sid not in worksheets.catalog_rows(c,[vid],'synthetic-fixture-source')[0]['offers']
        assert public_fingerprints(c)==before


def test_approved_link_pins_price_and_changes_default_only_explicitly(unresolved):
    e,s,sid=unresolved;vid=s['rows'][0]['variation_id'];cid=str(uuid4());value=dict(pack_size=5,expected_unit_cost='2.50',vendor_sku='000123',approve=True)
    result=save(e,s,sid,command_id=cid,scope='default',value=value);assert result['setup_complete'] and result['default_changed']
    assert save(e,s,sid,command_id=cid,scope='default',value=value)['replayed']
    with e.connect() as c:
        row=worksheets.snapshot(c,s['session']['id'],1)['rows'][0];assert row['unit_cost']=='2.50' and row['packs']==[2,1,1,1]
        assert row['original']==s['rows'][0]['original'] and row['supplier_setup_complete']
        future=worksheets.catalog_rows(c,[vid],'synthetic-fixture-source')[0];assert future['supplier_id']==sid and future['unit_cost']=='2.50'
    key=vid+':'+sid;offer=result['pinned_offer'];updated={k:offer[k] for k in ('variation_id','supplier_id','approved','pack_size','expected_unit_cost','cost_provenance','vendor_sku')};updated['expected_unit_cost']='9.99'
    execute(e,[change('OFFER',key,updated,1)])
    with e.connect() as c:assert worksheets.snapshot(c,s['session']['id'],1)['rows'][0]['unit_cost']=='2.50'


def test_edit_and_no_reorder_preserve_supplier_pin_and_complete_five_field_contract(unresolved):
    e,s,sid=unresolved;save(e,s,sid,value=dict(pack_size=5,expected_unit_cost='1.25',vendor_sku=None,approve=True))
    with e.connect() as c:state=worksheets.snapshot(c,s['session']['id'],1)
    row=state['rows'][0];value=worksheets.editable(row);value['packs']=[3]*4;value['touched']=[True]*4
    with e.begin() as c:
        receipt=worksheets.patch(c,actor_id=1,session_id=s['session']['id'],command_id=str(uuid4()),expected_version=1,changes=[dict(id=row['id'],value=value)])
        assert set(receipt['inverse'][0]['value'])==worksheets.FIELDS
    with e.connect() as c:
        current=worksheets.snapshot(c,s['session']['id'],1)['rows'][0];assert current['unit_cost']=='1.25' and current['offers'][sid]['expected_unit_cost']=='1.25'
        stored=c.scalar(text('SELECT working FROM purchasing_v4.worksheet_row WHERE session_id=:id'),{'id':s['session']['id']});assert stored['_supplier_offers'][sid]['expected_unit_cost']=='1.25'
    with e.begin() as c:worksheets.no_reorder(c,actor_id=1,session_id=s['session']['id'],command_id=str(uuid4()),expected_version=2,row_ids=[row['id']])
    with e.connect() as c:assert worksheets.snapshot(c,s['session']['id'],1)['rows'][0]['removed']


@pytest.mark.parametrize('value,scope',[({'pack_size':5,'expected_unit_cost':None,'vendor_sku':None,'approve':True},'order'),({'pack_size':None,'expected_unit_cost':'2','vendor_sku':None,'approve':True},'order'),({'pack_size':5,'expected_unit_cost':'-1','vendor_sku':None,'approve':False},'order'),({'pack_size':5,'expected_unit_cost':'2','vendor_sku':None,'approve':False},'default')])
def test_incomplete_or_invalid_setup_never_partially_saves(unresolved,value,scope):
    e,s,sid=unresolved;vid=s['rows'][0]['variation_id']
    with pytest.raises(controls.InvalidPolicy):save(e,s,sid,value=value,scope=scope)
    with e.connect() as c:
        assert not controls.heads(c,{('OFFER',vid+':'+sid)})
        assert worksheets.snapshot(c,s['session']['id'],1)['session']['version']==0


def test_version_conflicts_and_receipt_reuse_are_atomic(unresolved):
    e,s,sid=unresolved;cid=str(uuid4())
    with pytest.raises(controls.Conflict):save(e,s,sid,expected_supplier_version=0)
    save(e,s,sid,command_id=cid)
    with pytest.raises(controls.Conflict):save(e,s,sid,command_id=cid,value=dict(pack_size=5,expected_unit_cost='1',vendor_sku=None,approve=True))
    with pytest.raises(controls.Conflict):save(e,s,sid)


def test_late_row_failure_rolls_back_control_offer(unresolved,monkeypatch):
    e,s,sid=unresolved;vid=s['rows'][0]['variation_id']
    def fail(*args,**kwargs):raise controls.InvalidPolicy('Injected row qualification failure')
    monkeypatch.setattr(worksheets,'validate_row',fail)
    with pytest.raises(controls.InvalidPolicy):save(e,s,sid)
    with e.connect() as c:
        assert not controls.heads(c,{('OFFER',vid+':'+sid)})
        assert worksheets.snapshot(c,s['session']['id'],1)['session']['version']==0


def test_pending_link_can_be_completed_later_without_rewriting_original(unresolved):
    e,s,sid=unresolved;save(e,s,sid)
    with e.connect() as c:current=worksheets.snapshot(c,s['session']['id'],1)
    save(e,current,sid,expected_offer_version=1,value=dict(pack_size=5,expected_unit_cost='1',vendor_sku='001',approve=True),scope='default')
    with e.connect() as c:
        row=worksheets.snapshot(c,s['session']['id'],1)['rows'][0];assert row['supplier_setup_complete'] and row['packs']==[2,1,1,1]
        assert row['original']==s['rows'][0]['original']


def test_existing_approved_offer_is_reused_without_repricing(unresolved):
    e,s,sid=unresolved;vid=s['rows'][0]['variation_id'];save(e,s,sid,value=dict(pack_size=5,expected_unit_cost='2',vendor_sku=None,approve=True))
    with e.begin() as c:other=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Another unresolved draft',rows=[s['rows'][0]['original']])
    save(e,other,sid,expected_offer_version=1,value=None)
    with e.connect() as c:
        assert controls.heads(c,{('OFFER',vid+':'+sid)})[('OFFER',vid+':'+sid)]['version']==1
        assert worksheets.snapshot(c,other['session']['id'],1)['rows'][0]['unit_cost']=='2'


def test_pending_manual_quantities_still_require_supplier_review(unresolved):
    e,s,sid=unresolved;save(e,s,sid,value=dict(pack_size=5,expected_unit_cost='2',vendor_sku=None,approve=False))
    with e.connect() as c:state=worksheets.snapshot(c,s['session']['id'],1)
    row=state['rows'][0];value=worksheets.editable(row);value.update(packs=[2]*4,touched=[True]*4)
    with e.begin() as c:worksheets.patch(c,actor_id=1,session_id=s['session']['id'],command_id=str(uuid4()),expected_version=1,changes=[dict(id=row['id'],value=value)])
    with e.connect() as c:
        row=worksheets.snapshot(c,s['session']['id'],1)['rows'][0];assert worksheets.totals([row])['unresolved_cells']==4
        assert row['packs']==[2]*4 and not row['supplier_setup_complete']


def test_supplier_required_sku_and_cross_owner_are_enforced(unresolved):
    e,s,sid=unresolved
    with e.connect() as c:p=controls.heads(c,{('SUPPLIER',sid)})[('SUPPLIER',sid)]['payload']
    execute(e,[change('SUPPLIER',sid,dict(p,use_vendor_sku=True),1)])
    with pytest.raises(controls.InvalidPolicy):save(e,s,sid,expected_supplier_version=2,value=dict(pack_size=5,expected_unit_cost='2',vendor_sku=None,approve=True))
    with pytest.raises(PermissionError):save(e,s,sid,actor_id=2)


from test_purchasing_v4_owner_inspection import inspection_site,login,csrf


def add_profile(e):
    with e.connect() as c:
        first=str(c.scalar(text("SELECT record_key FROM purchasing_v4.control_head WHERE kind='SUPPLIER' LIMIT 1")))
        other=str(c.scalar(text('SELECT id FROM purchasing_v4.authority_supplier WHERE id<>CAST(:id AS uuid) LIMIT 1'),{'id':first}))
        sid=str(c.scalar(text('SELECT id FROM purchasing_v4.worksheet ORDER BY created_at LIMIT 1')))
        state=worksheets.snapshot(c,sid,1)
    execute(e,[change('SUPPLIER',other,dict(profile('HTTP explicit alternative'),supplier_id=other))])
    return state,other


@pytest.mark.parametrize('inspection_site',['supplier-resolution'],indirect=True)
def test_http_resolution_requires_owner_csrf_and_explicit_feature(inspection_site):
    client,e,_=inspection_site;state,supplier=add_profile(e)
    assert login(client).status_code==303
    assert client.get('/api/context').json()['supplier_resolution_enabled']
    sid=state['session']['id'];rid=state['rows'][0]['id'];options=client.get('/api/worksheets/'+sid+'/supplier-options',params={'row_id':rid});assert options.status_code==200
    assert any(x['id']==supplier for x in options.json()['suppliers'])
    payload=dict(command_id=str(uuid4()),expected_version=0,row_id=rid,supplier_id=supplier,expected_offer_version=0,expected_supplier_version=1,expected_assignment_version=1,scope='order',value=dict(pack_size=None,expected_unit_cost=None,vendor_sku=None,approve=False))
    assert client.post('/api/worksheets/'+sid+'/supplier-resolution',json=payload).status_code==403
    response=client.post('/api/worksheets/'+sid+'/supplier-resolution',json=payload,headers=csrf(client));assert response.status_code==200,response.text
    assert not response.json()['setup_complete']
    current=client.get('/api/worksheets/'+sid).json()['rows'][0];assert current['supplier_id']==supplier and current['packs']==[None]*4
    assert client.post('/api/worksheets/'+sid+'/supplier-resolution',json=dict(payload,actor_id=1),headers=csrf(client)).status_code==422
    login(client,'other-admin')
    assert client.get('/api/worksheets/'+sid+'/supplier-options',params={'row_id':rid}).status_code==403
    assert client.post('/api/worksheets/'+sid+'/supplier-resolution',json=payload,headers=csrf(client)).status_code==403


@pytest.mark.parametrize('inspection_site',[True],indirect=True)
def test_compatibility_mode_reads_and_edits_new_pins_while_new_setup_is_paused(inspection_site):
    client,e,_=inspection_site;state,supplier=add_profile(e)
    save(e,state,supplier,expected_assignment_version=1,value=dict(pack_size=5,expected_unit_cost='3.25',vendor_sku=None,approve=True))
    assert login(client).status_code==303
    context=client.get('/api/context').json();assert context['configuration_enabled'] and not context['supplier_resolution_enabled']
    sid=state['session']['id'];current=client.get('/api/worksheets/'+sid).json();row=current['rows'][0]
    assert row['supplier_id']==supplier and row['unit_cost']=='3.25' and row['original']==state['rows'][0]['original']
    assert client.post('/api/worksheets/'+sid+'/supplier-resolution',json={},headers=csrf(client)).status_code==503
    value=worksheets.editable(row);value.update(packs=[3]*4,touched=[True]*4)
    response=client.post('/api/worksheets/'+sid+'/commands',json=dict(command_id=str(uuid4()),expected_version=current['session']['version'],changes=[dict(id=row['id'],value=value)]),headers=csrf(client));assert response.status_code==200,response.text
    after=client.get('/api/worksheets/'+sid).json()['rows'][0];assert after['unit_cost']=='3.25' and after['packs']==[3]*4
