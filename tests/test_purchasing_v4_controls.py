"""Real disposable PostgreSQL owner controls, preservation and concurrent writes."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date,timedelta
from uuid import uuid4
import json
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_purchasing_v4_postgres import template,engine,reading,get_plan,AS_OF,AGE,pytestmark
from app.purchasing_v4.schema import migrate,migrate_controls
from app.purchasing_v4.storage import apply_plan
from app.purchasing_v4 import controls
from app.purchasing_v4.management_read import catalog_projection,supplier_context,list_catalog,list_manual
from app.purchasing_v4.policy_projection import calculate_history_check,save_history_check,use_automatic,project_rows
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure


def change(kind,key,value,version=0):return dict(kind=kind,key=key,value=value,expected_version=version)

def global_policy():
    p=json.load(open('tests/fixtures/purchasing_v4_phase2_profiles.json'))['profiles'][1]
    p.pop('id');p.update(status='DRAFT',lookback_days=56,recent_days=28,negative_inventory='abstain_on_negative');return p

def profile(name='Explicit owner supplier'):
    return dict(name=name,primary_contact=None,secondary_contact=None,email=None,phone=None,address=None,account_number=None,payment_terms=None,order_minimum=None,internal_notes=None,
      active=True,confirmation_enabled=False,automatic_spreadsheet_email=False,automatic_po_email=False,preferred_po_format='PDF',use_vendor_sku=False)

def offer(vid,sid,pack=5,cost=None):return dict(variation_id=vid,supplier_id=sid,approved=True,pack_size=pack,expected_unit_cost=cost,cost_provenance='Owner reviewed; cost unknown' if cost is None else 'Explicit owner entered expected unit cost',vendor_sku='000123')

def manual(vid,store=1,state='ACTIVE',par=5,level=20):return dict(variation_id=vid,store_id=store,state=state,par_units=par,level_units=level)

def execute(engine,changes,*,actor=1,command=None):
    with engine.begin() as c:return controls.apply(c,actor_id=actor,command_id=command or str(uuid4()),reason='Explicit synthetic owner test decision',changes=changes)

@pytest.fixture
def ready(engine):
    plan=get_plan(engine)
    with engine.begin() as c:
        migrate(c);apply_plan(c,plan);migrate_controls(c);catalog_projection(c,source_namespace='synthetic-fixture-source')
    with engine.connect() as c:
        vid=c.execute(text("SELECT variation_id::text FROM purchasing_v4.variation_reference WHERE square_variation_id='FIXTURE_V1'")).scalar_one()
        sid=c.execute(text('SELECT supplier_id::text FROM purchasing_v4.supplier_reference WHERE legacy_vendor_id=1')).scalar_one()
    return engine,vid,sid


def configure(ready,*,history=False):
    engine,vid,sid=ready
    execute(engine,[change('GLOBAL','global',global_policy()),change('SUPPLIER',sid,dict(profile(),supplier_id=sid)),change('OFFER',vid+':'+sid,offer(vid,sid)),change('ASSIGNMENT',vid,dict(variation_id=vid,supplier_id=sid)),change('MANUAL',vid+':1',manual(vid))])
    if history:
        with engine.begin() as c:
            c.execute(text("UPDATE ordering_current_inventory SET counted_quantity=20"))
            c.execute(text("UPDATE purchasing_history_coverage SET start_at='2026-08-01T00:00Z'"))
    return engine,vid,sid


def test_additive_migration_preserves_public_and_foundation(engine):
    plan=get_plan(engine)
    with engine.begin() as c:migrate(c);apply_plan(c,plan)
    with reading(engine) as c:before=public_fingerprints(c);structure=public_structure(c);identities=c.execute(text('SELECT count(*) FROM purchasing_v4.variation')).scalar_one()
    with engine.begin() as c:migrate_controls(c);migrate_controls(c)
    with reading(engine) as c:
        assert public_fingerprints(c)==before and public_structure(c)==structure
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.variation')).scalar_one()==identities
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.control_head')).scalar_one()==0
        assert c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()=='p3_0003'


def test_global_category_inheritance_remove_override_and_restore(ready):
    engine,vid,sid=ready;first=execute(engine,[change('GLOBAL','global',global_policy())])
    cat=dict(category_id='CATEGORY',parameters=dict(increasing_sensitivity='1/4',decreasing_sensitivity='1/5'),coverage=None)
    execute(engine,[change('CATEGORY','CATEGORY',cat)])
    with engine.connect() as c:
        config=controls.configuration(c);assert config.resolve('CATEGORY')[0].model.increasing_sensitivity==controls.exact('1/4')
    new=dict(cat,parameters=dict(decreasing_sensitivity='1/5'))
    saved=execute(engine,[change('CATEGORY','CATEGORY',new,1)])
    with engine.connect() as c:
        config=controls.configuration(c);assert config.resolve('CATEGORY')[0].model.increasing_sensitivity==controls.exact('3/4')
        assert config.resolve('CATEGORY')[0].model.decreasing_sensitivity==controls.exact('1/5')
    with engine.begin() as c:controls.restore(c,actor_id=1,command_id=str(uuid4()),reason='Owner restores prior category controls',target_command=saved['command_id'],expected={'CATEGORY:CATEGORY':2})
    with engine.connect() as c:assert controls.heads(c,{('CATEGORY','CATEGORY')})[('CATEGORY','CATEGORY')]['version']==3


@pytest.mark.parametrize('kind,key,value',[
 ('GLOBAL','global',dict(global_policy(),negative_inventory='preserve_qualified_negative')),
 ('GLOBAL','global',dict(global_policy(),recent_influence=0.5)),
 ('CATEGORY','c',dict(category_id='c',parameters={'made_up':'1'},coverage=None))])
def test_invalid_settings_never_install_defaults(ready,kind,key,value):
    engine,_,_=ready
    if kind=='CATEGORY':execute(engine,[change('GLOBAL','global',global_policy())])
    with pytest.raises((ValueError,KeyError)):execute(engine,[change(kind,key,value)])


def test_manual_indefinite_units_inactive_and_no_generic_conversion(ready):
    engine,vid,_=ready;key=vid+':1'
    execute(engine,[change('MANUAL',key,manual(vid))])
    with pytest.raises(ValueError):execute(engine,[change('MANUAL',key,manual(vid,state='AUTOMATIC'),1)])
    with pytest.raises(ValueError):execute(engine,[change('MANUAL',key,manual(vid,par=20,level=20),1)])
    execute(engine,[change('MANUAL',key,manual(vid,state='INACTIVE'),1)])
    execute(engine,[change('GLOBAL','global',global_policy())])
    with engine.connect() as c:
        row=controls.heads(c,{('MANUAL',key)})[('MANUAL',key)];assert row['payload']['par_units']==5 and row['payload']['level_units']==20
        assert row['payload']['state']=='INACTIVE' and row['version']==2
        assert c.execute(text("SELECT count(*) FROM purchasing_v4.control_revision WHERE kind='MANUAL'")).scalar_one()==2


def test_local_supplier_global_assignment_unknown_cost_and_no_square_identity(ready):
    engine,vid,_=ready;sid=str(uuid4());command=str(uuid4())
    with engine.begin() as c:
        first=controls.create_supplier(c,actor_id=1,command_id=command,reason='Create locally owned supplier',supplier_id=sid,profile=profile())
    with engine.begin() as c:assert controls.create_supplier(c,actor_id=1,command_id=command,reason='Create locally owned supplier',supplier_id=sid,profile=profile())['replayed']
    execute(engine,[change('OFFER',vid+':'+sid,offer(vid,sid,pack=None)),change('ASSIGNMENT',vid,dict(variation_id=vid,supplier_id=sid))])
    with engine.connect() as c:
        context=supplier_context(c,vid);assert context['supplier_id']==sid and context['expected_unit_cost'] is None and context['pack_size'] is None
        assert c.execute(text('SELECT count(*) FROM public.vendors')).scalar_one()==2
        assert c.execute(text('SELECT foundation_supplier_id FROM purchasing_v4.authority_supplier WHERE id=:id'),{'id':sid}).scalar_one() is None
    # Observed Square supplier metadata cannot change an explicit internal default.
    with engine.begin() as c:c.execute(text("UPDATE ordering_catalog_identity SET catalog_attributes='{\"vendors\":[\"OTHER\"]}' WHERE square_variation_id='FIXTURE_V1'"))
    with engine.connect() as c:assert supplier_context(c,vid)['supplier_id']==sid


def test_owner_authorization_and_bulk_atomicity(ready):
    engine,vid,_=ready
    with engine.begin() as c:c.execute(text("INSERT INTO principals(id,username,password_hash,role,active) VALUES (2,'not-owner','invalid','MANAGER',true)"))
    with pytest.raises(PermissionError):execute(engine,[change('MANUAL',vid+':1',manual(vid))],actor=2)
    execute(engine,[change('MANUAL',vid+':1',manual(vid))])
    with pytest.raises(controls.Conflict):execute(engine,[change('MANUAL',vid+':1',manual(vid,par=6),1),change('MANUAL',vid+':2',manual(vid,store=2),99)])
    with engine.connect() as c:assert controls.heads(c,{('MANUAL',vid+':1')})[('MANUAL',vid+':1')]['version']==1


def test_concurrent_versions_and_lost_response_replay(ready):
    engine,_,_=ready;execute(engine,[change('GLOBAL','global',global_policy())])
    def update(value):
        try:return execute(engine,[change('GLOBAL','global',dict(global_policy(),target_weeks=str(value)),1)])
        except controls.Conflict:return 'CONFLICT'
    with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(update,[9,10]))
    assert sum(r=='CONFLICT' for r in results)==1
    command=str(uuid4());changes=[change('GLOBAL','global',global_policy(),2)]
    first=execute(engine,changes,command=command);assert execute(engine,changes,command=command)['replayed']
    with pytest.raises(controls.Conflict):execute(engine,[change('GLOBAL','global',global_policy(),3)],command=command)


def test_commands_preserve_all_existing_public_records(ready):
    engine,vid,sid=ready
    with reading(engine) as c:before=public_fingerprints(c);structure=public_structure(c)
    configure(ready)
    with reading(engine) as c:assert public_fingerprints(c)==before and public_structure(c)==structure


def test_immutable_audit_and_head_requires_matching_revision(ready):
    engine,_,_=ready;execute(engine,[change('GLOBAL','global',global_policy())])
    for sql in ('UPDATE purchasing_v4.control_revision SET reason=\'changed\'','DELETE FROM purchasing_v4.control_command',
                'TRUNCATE purchasing_v4.control_head',"UPDATE purchasing_v4.control_head SET version=version+1"):
        with pytest.raises(DBAPIError):
            with engine.begin() as c:c.execute(text(sql))


def test_history_check_is_readonly_unchecked_and_explicit_conversion(ready):
    engine,vid,_=configure(ready,history=True);key=vid+':1'
    with reading(engine) as c:
        before=public_fingerprints(c);check=calculate_history_check(c,actor_id=1,manual_keys=[key],as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        assert public_fingerprints(c)==before
    assert check['rows'][0]['selected_for_conversion'] is False and check['rows'][0]['eligible_for_conversion'] is True
    with engine.begin() as c:save_history_check(c,check)
    with engine.connect() as c:assert controls.heads(c,{('MANUAL',key)})[('MANUAL',key)]['payload']['state']=='ACTIVE'
    with pytest.raises(ValueError):
        with engine.begin() as c:use_automatic(c,actor_id=1,command_id=str(uuid4()),reason='Explicit selection required',check_id=check['id'],selected_keys=[])
    command=str(uuid4())
    with engine.begin() as c:result=use_automatic(c,actor_id=1,command_id=command,reason='Owner explicitly selected Use Automatic Engine',check_id=check['id'],selected_keys=[key])
    with engine.begin() as c:assert use_automatic(c,actor_id=1,command_id=command,reason='Owner explicitly selected Use Automatic Engine',check_id=check['id'],selected_keys=[key])['replayed']
    with engine.connect() as c:
        value=controls.heads(c,{('MANUAL',key)})[('MANUAL',key)]['payload'];assert value['state']=='AUTOMATIC' and value['par_units']==5 and value['level_units']==20


def test_history_dependency_change_blocks_conversion(ready):
    engine,vid,_=configure(ready,history=True);key=vid+':1'
    with reading(engine) as c:check=calculate_history_check(c,actor_id=1,manual_keys=[key],as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
    with engine.begin() as c:save_history_check(c,check)
    execute(engine,[change('PRODUCT',vid,dict(variation_id=vid,no_reorder=True,coverage=None))])
    with pytest.raises(controls.Conflict):
        with engine.begin() as c:use_automatic(c,actor_id=1,command_id=str(uuid4()),reason='Stale owner check',check_id=check['id'],selected_keys=[key])


def test_legacy_bulk_adoption_is_explicit_versioned_and_preserves_original(ready):
    engine,vid,_=ready
    from app.purchasing_v4.management_read import list_legacy_manual
    with engine.begin() as c:
        identity=c.execute(text("INSERT INTO par_levels(vendor_id,sku,store_id,par_source,manual_par_level,manual_stock_up_level) VALUES (1,'SAME',1,'MANUAL',5,20) RETURNING id")).scalar_one()
    with reading(engine) as c:
        before=public_fingerprints(c);row=list_legacy_manual(c,source_namespace='synthetic-fixture-source')['rows'][0]
    selected=[dict(legacy_id=identity,source_digest=row['source_digest'],expected_version=0)];command=str(uuid4())
    with engine.begin() as c:result=controls.adopt_legacy_manual(c,actor_id=1,command_id=command,reason='Owner explicitly adopts the selected original store unit rule',selections=selected,source_namespace='synthetic-fixture-source')
    with engine.begin() as c:assert controls.adopt_legacy_manual(c,actor_id=1,command_id=command,reason='Owner explicitly adopts the selected original store unit rule',selections=selected,source_namespace='synthetic-fixture-source')['replayed']
    with reading(engine) as c:
        assert public_fingerprints(c)==before
        assert result['source_provenance']['legacy_records'][0]['manual_par_level']==5
        assert controls.heads(c,{('MANUAL',vid+':1')})[('MANUAL',vid+':1')]['payload']['state']=='ACTIVE'
    with pytest.raises(controls.Conflict):
        with engine.begin() as c:controls.adopt_legacy_manual(c,actor_id=1,command_id=str(uuid4()),reason='Stale source/expected version must not overwrite the owner rule',selections=selected,source_namespace='synthetic-fixture-source')


def test_inactive_manual_comparison_never_implicitly_enables_automatic(ready):
    engine,vid,_=configure(ready,history=True);key=vid+':1'
    execute(engine,[change('MANUAL',key,manual(vid,state='INACTIVE'),1)])
    with reading(engine) as c:check=calculate_history_check(c,actor_id=1,manual_keys=[key],as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
    assert check['rows'][0]['eligible_for_conversion'] and not check['rows'][0]['selected_for_conversion']
    with engine.begin() as c:save_history_check(c,check)
    with engine.connect() as c:assert controls.heads(c,{('MANUAL',key)})[('MANUAL',key)]['payload']['state']=='INACTIVE'


def test_manual_units_ignore_only_automatic_scope_conflicts(ready):
    engine,vid,_=configure(ready)
    from test_purchasing_v4_calculation import row,ASOF
    from app.purchasing_v4.calculation import recommend_batch
    source=row(variation_id=vid,store_id='1',policy_issues=('UNRESOLVED_CATEGORY_OVERRIDE_SCOPE',))
    with engine.connect() as c:
        projected=project_rows(c,(source,));config=controls.configuration(c)
    manual_result=recommend_batch(projected,config,as_of=ASOF,input_digest='fixture-scope').recommendations[0]
    assert manual_result.policy_mode=='MANUAL_UNITS' and manual_result.raw_need_units==20
    from dataclasses import replace
    automatic_result=recommend_batch((replace(projected[0],manual=None),),config,as_of=ASOF,input_digest='fixture-scope').recommendations[0]
    assert automatic_result.forecast.weekly_units is None and 'UNRESOLVED_CATEGORY_OVERRIDE_SCOPE' in automatic_result.reasons


def test_interrupted_batch_rolls_back_even_if_caller_catches_failure(ready):
    engine,vid,_=ready
    from sqlalchemy import event
    count=0
    def fail_second(conn,cursor,statement,params,context,executemany):
        nonlocal count
        if statement.startswith('INSERT INTO purchasing_v4.control_head'):
            count+=1
            if count==2:raise RuntimeError('Injected mid-batch interruption')
    event.listen(engine,'before_cursor_execute',fail_second)
    command=str(uuid4());changes=[change('MANUAL',vid+':1',manual(vid)),change('MANUAL',vid+':2',manual(vid,store=2))]
    with engine.begin() as c:
        with pytest.raises(RuntimeError):controls.apply(c,actor_id=1,command_id=command,reason='Atomic interrupted fixture',changes=changes)
    event.remove(engine,'before_cursor_execute',fail_second)
    with engine.connect() as c:
        assert not controls.heads(c,{('MANUAL',vid+':1'),('MANUAL',vid+':2')})
        assert c.execute(text('SELECT count(*) FROM purchasing_v4.control_command WHERE id=:id'),{'id':command}).scalar_one()==0
    assert execute(engine,changes,command=command)['replayed'] is False


def test_approved_profile_gate_and_negative_inventory_derived_zero(ready):
    engine,vid,_=ready;execute(engine,[change('GLOBAL','global',dict(global_policy(),max_source_age_seconds=86400))])
    with engine.connect() as c:assert controls.approved_configuration(c) is None
    from test_purchasing_v4_calculation import row,known,ASOF
    from app.purchasing_v4.calculation import recommend_batch
    with engine.connect() as c:config=controls.configuration(c)
    record=row(on_hand=known(-3));result=recommend_batch((record,),config,as_of=ASOF,input_digest='negative-evidence').recommendations[0]
    assert result.observed_on_hand.value==-3 and result.inventory_position==0 and result.raw_need_units is not None
    assert 'NEGATIVE_OBSERVED_ON_HAND' in result.reasons and 'NEGATIVE_INVENTORY_CANDIDATE_GATE' not in result.reasons


def test_foundation_migrator_remains_compatible_after_controls(ready):
    engine,_,_=ready
    with engine.begin() as c:migrate(c)
    with engine.connect() as c:assert c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()=='p3_0003'


def test_cleared_default_stays_unresolved_and_deactivation_never_selects_another(ready):
    engine,vid,sid=configure(ready)
    execute(engine,[change('SUPPLIER',sid,dict(profile(),supplier_id=sid,active=False),1)])
    with engine.connect() as c:
        result=supplier_context(c,vid);assert result['state']=='UNRESOLVED_AUTHORITY' and result['supplier_id']==sid
    execute(engine,[change('ASSIGNMENT',vid,dict(variation_id=vid,supplier_id=None),1)])
    with engine.connect() as c:assert supplier_context(c,vid)['state']=='UNRESOLVED_DEFAULT'


def test_database_rejects_noncanonical_store_specific_supplier_assignment(ready):
    engine,vid,sid=ready;command=str(uuid4());value=json.dumps(dict(variation_id=vid,supplier_id=sid));key=vid+':1'
    with pytest.raises(DBAPIError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO purchasing_v4.control_command(id,request_digest,actor_id,reason,action,result) VALUES (:id,:sha,1,'Adversarial DB integrity fixture','FIXTURE','{}')"),{'id':command,'sha':'a'*64})
            c.execute(text("INSERT INTO purchasing_v4.control_revision(kind,record_key,version,payload,command_id,actor_id,reason) VALUES ('ASSIGNMENT',:key,1,CAST(:payload AS jsonb),:id,1,'Invalid store-specific key')"),dict(key=key,payload=value,id=command))
            c.execute(text("INSERT INTO purchasing_v4.control_head(kind,record_key,version,payload,last_command) VALUES ('ASSIGNMENT',:key,1,CAST(:payload AS jsonb),:id)"),dict(key=key,payload=value,id=command))


def test_controls_migration_interruption_recovers_forward(engine):
    plan=get_plan(engine)
    with engine.begin() as c:migrate(c);apply_plan(c,plan)
    with pytest.raises(RuntimeError):
        with engine.begin() as c:
            migrate_controls(c);raise RuntimeError('Injected migration interruption')
    with engine.connect() as c:
        assert c.execute(text("SELECT to_regclass('purchasing_v4.control_head')")).scalar_one() is None
        assert c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()=='p1_0001'
    with engine.begin() as c:migrate_controls(c)


def test_activity_sql_reads_real_schema_and_distinguishes_active_from_unknown(ready):
    engine,vid,_=ready
    from app.purchasing_v4.activity import investigate
    from app.purchasing_v4.calculation_source import load_snapshot
    from test_purchasing_v4_calculation import configuration as trial_configuration
    with reading(engine) as c:
        before=public_fingerprints(c)
        snapshot=load_snapshot(c,trial_configuration(),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        report=investigate(c,as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source',availability_snapshot=snapshot)
        assert public_fingerprints(c)==before
    assert report['cohort_counts']['VERIFIED_RECENT_ACTIVITY']==1
    assert report['cohort_counts']['INSUFFICIENT_EVIDENCE']==1
    product=next(r for r in report['products'] if r['id']==vid)
    assert product['streams']['sales']['windows']['30']['positive_records']==1
    assert product['streams']['receipts']['windows']['30']['state']=='UNKNOWN'
    assert report['status_mutations']==report['policy_mutations']==0

@pytest.mark.parametrize('par,level,valid',[(5,20,True),(5,5,False)])
def test_frozen_legacy_intent_cannot_silently_expire(ready,par,level,valid):
    engine,vid,_=ready
    from app.purchasing_v4.management_read import freeze_legacy_manual,list_legacy_manual
    from test_purchasing_v4_calculation import row
    with engine.begin() as c:
        identity=c.execute(text("INSERT INTO par_levels(vendor_id,sku,store_id,par_source,manual_par_level,manual_stock_up_level) VALUES (1,'SAME',1,'MANUAL',:par,:level) RETURNING id"),dict(par=par,level=level)).scalar_one()
        freeze_legacy_manual(c,source_namespace='synthetic-fixture-source')
        baseline=c.execute(text('SELECT observation FROM purchasing_v4.legacy_manual_evidence WHERE legacy_id=:id'),{'id':identity}).scalar_one()
        c.execute(text("UPDATE par_levels SET par_source='DYNAMIC',manual_par_level=0,manual_stock_up_level=0 WHERE id=:id"),{'id':identity})
        freeze_legacy_manual(c,source_namespace='synthetic-fixture-source')
        assert c.execute(text('SELECT observation FROM purchasing_v4.legacy_manual_evidence WHERE legacy_id=:id'),{'id':identity}).scalar_one()==baseline
        result=project_rows(c,(row(variation_id=vid,store_id='1',manual=None),))[0]
        if valid:assert result.manual.par_units==5 and result.manual.level_units==20
        else:assert result.manual is None and 'UNRESOLVED_FROZEN_LEGACY_MANUAL_POLICY' in result.policy_issues
        reviewed=list_legacy_manual(c,source_namespace='synthetic-fixture-source')['rows']
        assert any(r['id']==identity and r['frozen_observation']==baseline for r in reviewed)
    execute(engine,[change('MANUAL',vid+':1',manual(vid,state='INACTIVE'))])
    with engine.connect() as c:
        result=project_rows(c,(row(variation_id=vid,store_id='1',manual=None),))[0]
        assert result.manual is None and result.policy_issues==('EXPLICIT_INACTIVE_MANUAL_POLICY',)
