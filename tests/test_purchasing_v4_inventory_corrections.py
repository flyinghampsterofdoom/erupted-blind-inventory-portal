"""October 30 owner-approved derived inventory and default freshness contracts."""
from datetime import timedelta
from dataclasses import replace
import pytest
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,pytestmark,AS_OF
from test_purchasing_v4_controls import execute,change,global_policy
from test_purchasing_v4_calculation import row,known,run
from app.purchasing_v4 import controls,worksheets
from app.purchasing_v4.calculation import ManualRule,Supply,SupplyLot,Deduction
from app.purchasing_v4.contracts import QuantityEvidence,EvidenceState
from app.purchasing_v4.worksheet_inventory import inventory_view
from app.purchasing_v4.recommendation_status import describe

@pytest.mark.parametrize('quantity,position,order',[(-3,0,20),(0,0,20),(4,4,16),(15,15,0)])
def test_manual_inventory_preserves_raw_count_and_uses_derived_nonnegative(quantity,position,order):
    result=run(row(on_hand=known(quantity),history=(),manual=ManualRule('unchanged-manual',5,20)))
    assert result.observed_on_hand.value==quantity and result.inventory_position==position and result.raw_need_units==order
    assert ('NEGATIVE_OBSERVED_ON_HAND' in result.reasons)==(quantity<0)
    assert 'NEGATIVE_INVENTORY_CANDIDATE_GATE' not in result.reasons

def test_clamped_stock_preserves_supply_reconciliation_and_missing_counts():
    incoming=Supply((SupplyLot('open','VENDOR_OPEN',known(4),False),),(),(),True,'verified')
    r=row(on_hand=known(-3),manual=ManualRule('manual',5,20),supply=incoming)
    assert run(r).inventory_position==4 and run(r).raw_need_units==16
    for supply in (replace(incoming,coherent_with_on_hand=False),replace(incoming,outward_reserved=(Deduction('hold',known(1),True),))):
        assert run(replace(r,supply=supply)).raw_need_units is None
    unknown=QuantityEvidence(EvidenceState.UNKNOWN,None,'square','NO_OBSERVATION')
    assert run(replace(r,on_hand=unknown)).raw_need_units is None

def test_freshness_default_is_saved_owner_value_and_explicit_zero_is_preserved(sheet):
    e,state=sheet
    execute(e,[change('GLOBAL','global',dict(global_policy(),max_source_age_seconds=129600),1)])
    with reading(e) as c:
        assert controls.evidence_age(c)==timedelta(days=1.5)
        result=inventory_view(c,session_id=state['session']['id'],actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF)
        assert result['max_age_seconds']==129600
        assert result['rows'][state['rows'][0]['id']][0]['state']=='KNOWN'
        assert any(x['reason']=='NO_OBSERVATION' for row in result['rows'].values() for x in row)
        assert controls.evidence_age(c,timedelta())==timedelta()
        assert worksheets.snapshot(c,state['session']['id'],1)['session']['version']==0
    execute(e,[change('GLOBAL','global',dict(global_policy(),max_source_age_seconds=129600,status='OWNER_APPROVED'),2)])
    implicit,pin=worksheets.prepare_generation(e,actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF)
    explicit,pinned=worksheets.prepare_generation(e,actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=timedelta(days=1.5))
    assert pin==pinned
    assert [(r['variation_id'],r['packs']) for r in implicit]==[(r['variation_id'],r['packs']) for r in explicit]

def test_readable_status_separates_stock_history_and_no_reorder():
    assert describe('EXCLUDED_NO_REORDER',['GLOBAL_NO_REORDER'])['state']=='NO_REORDER'
    message=describe('UNRESOLVED_EVIDENCE',['INSUFFICIENT_CONFIRMED_AVAILABLE_DAYS'])
    assert message['state']=='UNRESOLVED' and 'current inventory is still shown' in message['explanation']
    assert 'supplier' in describe('UNRESOLVED_PACK_CONFIGURATION',['UNRESOLVED_PRIMARY_SUPPLIER_EVIDENCE'])['explanation']
