"""Owner spreadsheet pack authority is shared, audited and atomic; old advice stays pinned."""
from fractions import Fraction
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,pytestmark,save
from app.purchasing_v4 import worksheets,controls


def pack_change(state,size):
    row=state['rows'][0];value={k:row[k] for k in worksheets.FIELDS}
    value['pack_size']=size
    value['packs']=[worksheets.final_pack_conversion(Fraction(x),size) if x is not None else None for x in row['advice_units']]
    return [dict(id=row['id'],value=value)]


def test_spreadsheet_pack_saves_offer_for_future_worksheets_and_undo_is_audited(sheet):
    e,state=sheet;row=state['rows'][0];key=row['variation_id']+':'+row['supplier_id'];command=str(uuid4())
    with e.connect() as c:
        before=controls.live(controls.heads(c,{('OFFER',key)})[('OFFER',key)])
        original=c.scalar(text('select original from purchasing_v4.worksheet_row where id=:id'),{'id':row['id']})
    receipt=save(e,state,pack_change(state,7),command=command)
    assert save(e,state,pack_change(state,7),command=command)['replayed']
    with e.connect() as c:
        offer=controls.live(controls.heads(c,{('OFFER',key)})[('OFFER',key)])
        assert offer==dict(before,pack_size=7)
        fresh=worksheets.catalog_rows(c,[row['variation_id']],'synthetic-fixture-source')[0]
        assert fresh['pack_size']==7
        assert c.scalar(text('select original from purchasing_v4.worksheet_row where id=:id'),{'id':row['id']})==original
        current=worksheets.snapshot(c,state['session']['id'],1)
        assert current['rows'][0]['pack_size']==7 and current['rows'][0]['packs']==[1,2,3,0]
        assert current['rows'][0]['pack_sizes'][row['supplier_id']]==7
    save(e,state,receipt['inverse'],version=1)
    with e.connect() as c:
        head=controls.heads(c,{('OFFER',key)})[('OFFER',key)]
        assert head['payload']==before and head['version']==3
        assert worksheets.snapshot(c,state['session']['id'],1)['rows'][0]['pack_size']==5


def test_concurrent_offer_change_rejects_entire_spreadsheet_pack_command(sheet):
    e,state=sheet;row=state['rows'][0];key=row['variation_id']+':'+row['supplier_id']
    with e.begin() as c:
        head=controls.heads(c,{('OFFER',key)})[('OFFER',key)]
        controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Another explicit pack decision',changes=[dict(kind='OFFER',key=key,expected_version=head['version'],value=dict(head['payload'],pack_size=10))])
    with pytest.raises(controls.Conflict,match='pack size changed'):
        save(e,state,pack_change(state,7))
    with e.connect() as c:
        assert worksheets.snapshot(c,state['session']['id'],1)['session']['version']==0
        assert controls.heads(c,{('OFFER',key)})[('OFFER',key)]['payload']['pack_size']==10


def test_prior_half_up_worksheet_survives_quantity_edits_without_recalculation(sheet,monkeypatch):
    from copy import deepcopy
    e,state=sheet;row=deepcopy(state['rows'][0]);row['id']=str(uuid4())
    row['advice_units'][0]='6';row['packs'][0]=1
    # Create the historical fixture through the prior conversion contract rather
    # than weakening immutable-original guards or altering any saved evidence.
    with monkeypatch.context() as m:
        m.setattr(worksheets,'final_pack_conversion',lambda need,size:0 if not need else max(1,(2*need.numerator+size*need.denominator)//(2*size*need.denominator)))
        with e.begin() as c:
            old=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Prior algorithm worksheet',rows=[row])
    change=pack_change(old,5);change[0]['value']['packs'][0]=1
    change[0]['value']['packs'][1]=9;change[0]['value']['touched'][1]=True
    receipt=save(e,old,change)
    with e.connect() as c:
        saved=worksheets.snapshot(c,old['session']['id'],1)
        assert saved['rows'][0]['packs']==[1,9,3,0]
        assert saved['rows'][0]['original']['packs'][0]==1


def test_paused_configuration_cannot_be_changed_through_pack_edit(sheet):
    e,state=sheet
    with pytest.raises(controls.InvalidPolicy,match='configuration is paused'):
        save(e,state,pack_change(state,7),pack_authority_enabled=False)
    with e.connect() as c:
        assert worksheets.snapshot(c,state['session']['id'],1)['session']['version']==0


def test_unassigned_uwell_pack_and_quantity_edits_persist_without_creating_offer(sheet):
    from copy import deepcopy
    e,state=sheet;row=deepcopy(state['rows'][0]);row.update(id=str(uuid4()),name='Uwell Crown 5 Coil',supplier_id=None,pack_size=None,offers={},packs=[None]*4,touched=[False]*4,advice_units=[None]*4)
    with e.begin() as c:
        head_before=c.scalar(text('select count(*) from purchasing_v4.control_revision'))
        unassigned=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Unassigned pack regression',rows=[row])
    change=pack_change(unassigned,2);receipt=save(e,unassigned,change)
    with e.connect() as c:
        current=worksheets.snapshot(c,unassigned['session']['id'],1)
        assert current['rows'][0]['pack_size']==2 and current['rows'][0]['supplier_id'] is None
        assert current['rows'][0]['original']['pack_size'] is None
        assert c.scalar(text('select count(*) from purchasing_v4.control_revision'))==head_before
    values={k:current['rows'][0][k] for k in worksheets.FIELDS};values['packs']=[1,2,3,4];values['touched']=[True]*4
    save(e,current,[dict(id=row['id'],value=values)],version=receipt['version'])
    with e.connect() as c:assert worksheets.snapshot(c,current['session']['id'],1)['rows'][0]['packs']==[1,2,3,4]


def test_pack_authority_conflict_can_be_explicitly_resolved_once(sheet):
    e,state=sheet;row=state['rows'][0];key=row['variation_id']+':'+row['supplier_id']
    with e.begin() as c:
        h=controls.heads(c,{('OFFER',key)})[('OFFER',key)]
        controls.apply(c,actor_id=1,command_id=str(uuid4()),reason='Other worksheet pack edit',changes=[dict(kind='OFFER',key=key,expected_version=h['version'],value=dict(h['payload'],pack_size=10))])
    with pytest.raises(controls.Conflict):save(e,state,pack_change(state,7))
    cid=str(uuid4());change=pack_change(state,7)
    receipt=save(e,state,change,command=cid,expected_pack_sizes={row['id']:10})
    assert save(e,state,change,command=cid,expected_pack_sizes={row['id']:10})['replayed']
    with e.connect() as c:
        saved=worksheets.snapshot(c,state['session']['id'],1)
        assert saved['rows'][0]['pack_size']==7 and saved['rows'][0]['pack_sizes'][row['supplier_id']]==7
    save(e,saved,pack_change(saved,8),version=receipt['version'],expected_pack_sizes={row['id']:7})
