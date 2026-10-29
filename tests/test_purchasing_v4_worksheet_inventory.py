"""Real PostgreSQL inventory evidence, pinned store identity and SELECT-only scope."""
from datetime import timedelta
from sqlalchemy import text
import pytest
from app.purchasing_v4.worksheet_inventory import inventory_view
from app.purchasing_v4.evidence import ReadOnlyEvidence
from app.purchasing_v4.calculation_serialization import primitive
from app.purchasing_v4.reconciliation import public_fingerprints,public_structure
from test_purchasing_v4_worksheets import sheet,ready,engine,template,reading,pytestmark,AS_OF


def test_inventory_matches_verified_adapter_and_preserves_all_records(sheet):
 e,state=sheet;sid=state['session']['id'];vid=state['rows'][0]['variation_id']
 with e.begin() as c:c.execute(text('UPDATE ordering_current_inventory SET counted_quantity=0'))
 with reading(e) as c:
  before=public_fingerprints(c);structure=public_structure(c)
  ref=c.execute(text('SELECT square_variation_id FROM purchasing_v4.variation_reference WHERE variation_id=:id'),{'id':vid}).scalar_one()
  result=inventory_view(c,session_id=sid,actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=timedelta(days=2))
  expected=[primitive(ReadOnlyEvidence(c).inventory(ref,s['id'],as_of=AS_OF,max_age=timedelta(days=2))) for s in state['session']['stores']]
  assert result['rows'][state['rows'][0]['id']]==expected
  assert expected[0]['state']=='KNOWN' and expected[0]['value']=='0.000'
  assert all(x['value'] is None for x in expected[1:])
  assert public_fingerprints(c)==before and public_structure(c)==structure
  assert c.execute(text('SELECT version FROM purchasing_v4.worksheet WHERE id=:id'),{'id':sid}).scalar_one()==0


def test_inventory_requires_readonly_owner_and_explicit_age(sheet):
 e,state=sheet;args=dict(session_id=state['session']['id'],actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF)
 with e.connect() as c:
  with pytest.raises(PermissionError):inventory_view(c,**args)
 with reading(e) as c:
  rows=inventory_view(c,**args)['rows'];assert all(v['value'] is None and v['reason']=='FRESHNESS_NOT_CONFIGURED' for r in rows.values() for v in r)
  stale=inventory_view(c,**args,max_age=timedelta(seconds=1))['rows'][state['rows'][0]['id']][0];assert stale['value'] is None and stale['reason']=='STALE_OBSERVATION'
  with pytest.raises(Exception):inventory_view(c,**dict(args,actor_id=999))


def test_pinned_store_order_negative_fraction_and_stale_fail_closed(sheet):
 e,state=sheet;rid=state['rows'][0]['id']
 with e.begin() as c:
  c.execute(text("UPDATE ordering_current_inventory SET counted_quantity=-2.125"))
 with reading(e) as c:
  result=inventory_view(c,session_id=state['session']['id'],actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=timedelta(days=2))
  assert result['rows'][rid][0]['value']=='-2.125'
 with e.begin() as c:c.execute(text("UPDATE ordering_current_inventory SET freshness_state='STALE'"))
 with reading(e) as c:
  result=inventory_view(c,session_id=state['session']['id'],actor_id=1,source_namespace='synthetic-fixture-source',as_of=AS_OF,max_age=timedelta(days=2))
  assert result['rows'][rid][0]['value'] is None and 'STALE' in result['rows'][rid][0]['reason']
