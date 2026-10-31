"""Atomic owner-confirmed exclusion without changing manual or financial authority."""
from uuid import uuid4
from copy import deepcopy
import pytest
from sqlalchemy import text
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,pytestmark,save,edit
from test_purchasing_v4_controls import execute,change
from app.purchasing_v4 import worksheets,controls
from app.purchasing_v4.reconciliation import public_fingerprints


def stop(e,s,**kw):
    request=dict(actor_id=1,session_id=s['session']['id'],command_id=str(uuid4()),expected_version=0,row_ids=[r['id'] for r in s['rows']]);request.update(kw)
    with e.begin() as c:return worksheets.no_reorder(c,**request)


def test_no_reorder_removes_only_selected_draft_preserves_manual_public_and_coverage(sheet):
    e,s=sheet;vid=s['rows'][0]['variation_id'];cid=str(uuid4())
    execute(e,[change('PRODUCT',vid,dict(variation_id=vid,no_reorder=False,coverage=dict(reorder_weeks=3,target_weeks=6)))])
    with e.begin() as c:
        other=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Untouched',rows=[s['rows'][0]['original']])
    with reading(e) as c:
        before=public_fingerprints(c);manual=c.execute(text("SELECT record_key,payload,version FROM purchasing_v4.control_head WHERE kind='MANUAL' ORDER BY record_key")).all()
    result=stop(e,s,command_id=cid)
    assert result['variation_count']==1 and result['version']==1
    assert stop(e,s,command_id=cid)['replayed']
    with reading(e) as c:
        current=worksheets.snapshot(c,s['session']['id'],1)
        assert current['rows'][0]['removed'] and current['rows'][0]['packs']==s['rows'][0]['packs']
        assert current['rows'][0]['original']==s['rows'][0]['original']
        assert not worksheets.snapshot(c,other['session']['id'],1)['rows'][0]['removed']
        head=controls.heads(c,{('PRODUCT',vid)})[('PRODUCT',vid)]
        assert head['payload']==dict(variation_id=vid,no_reorder=True,coverage=dict(reorder_weeks=3,target_weeks=6))
        assert head['version']==2
        assert public_fingerprints(c)==before
        assert c.execute(text("SELECT record_key,payload,version FROM purchasing_v4.control_head WHERE kind='MANUAL' ORDER BY record_key")).all()==manual
        assert c.scalar(text("SELECT count(*) FROM purchasing_v4.control_command WHERE reason LIKE 'Owner confirmed No Reorder from worksheet %'"))==1
    with pytest.raises(controls.Conflict):stop(e,s,command_id=cid,expected_version=1)


@pytest.mark.parametrize('failure',['stale','missing','duplicate','other-owner'])
def test_invalid_selection_has_no_partial_policy_or_draft_changes(sheet,failure):
    e,s=sheet;kw={}
    if failure=='stale':kw['expected_version']=1
    if failure=='missing':kw['row_ids']=[s['rows'][0]['id'],str(uuid4())]
    if failure=='duplicate':kw['row_ids']=[s['rows'][0]['id']]*2
    if failure=='other-owner':kw['actor_id']=2
    with pytest.raises((controls.InvalidPolicy,controls.Conflict,PermissionError)):stop(e,s,**kw)
    with e.connect() as c:
        assert worksheets.snapshot(c,s['session']['id'],1)['session']['version']==0
        assert not controls.heads(c,{('PRODUCT',s['rows'][0]['variation_id'])})


def test_already_excluded_product_can_be_removed_without_duplicate_policy_revision(sheet):
    e,s=sheet;vid=s['rows'][0]['variation_id']
    execute(e,[change('PRODUCT',vid,dict(variation_id=vid,no_reorder=True,coverage=None))])
    stop(e,s)
    with e.connect() as c:assert controls.heads(c,{('PRODUCT',vid)})[('PRODUCT',vid)]['version']==1


def test_policy_and_draft_rollback_together_when_removal_fails(sheet,monkeypatch):
    e,s=sheet
    def fail(*a,**kw):raise controls.Conflict('Injected row update conflict')
    monkeypatch.setattr(worksheets,'patch',fail)
    with pytest.raises(controls.Conflict):stop(e,s)
    with e.connect() as c:
        assert not controls.heads(c,{('PRODUCT',s['rows'][0]['variation_id'])})
        assert worksheets.snapshot(c,s['session']['id'],1)['session']['version']==0


def test_more_than_one_control_batch_is_atomic_and_replayable(sheet,monkeypatch):
    e,s=sheet;rows=[]
    with e.begin() as c:
        for _ in range(101):
            vid=str(uuid4());c.execute(text('INSERT INTO purchasing_v4.variation(id,product_id,created_by_run) SELECT :id,product_id,created_by_run FROM purchasing_v4.variation LIMIT 1'),{'id':vid})
            row=deepcopy(s['rows'][0]['original']);row.update(id=str(uuid4()),variation_id=vid);rows.append(row)
        bulk=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Synthetic 101 rows',rows=rows)
    apply=controls.apply;calls=[]
    def fail_second(*a,**kw):
        calls.append(len(kw['changes']))
        if len(calls)==2:raise controls.Conflict('Injected second batch failure')
        return apply(*a,**kw)
    monkeypatch.setattr(controls,'apply',fail_second)
    with pytest.raises(controls.Conflict):stop(e,bulk)
    assert calls==[100,1]
    with e.connect() as c:
        assert not controls.heads(c,{('PRODUCT',r['variation_id']) for r in rows})
        assert worksheets.snapshot(c,bulk['session']['id'],1)['session']['version']==0
    monkeypatch.setattr(controls,'apply',apply);cid=str(uuid4())
    assert stop(e,bulk,command_id=cid)['variation_count']==101
    assert stop(e,bulk,command_id=cid)['replayed']
    with e.connect() as c:
        assert all(r['removed'] for r in worksheets.snapshot(c,bulk['session']['id'],1)['rows'])
        assert all(h['version']==1 for h in controls.heads(c,{('PRODUCT',r['variation_id']) for r in rows}).values())


def test_old_open_copy_can_stop_reordering_without_overwriting_newer_quantities(sheet):
    e,s=sheet;row=s['rows'][0];value=worksheets.editable(row);value.update(packs=[9]*4,touched=[True]*4)
    with e.begin() as c:worksheets.patch(c,actor_id=1,session_id=s['session']['id'],command_id=str(uuid4()),expected_version=0,changes=[dict(id=row['id'],value=value)])
    result=stop(e,s,expected_version=0);assert result['version']==2
    with e.connect() as c:
        current=worksheets.snapshot(c,s['session']['id'],1)['rows'][0]
        assert current['removed'] and current['packs']==[9]*4 and current['touched']==[True]*4
        assert current['original']==row['original']


def test_already_removed_row_can_still_get_the_explicit_future_no_reorder_policy(sheet):
    e,s=sheet;row=s['rows'][0];value=worksheets.editable(row);value['removed']=True
    with e.begin() as c:worksheets.patch(c,actor_id=1,session_id=s['session']['id'],command_id=str(uuid4()),expected_version=0,changes=[dict(id=row['id'],value=value)])
    result=stop(e,s,expected_version=0);assert result['version']==2
    with e.connect() as c:
        assert controls.heads(c,{('PRODUCT',row['variation_id'])})[('PRODUCT',row['variation_id'])]['payload']['no_reorder']
        assert worksheets.snapshot(c,s['session']['id'],1)['rows'][0]['packs']==row['packs']
