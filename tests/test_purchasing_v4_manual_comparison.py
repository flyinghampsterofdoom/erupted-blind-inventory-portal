"""Real PostgreSQL comparisons, guarded explicit conversion and reversible audit."""
from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_purchasing_v4_postgres import template,engine,reading,AS_OF,pytestmark
from test_purchasing_v4_controls import ready,configure,execute,change,global_policy,offer,profile
from app.purchasing_v4 import controls,manual_comparison as mc
from app.purchasing_v4.reconciliation import public_fingerprints


def prepared(ready):
    engine,vid,sid=configure(ready,history=True)
    execute(engine,[change('GLOBAL','global',dict(global_policy(),status='OWNER_APPROVED',max_source_age_seconds=172800),1),
                   change('OFFER',vid+':'+sid,offer(vid,sid,cost='10'),1)])
    return engine,vid,sid


def calculate(engine,**kwargs):
    with reading(engine) as c:return mc.calculate(c,actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,**kwargs)


def test_all_rules_same_snapshot_units_costs_and_no_mutations(ready):
    engine,vid,sid=prepared(ready)
    with reading(engine) as c:before=public_fingerprints(c)
    result=calculate(engine)
    row=next(r for r in result['rows'] if r['key']==vid+':1')
    assert row['manual_par']==5 and row['manual_level']==20 and row['pack_size']==5
    assert row['eligible_for_conversion'] and row['selected_for_conversion'] is False
    assert row['manual_order']%5==row['engine_order']%5==0
    assert Decimal(row['cost_impact'])==Decimal('10')*row['difference']
    assert result['summary']['net_difference']==row['cost_impact']
    with engine.begin() as c:mc.save(c,result)
    with reading(engine) as c:
        assert public_fingerprints(c)==before
        assert controls.heads(c,{('MANUAL',vid+':1')})[('MANUAL',vid+':1')]['payload']['state']=='ACTIVE'
        for sort in mc.SORTS:
            page=mc.page(c,actor_id=1,report_id=result['id'],sort=sort)
            assert page['total']==1 and len(page['rows'])==1
        assert mc.page(c,actor_id=1,report_id=result['id'],search='Fixture',store_id='1',qualification='qualified')['total']==1
        assert mc.page(c,actor_id=1,report_id=result['id'],store_id='2')['total']==0
        assert mc.page(c,actor_id=1,report_id=result['id'],search='%')['total']==0
        assert 'rows' not in mc.metadata(c,1,result['id']) and 'dependency_versions' not in mc.metadata(c,1,result['id'])


@pytest.mark.parametrize('argument,value',[('sort','cost_impact; DROP TABLE principals'),('direction','asc;--'),('qualification','yes'),('flag','made_up'),('limit',101),('offset',-1)])
def test_filters_fail_closed(ready,argument,value):
    engine,_,_=ready
    with engine.connect() as c:
        with pytest.raises(ValueError):mc.page(c,actor_id=1,report_id=str(uuid4()),**{argument:value})


def test_unknown_supplier_cost_or_stock_never_converts(ready):
    engine,vid,sid=prepared(ready)
    execute(engine,[change('OFFER',vid+':'+sid,offer(vid,sid,cost=None),2)])
    result=calculate(engine);assert not result['rows'][0]['eligible_for_conversion']
    assert result['rows'][0]['engine_order'] is None and result['summary']['comparable_rules']==0
    with engine.begin() as c:c.execute(text('DELETE FROM ordering_current_inventory'))
    result=calculate(engine);assert result['rows'][0]['on_hand'] is None
    assert result['rows'][0]['manual_order'] is None and not result['rows'][0]['eligible_for_conversion']


def test_unresolved_legacy_identity_is_counted_visible_and_blocked(ready):
    engine,_,_=prepared(ready)
    from app.purchasing_v4.management_read import freeze_legacy_manual
    with engine.begin() as c:
        c.execute(text("INSERT INTO par_levels(vendor_id,sku,store_id,par_source,manual_par_level,manual_stock_up_level) VALUES(2,'MISSING',NULL,'MANUAL',5,20)"))
        freeze_legacy_manual(c,source_namespace='synthetic-fixture-source')
    result=calculate(engine)
    assert result['manual_records_evaluated']==1
    row=next(r for r in result['rows'] if r['key'].startswith('legacy:'))
    assert row['store_id'] is None and row['eligible_for_conversion'] is False and len(row['legacy_ids'])==1


def test_only_fresh_selected_preview_can_convert_with_replay_and_restore(ready):
    engine,vid,_=prepared(ready);key=vid+':1'
    report=calculate(engine)
    with engine.begin() as c:mc.save(c,report)
    from app.purchasing_v4.policy_projection import use_automatic
    with pytest.raises(ValueError,match='fresh impact preview'):
        with engine.begin() as c:use_automatic(c,actor_id=1,command_id=str(uuid4()),reason='Old endpoint cannot bypass impact preview',check_id=report['id'],selected_keys=[key])
    def confirm(preview,keys,command=None):
        with engine.begin() as c:return mc.confirm(c,actor_id=1,command_id=command or str(uuid4()),reason='Explicit synthetic owner confirmation',preview_id=preview,selected_keys=keys)
    with pytest.raises(ValueError):confirm(report['id'],[key])
    preview=calculate(engine,selected_keys=[key])
    with engine.begin() as c:mc.save(c,preview)
    with pytest.raises(controls.Conflict,match='expired'):confirm(preview['id'],[key])
    preview['id']=str(uuid4());preview['as_of']=datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:mc.save(c,preview)
    with pytest.raises(ValueError):confirm(preview['id'],[])
    command=str(uuid4());result=confirm(preview['id'],[key],command)
    assert confirm(preview['id'],[key],command)['replayed']
    with engine.begin() as c:
        assert controls.heads(c,{('MANUAL',key)})[('MANUAL',key)]['payload']['state']=='AUTOMATIC'
        controls.restore(c,actor_id=1,command_id=str(uuid4()),reason='Authorized owner reverses conversion',target_command=result['command_id'],expected={'MANUAL:'+key:2})
        assert controls.heads(c,{('MANUAL',key)})[('MANUAL',key)]['payload']['state']=='ACTIVE'


def test_changed_evidence_rejects_confirm_and_non_owner_denied(ready):
    engine,vid,_=prepared(ready);key=vid+':1';preview=calculate(engine,selected_keys=[key]);preview['as_of']=datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        mc.save(c,preview);c.execute(text('UPDATE ordering_current_inventory SET counted_quantity=counted_quantity+1'))
    with pytest.raises(controls.Conflict,match='Evidence changed'):
        with engine.begin() as c:mc.confirm(c,actor_id=1,command_id=str(uuid4()),reason='Stale preview',preview_id=preview['id'],selected_keys=[key])
    with pytest.raises(PermissionError):
        with engine.connect() as c:mc.metadata(c,999)


def test_totals_do_not_compare_unknown_or_double_count_noncomparable_costs():
    rows=[dict(eligible_for_conversion=True,history_qualified=True,manual_cost='100',engine_cost='60',cost_impact='-40',manual_order=10,unit_cost='10'),
          dict(eligible_for_conversion=True,history_qualified=True,manual_cost='10',engine_cost='30',cost_impact='20',manual_order=1,unit_cost='10'),
          dict(eligible_for_conversion=False,history_qualified=False,manual_cost='70',engine_cost=None,cost_impact=None,manual_order=7,unit_cost='10')]
    s=mc.totals(rows)
    assert s['current_manual_cost']=='180' and s['comparable_manual_cost']=='110'
    assert s['equivalent_engine_cost']=='90' and s['gross_reductions']=='40' and s['gross_increases']=='20' and s['net_difference']=='-20'


def test_duplicate_frozen_records_compare_once_and_new_conversion_is_reversible(ready):
    engine,vid,sid=ready;key=vid+':1'
    execute(engine,[change('GLOBAL','global',dict(global_policy(),status='OWNER_APPROVED',max_source_age_seconds=172800)),
        change('SUPPLIER',sid,dict(profile(),supplier_id=sid)),change('OFFER',vid+':'+sid,offer(vid,sid,cost='10')),
        change('ASSIGNMENT',vid,dict(variation_id=vid,supplier_id=sid))])
    from app.purchasing_v4.management_read import freeze_legacy_manual
    with engine.begin() as c:
        c.execute(text("UPDATE ordering_current_inventory SET counted_quantity=20"))
        c.execute(text("UPDATE purchasing_history_coverage SET start_at='2026-08-01T00:00Z'"))
        c.execute(text("INSERT INTO vendor_sku_configs(id,vendor_id,sku,square_variation_id,unit_cost,pack_size,is_default_vendor,active) VALUES(2,2,'SAME','FIXTURE_V1',10,5,false,true)"))
        c.execute(text("INSERT INTO par_levels(vendor_id,sku,store_id,par_source,manual_par_level,manual_stock_up_level) VALUES(1,'SAME',1,'MANUAL',5,20),(2,'SAME',1,'MANUAL',5,20)"))
        freeze_legacy_manual(c,source_namespace='synthetic-fixture-source')
    result=calculate(engine)
    assert result['manual_records_evaluated']==2 and len(result['rows'])==1
    assert len(result['rows'][0]['legacy_ids'])==2 and result['rows'][0]['expected_version']==0
    assert result['rows'][0]['eligible_for_conversion']
    preview=calculate(engine,selected_keys=[key]);preview['as_of']=datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        mc.save(c,preview)
        result=mc.confirm(c,actor_id=1,command_id=str(uuid4()),reason='Explicit owner selection of verified frozen manual rule',preview_id=preview['id'],selected_keys=[key])
        assert controls.heads(c,{('MANUAL',key)})[('MANUAL',key)]['payload']['state']=='AUTOMATIC'
        controls.restore(c,actor_id=1,command_id=str(uuid4()),reason='Owner restores original frozen authority',target_command=result['command_id'],expected={'MANUAL:'+key:2})
    after=calculate(engine)
    assert after['rows'][0]['manual_par']==5 and after['rows'][0]['manual_level']==20
    assert after['manual_records_evaluated']==2 and after['summary']['rules']==1
