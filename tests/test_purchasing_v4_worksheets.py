"""Disposable PostgreSQL worksheet CAS, recovery, generation and preservation contracts."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from datetime import timedelta
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_purchasing_v4_controls import ready,engine,template,reading,pytestmark,configure,change,global_policy,manual,execute,AS_OF,AGE,profile,offer
from app.purchasing_v4 import worksheets,controls
from app.purchasing_v4.schema import migrate_worksheets,migrate
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure

@pytest.fixture
def sheet(ready):
    engine,vid,sid=configure(ready,history=True)
    with engine.begin() as c:
        migrate_worksheets(c)
        c.execute(text("UPDATE stores SET name=CASE id WHEN 1 THEN 'Andresen' ELSE 'HWY 99' END"))
        c.execute(text("INSERT INTO stores(id,name,square_location_id,active) VALUES (3,'SR 503','FIXTURE_LOC3',true),(4,'Longview','FIXTURE_LOC4',true)"))
        rows=worksheets.catalog_rows(c,[vid],'synthetic-fixture-source');rows[0].update(packs=[1,2,3,0],touched=[False]*4,advice_units=['5','10','15','0'])
        result=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Deterministic worksheet',rows=rows)
    return engine,result

def edit(sheet,value=7):
    row=sheet['rows'][0];working={k:row[k] for k in worksheets.FIELDS};working['packs']=[value]*4;working['touched']=[True]*4;return [dict(id=row['id'],value=working)]

def save(engine,sheet,changes,version=0,command=None,**extra):
    with engine.begin() as c:return worksheets.patch(c,actor_id=1,session_id=sheet['session']['id'],command_id=command or str(uuid4()),expected_version=version,changes=changes,**extra)

def test_additive_schema_and_patches_preserve_public_and_pinned_original(sheet):
    engine,state=sheet
    with reading(engine) as c:before=public_fingerprints(c);structure=public_structure(c)
    receipt=save(engine,state,edit(state));assert receipt['version']==1
    with reading(engine) as c:
        current=worksheets.snapshot(c,state['session']['id'],1)
        assert current['rows'][0]['packs']==[7]*4
        assert current['rows'][0]['original']==state['rows'][0]['original']
        assert public_fingerprints(c)==before and public_structure(c)==structure
    with engine.begin() as c:migrate(c)


def test_concurrent_aggregate_commands_are_atomic_and_lost_response_replays(sheet):
    engine,state=sheet;ids=[str(uuid4()),str(uuid4())]
    def run(i):
        try:return save(engine,state,edit(state,i+3),command=ids[i])
        except controls.Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,[0,1]))
    assert results.count('conflict')==1
    index=0 if results[0]!='conflict' else 1
    assert save(engine,state,edit(state,index+3),command=ids[index])['replayed']
    with pytest.raises(controls.Conflict):save(engine,state,edit(state,99),command=ids[index])


def test_invalid_bulk_nothing_applies_and_no_history_is_loaded_on_edit(sheet,monkeypatch):
    engine,state=sheet
    import app.purchasing_v4.calculation_source as source
    monkeypatch.setattr(source,'load_snapshot',lambda *a,**k:pytest.fail('History must not run on ordinary edits'))
    changes=edit(state);changes.append(dict(id=str(uuid4()),value=changes[0]['value']))
    with pytest.raises(controls.InvalidPolicy):save(engine,state,changes)
    with engine.connect() as c:assert worksheets.snapshot(c,state['session']['id'],1)['session']['version']==0
    save(engine,state,edit(state,0))
    with engine.connect() as c:assert not worksheets.snapshot(c,state['session']['id'],1)['rows'][0]['removed']


def test_default_supplier_and_draft_changes_share_one_atomic_transaction(sheet):
    engine,state=sheet;vid=state['rows'][0]['variation_id'];sid=state['rows'][0]['supplier_id']
    bad=[dict(variation_id=vid,supplier_id=str(uuid4()),expected_version=1)]
    with pytest.raises(controls.InvalidPolicy):save(engine,state,edit(state),defaults=bad)
    with engine.connect() as c:assert worksheets.snapshot(c,state['session']['id'],1)['session']['version']==0
    result=save(engine,state,edit(state),defaults=[dict(variation_id=vid,supplier_id=sid,expected_version=1)])
    assert result['inverse_defaults'][0]['expected_version']==2
    with pytest.raises(controls.Conflict):save(engine,state,result['inverse'],version=1,defaults=[dict(variation_id=vid,supplier_id=sid,expected_version=1)])
    save(engine,state,result['inverse'],version=1,defaults=result['inverse_defaults'])


def test_discard_preserves_audit_originals_and_never_changes_commitments(sheet):
    engine,state=sheet
    with reading(engine) as c:before=public_fingerprints(c)
    save(engine,state,[],discard=True)
    with engine.connect() as c:
        assert worksheets.snapshot(c,state['session']['id'],1)['session']['state']=='DISCARDED'
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.worksheet_command')).scalar_one()==1
    with pytest.raises(controls.Conflict):save(engine,state,edit(state),version=1)
    with reading(engine) as c:assert public_fingerprints(c)==before


def test_generation_uses_single_accepted_engine_and_requires_explicit_approval(sheet):
    engine,state=sheet
    with pytest.raises(controls.InvalidPolicy):worksheets.prepare_generation(engine,actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=AGE)
    execute(engine,[change('GLOBAL','global',dict(global_policy(),status='OWNER_APPROVED'),1)])
    rows,pin=worksheets.prepare_generation(engine,actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=AGE)
    assert rows and pin['input_digest'] and pin['configuration_digest']
    assert len(rows[0]['packs'])==4 and all(v is None or type(v) is int for v in rows[0]['packs'])
    assert any(v is None for r in rows for v in r['packs'])


def test_worksheet_original_and_receipts_reject_tampering(sheet):
    engine,state=sheet;save(engine,state,edit(state))
    for sql in ["UPDATE purchasing_v4.worksheet_row SET original='{}'::jsonb", "DELETE FROM purchasing_v4.worksheet_command",'TRUNCATE purchasing_v4.worksheet_command']:
        with pytest.raises(DBAPIError):
            with engine.begin() as c:c.execute(text(sql))


def test_multi_draft_disposal_is_atomic_versioned_and_replayable(sheet):
    engine,state=sheet;sid=state['session']['id']
    with engine.begin() as c:other=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Second fixture draft',rows=[])
    selections=[dict(id=sid,expected_version=0),dict(id=other['session']['id'],expected_version=1)]
    with pytest.raises(controls.Conflict):
        with engine.begin() as c:worksheets.discard_selected(c,actor_id=1,command_id=str(uuid4()),selections=selections)
    with engine.connect() as c:assert worksheets.snapshot(c,sid,1)['session']['state']=='EDITABLE'
    selections[1]['expected_version']=0;command=str(uuid4())
    with engine.begin() as c:result=worksheets.discard_selected(c,actor_id=1,command_id=command,selections=selections)
    with engine.begin() as c:assert worksheets.discard_selected(c,actor_id=1,command_id=command,selections=selections)['replayed']
    assert set(result['discarded_ids'])=={sid,other['session']['id']}


def test_interrupted_worksheet_migration_rolls_back_and_recovers_forward(ready):
 engine,vid,sid=ready
 from sqlalchemy import event
 def interrupt(conn,cursor,statement,parameters,context,executemany):
  marker='CREATE TRIGGER v4_worksheet_owner_command_immutable'
  if marker in statement:statement=statement.replace(marker,'SELECT 1/0; '+marker)
  return statement,parameters
 with reading(engine) as c:before=public_fingerprints(c)
 event.listen(engine,'before_cursor_execute',interrupt,retval=True)
 try:
  with pytest.raises(DBAPIError):
   with engine.begin() as c:migrate_worksheets(c)
 finally:event.remove(engine,'before_cursor_execute',interrupt)
 with engine.connect() as c:
  assert c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()=='p3_0003'
  assert c.execute(text("SELECT to_regclass('purchasing_v4.worksheet_generation')")).scalar_one() is None
 with engine.begin() as c:migrate_worksheets(c)
 with reading(engine) as c:
  assert c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()=='p4_0003'
  assert public_fingerprints(c)==before


def test_server_rejects_forged_untouched_advice_and_retains_exact_costs(sheet):
    from decimal import Decimal,localcontext
    engine,state=sheet;changes=edit(state);changes[0]['value']['touched']=[False]*4
    with pytest.raises(controls.InvalidPolicy):save(engine,state,changes)
    record=state['rows'][0];supplier=record['supplier_id'];record['offers'][supplier]['expected_unit_cost']='0.123456789012345678901234567890123456789'
    record['packs']=[1000000]*4;record['pack_size']=1000000
    total=worksheets.totals([record])
    with localcontext() as context:
        context.prec=100
        assert Decimal(total['known_cost'])==Decimal(record['offers'][supplier]['expected_unit_cost'])*4000000000000


def test_store_bindings_pin_approved_order_and_survive_registry_changes(sheet):
 engine,state=sheet
 assert [s['name'] for s in state['session']['stores']]==['Andresen','HWY 99','SR 503','Longview']
 with engine.begin() as c:
  c.execute(text("UPDATE stores SET active=false,name='Renamed existing store' WHERE id=3"))
 with engine.connect() as c:assert worksheets.snapshot(c,state['session']['id'],1)['session']['stores']==state['session']['stores']
 with pytest.raises(DBAPIError):
  with engine.begin() as c:c.execute(text("UPDATE purchasing_v4.worksheet SET stores='[]'::jsonb"))


def test_global_default_command_identity_is_namespaced_by_worksheet(sheet):
 engine,state=sheet;vid=state['rows'][0]['variation_id'];supplier=state['rows'][0]['supplier_id'];command=str(uuid4())
 with engine.begin() as c:second=worksheets.create(c,actor_id=1,command_id=str(uuid4()),title='Independent worksheet intent',rows=[dict(state['rows'][0],id=str(uuid4()))])
 save(engine,state,edit(state),command=command,defaults=[dict(variation_id=vid,supplier_id=supplier,expected_version=1)])
 # Reusing a worksheet-scoped UUID elsewhere cannot replay the first worksheet's global policy command.
 with pytest.raises(controls.Conflict):save(engine,second,edit(second),command=command,defaults=[dict(variation_id=vid,supplier_id=supplier,expected_version=1)])
 with engine.connect() as c:assert worksheets.snapshot(c,second['session']['id'],1)['session']['version']==0


def test_approved_decimal_costs_are_canonical_in_the_browser_projection_without_rewriting_the_record(sheet):
 engine,state=sheet;record=state['rows'][0];vid=record['variation_id'];sid=record['supplier_id'];value=record['offers'][sid]
 # Phase3 accepts exact Decimal syntax. Normalize serialization while retaining the original approved record.
 execute(engine,[change('OFFER',vid+':'+sid,{k:v for k,v in dict(value,expected_unit_cost='+1_000.000',cost_provenance='Explicit synthetic decimal syntax').items() if k in {'variation_id','supplier_id','approved','pack_size','expected_unit_cost','cost_provenance','vendor_sku'}},1)])
 with engine.connect() as c:
  projected=worksheets.catalog_rows(c,[vid],'synthetic-fixture-source')[0]['offers'][sid]
  assert projected['expected_unit_cost']=='1000.000' and projected['recorded_unit_cost']=='+1_000.000'
  assert controls.heads(c,{('OFFER',vid+':'+sid)})[('OFFER',vid+':'+sid)]['payload']['expected_unit_cost']=='+1_000.000'


def test_policy_labels_preserve_manual_authority_when_calculation_is_held():
 from test_purchasing_v4_calculation import row
 from app.purchasing_v4.calculation import ManualRule
 assert worksheets.policy_label(row(manual=ManualRule('fixture',5,20),no_reorder=True))=='Manual'
 assert worksheets.policy_label(row(policy_issues=('EXPLICIT_INACTIVE_MANUAL_POLICY',)))=='Manual inactive'
 assert worksheets.policy_label(row(policy_issues=('UNRESOLVED_FROZEN_LEGACY_MANUAL_POLICY',)))=='Manual review'
 assert worksheets.policy_label(row())=='Automatic'
