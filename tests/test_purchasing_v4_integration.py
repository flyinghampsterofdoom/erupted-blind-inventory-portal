"""Integrated deployment boundaries, immutable source witnesses and exposure proofs."""
from datetime import datetime,date,timedelta,timezone
from decimal import Decimal
from pathlib import Path
import subprocess,sys,os,json
import pytest
from sqlalchemy import text
from app.purchasing_v4.integration_app import Capabilities
from app.purchasing_v4.contracts import QuantityEvidence,EvidenceState,Availability
from app.purchasing_v4.evidence_rules import availability_projection,BUSINESS_ZONE
from app.purchasing_v4 import evidence_qualification as quality
from app.purchasing_v4.schema import migrate_integration
from app.purchasing_v4.storage import apply_plan
from app.purchasing_v4.reconciliation import public_fingerprints
from test_purchasing_v4_postgres import template,engine,reading,get_plan,AS_OF,AGE
from test_purchasing_v4_calculation import configuration


def test_release_capabilities_have_no_implicit_activation():
    assert not any(vars(Capabilities()).values())
    for options in [dict(workspace=1),dict(authorization=True),dict(workspace=True,inventory=True)]:
        with pytest.raises(ValueError):Capabilities(**options)
    assert Capabilities(workspace=True,receiving=True,inventory=True).inventory


def test_isolated_process_denies_external_resolution_and_connections():
    code='''from app.purchasing_v4.network_isolation import install
import socket
install();install()
for call in [lambda:socket.getaddrinfo('example.com',443),lambda:socket.socket().connect(('192.0.2.1',443))]:
 try:call();raise AssertionError('External endpoint allowed')
 except PermissionError:pass
assert socket.getaddrinfo('127.0.0.1',58365)
'''
    r=subprocess.run([sys.executable,'-c',code],env=dict(os.environ,PYTHONPATH='.'),capture_output=True,text=True)
    assert r.returncode==0,r.stderr


def exposure(coverage=EvidenceState.KNOWN,anchor=True):
    asof=datetime(2026,10,28,12,tzinfo=timezone.utc);seen=asof-timedelta(minutes=5)
    hand=QuantityEvidence(EvidenceState.KNOWN if anchor else EvidenceState.UNAVAILABLE,Decimal(5) if anchor else None,'fixture',reason='' if anchor else 'NO_OBSERVATION',observed_at=seen if anchor else None)
    event=dict(event_id='physical-reset',physical_count=True,occurred_at=datetime(2026,10,25,12,tzinfo=BUSINESS_ZONE),synchronized_at=asof-timedelta(hours=1),calculated_before_at=asof-timedelta(hours=2),quantity_delta=Decimal(0))
    start=date(2026,10,24);end=date(2026,10,28);sales={start+timedelta(days=i):QuantityEvidence(EvidenceState.KNOWN,Decimal(0),'fixture') for i in range(4)}
    return availability_projection(location='fixture-location',inventory=hand,anchor_row=dict(source_calculated_at=seen-timedelta(minutes=1),started_at=seen-timedelta(minutes=1)) if anchor else None,coverage=(coverage,'NO_COVERAGE' if coverage!=EvidenceState.KNOWN else '',asof),events=[event],boundary=[],sales=sales,start_day=start,end_day=end,as_of=asof)


def test_physical_reset_blocks_crossing_trajectory_but_not_proven_later_days():
    rows=exposure()
    assert [r[1] for r in rows]==[Availability.UNKNOWN,Availability.UNKNOWN,Availability.AVAILABLE,Availability.AVAILABLE]
    assert all(r[3]=='PHYSICAL_COUNT_BETWEEN_DAY_AND_ANCHOR' for r in rows[:2])


@pytest.mark.parametrize('coverage,anchor',[(EvidenceState.UNAVAILABLE,True),(EvidenceState.KNOWN,False)])
def test_physical_reset_improvement_never_supplies_missing_anchor_or_coverage(coverage,anchor):
    assert all(r[1]==Availability.UNKNOWN for r in exposure(coverage,anchor))


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_ADMIN_URL'),reason='Explicit disposable PostgreSQL required')
def test_inventory_witness_replay_retains_original_zero_unknown_and_changed_observations(engine):
    with reading(engine) as c:before=public_fingerprints(c)
    plan=get_plan(engine)
    with engine.begin() as c:
        migrate_integration(c);apply_plan(c,plan)
        first=quality.capture_inventory(c,source_namespace='synthetic-fixture-source');assert first['inserted_witnesses']==1
        assert quality.capture_inventory(c,source_namespace='synthetic-fixture-source')['inserted_witnesses']==0
    with reading(engine) as c:assert public_fingerprints(c)==before
    with engine.begin() as c:
        c.execute(text('UPDATE public.ordering_current_inventory SET counted_quantity=4,source_calculated_at=NULL'))
        assert quality.capture_inventory(c,source_namespace='synthetic-fixture-source')['inserted_witnesses']==1
    with reading(engine) as c:
        rows=c.execute(text('SELECT payload FROM purchasing_v4.inventory_witness ORDER BY captured_at')).scalars().all();assert rows[0]['counted_quantity']=='0.000';assert rows[1]['source_calculated_at'] is None
    for sql in ['UPDATE purchasing_v4.inventory_witness SET source_calculated_at=now()','DELETE FROM purchasing_v4.inventory_witness','TRUNCATE purchasing_v4.inventory_witness']:
        with pytest.raises(Exception):
            with engine.begin() as c:c.execute(text(sql))


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_ADMIN_URL'),reason='Explicit disposable PostgreSQL required')
def test_catalog_quality_reports_missing_inventory_and_unknown_history_without_recommendations(engine):
    plan=get_plan(engine)
    with engine.begin() as c:migrate_integration(c);apply_plan(c,plan)
    with reading(engine) as c:
        before=public_fingerprints(c);result=quality.investigate(c,configuration(lookback_days=20,recent_days=10),source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=AGE);assert public_fingerprints(c)==before
    assert result['store_variation_pairs']==4
    assert result['reason_counts']['MISSING_INVENTORY_OBSERVATION']==3
    assert result['variations_with_minimum_confirmed_availability']==0
    assert result['configuration_persisted'] is False
    assert not any('recommendation' in p for p in result['pairs'])


@pytest.mark.skipif(not os.getenv('TEST_POSTGRES_ADMIN_URL'),reason='Explicit disposable PostgreSQL required')
@pytest.mark.parametrize('proof',['verified','absent','wrong_scope','wrong_pair','failed','duplicate','different_time','malformed'])
def test_partial_inventory_requires_exact_existing_collector_pair_certificate(engine,proof):
 from app.purchasing_v4.evidence import ReadOnlyEvidence
 plan=get_plan(engine)
 with engine.begin() as c:
  migrate_integration(c);apply_plan(c,plan)
  c.execute(text("UPDATE public.ordering_inventory_refresh_runs SET result='PARTIAL',active_store_count=2,expected_pair_count=2,covered_pair_count=1,missing_pair_count=1,error_code='PARTIAL_COVERAGE' WHERE id=1"))
  scope={'variation_ids':['FIXTURE_V1'],'store_ids':[1,2]};evidence={'error_code':'PARTIAL_COVERAGE','observed_pairs':[['FIXTURE_V1',1]]}
  if proof=='wrong_scope':scope['variation_ids']=['FIXTURE_V2']
  if proof=='wrong_pair':evidence['observed_pairs']=[['FIXTURE_V1',2]]
  if proof=='malformed':scope=['not a scope']
  if proof!='absent':
   for repeat in range(2 if proof=='duplicate' else 1):
    c.execute(text("INSERT INTO public.purchasing_evidence_runs(source,status,started_at,completed_at,scope,evidence) SELECT 'INVENTORY',:status,started_at,completed_at+(:delta*interval '1 second'),CAST(:scope AS json),CAST(:evidence AS json) FROM public.ordering_inventory_refresh_runs WHERE id=1"),dict(status='FAILED' if proof=='failed' else 'PARTIAL',delta=1 if proof=='different_time' else 0,scope=json.dumps(scope),evidence=json.dumps(evidence)))
 with reading(engine) as c:
  reader=ReadOnlyEvidence(c);observed=reader.inventory('FIXTURE_V1',1,as_of=AS_OF,max_age=AGE);missing=reader.inventory('FIXTURE_V1',2,as_of=AS_OF,max_age=AGE)
  assert missing.state==EvidenceState.UNAVAILABLE and missing.value is None
  if proof=='verified':
   assert observed.state==EvidenceState.KNOWN and observed.value==0
   assert any(ref.startswith('purchasing_evidence_runs:') for ref in observed.references)
   assert reader.inventory('FIXTURE_V1',1,as_of=AS_OF+timedelta(days=3),max_age=AGE).state!=EvidenceState.KNOWN
  else:assert observed.state==EvidenceState.UNKNOWN and observed.value is None
