"""Finite source collection, network/transaction separation and common evidence."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.error import HTTPError
from io import BytesIO
import time
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from app.forecast_validation.transport import BoundedRead
from app.forecast_validation.exposure import EvidenceGateway
from app.forecast_validation.collection import collect,source_session
from app.forecast_validation.contracts import EvidenceError,validate
from app.services.v2_ordering_square_gateway import SquareProductMetadata,SquareInventoryCountReadResult,SquareInventoryCount
from test_purchasing_receiving_extension import database_template,receiving_db

@pytest.mark.parametrize('sizes',[[0],[3],[100]*150+[7],[2,1,3]])
def test_exhausts_every_cursor_even_when_page_short(sizes):
    calls=[]
    def post(path,payload):
        i=len(calls);calls.append(payload)
        return {'changes':[{}]*sizes[i],**({'cursor':str(i+1)} if i+1<len(sizes) else {})}
    gateway=EvidenceGateway(post=post)
    got=gateway.fetch_verified_inventory_events(['L'],['V'],datetime.now(timezone.utc)-timedelta(days=84),datetime.now(timezone.utc))
    assert len(got)==sum(sizes) and len(calls)==len(sizes)
    assert gateway.history_batches==[{'offset':0,'variations':1,'pages':len(sizes),'records':sum(sizes),'pagination_exhausted':True}]
    assert all(v['limit']==100 for v in calls)

def test_multiple_variation_batches_and_repeat_cursor():
    calls=[]
    def post(path,payload):calls.append(payload);return {'changes':[]}
    g=EvidenceGateway(post=post);g.fetch_verified_inventory_events(['L'],list(map(str,range(222))),datetime.now(timezone.utc),datetime.now(timezone.utc))
    assert [v['variations'] for v in g.history_batches]==[100,100,22]
    g=EvidenceGateway(post=lambda *a:{'changes':[],'cursor':'REPEAT'})
    with pytest.raises(EvidenceError,match='Repeated'):g.fetch_verified_inventory_events(['L'],['V'],datetime.now(timezone.utc),datetime.now(timezone.utc))

@pytest.mark.parametrize('failure',[RuntimeError('Interrupted'),ValueError('Invalid page')])
def test_interrupted_pagination_never_returns_partial(failure):
    calls=[]
    def post(*a):
        calls.append(1)
        if len(calls)>1:raise failure
        return {'changes':[{}],'cursor':'NEXT'}
    g=EvidenceGateway(post=post)
    with pytest.raises(type(failure)):g.fetch_verified_inventory_events(['L'],['V'],datetime.now(timezone.utc),datetime.now(timezone.utc))
    assert not g.history_batches

def http_error(code,retry_after=None):
    return HTTPError('https://example.invalid',code,'test',{'Retry-After':retry_after} if retry_after else {},BytesIO(b''))

@pytest.mark.parametrize('code',[429,502,503,504])
def test_bounded_retry_counts_every_http_attempt(code):
    calls=[];sleeps=[]
    def post(*a,**kw):
        calls.append(kw)
        if len(calls)<3:raise RuntimeError('wrapped') from http_error(code)
        return {'changes':[{}]}
    b=BoundedRead(post,max_calls=4,sleep=sleeps.append)
    assert len(b('/v2/inventory/changes/batch-retrieve',{})['changes'])==1
    assert b.calls==3 and sleeps==[1,2] and b.metrics()['retries']==2 and b.metrics()['pages']==1
    assert all(0<q['timeout_seconds']<=15 for q in calls)

@pytest.mark.parametrize('code,retry_after,expected_calls',[(400,None,1),(401,None,1),(429,'60',1),(429,'2',3)])
def test_no_unbounded_retry_or_rate_limit_wait(code,retry_after,expected_calls):
    b=BoundedRead(lambda *a,**kw:(_ for _ in ()).throw(http_error(code,retry_after)),sleep=lambda _:None)
    with pytest.raises(HTTPError):b('/v2/inventory/changes/batch-retrieve',{})
    assert b.calls==expected_calls

def test_total_budget_and_retry_pool_exhaustion():
    b=BoundedRead(lambda *a,**kw:{},max_calls=2)
    for _ in range(2):b('/v2/catalog/search-catalog-items',{})
    with pytest.raises(TimeoutError,match='budget'):b('/v2/catalog/search-catalog-items',{})
    b=BoundedRead(lambda *a,**kw:(_ for _ in ()).throw(http_error(429)),max_retry_attempts=1,sleep=lambda _:None)
    with pytest.raises(HTTPError):b('/v2/catalog/search-catalog-items',{})
    assert b.calls==2
    clock=[0.0];b=BoundedRead(lambda *a,**kw:{},budget=1,clock=lambda:clock[0]);clock[0]=2
    with pytest.raises(TimeoutError):b('/v2/catalog/search-catalog-items',{})
    assert b.calls==0

def test_original_exception_survives_secondary_close_failure(monkeypatch):
    from app.forecast_validation import collection
    class Broken:
        def close(self):raise RuntimeError('secondary cleanup failure')
    monkeypatch.setattr(collection,'Session',lambda engine:Broken())
    error=TimeoutError('original pagination budget failure')
    with pytest.raises(TimeoutError) as exc:
        with source_session(None):raise error
    assert exc.value is error and exc.value.collection_cleanup_error=='RuntimeError'

def gateway(engine,delay=0,mutate=None):
    now=datetime.now(timezone.utc)
    class Gateway:
        def network(self):
            assert engine.pool.checkedout()==0,'A source database connection remains checked out during API retrieval'
        def fetch_active_catalog(self):
            self.network();time.sleep(delay)
            if mutate:mutate()
            return {'VAR1':SquareProductMetadata('VAR1','SKU','Test','Test',now-timedelta(days=200),False,vendor_ids=('V1',))}
        def fetch_current_inventory_counts(self,**kw):
            self.network();return SquareInventoryCountReadResult({(f'L{i}','VAR1'):SquareInventoryCount(f'L{i}','VAR1',Decimal(10),now) for i in range(1,5)})
        def fetch_verified_inventory_events(self,*a):self.network();return []
    return Gateway()

def test_no_transaction_during_network_even_with_one_second_idle_timeout(receiving_db):
    engine=create_engine(receiving_db.url,isolation_level='REPEATABLE READ',connect_args={'options':'-c default_transaction_read_only=on -c idle_in_transaction_session_timeout=1000'})
    try:
        snap=collect(engine,gateway(engine,delay=1.2));validate(snap)
        assert snap['collection']['source_before']==snap['collection']['source_after']
        assert not snap['collection']['source_transaction_during_network'] and engine.pool.checkedout()==0
        assert snap['collection']['started_at']<=snap['collection']['inventory_retrieved_at']<=snap['collection']['completed_at']
    finally:engine.dispose()

def test_changed_source_fails_closed_and_releases_connection(receiving_db):
    engine=create_engine(receiving_db.url,isolation_level='REPEATABLE READ',connect_args={'options':'-c default_transaction_read_only=on'})
    def change():
        with receiving_db.begin() as c:c.execute(text("UPDATE vendor_sku_configs SET pack_size=pack_size+1"))
        # Fixture may have no configs; an existing store input always exists.
        with receiving_db.begin() as c:c.execute(text("UPDATE stores SET name=name || ' changed' WHERE id=1"))
    try:
        with pytest.raises(EvidenceError,match='Source changed'):collect(engine,gateway(engine,mutate=change))
        assert engine.pool.checkedout()==0
    finally:engine.dispose()

def test_source_isolation_enforced(receiving_db):
    engine=create_engine(receiving_db.url,connect_args={'options':'-c default_transaction_read_only=on'})
    try:
        with pytest.raises(EvidenceError,match='repeatable read'):collect(engine,gateway(engine))
    finally:engine.dispose()

def test_catalog_or_duplicate_inventory_revision_change_fails_closed():
    n=[0]
    def post(path,payload):
        n[0]+=1;return {'counts':[{'location_id':'L','catalog_object_id':'V','state':'IN_STOCK','quantity':str(n[0])}]}
    g=EvidenceGateway(post=post)
    g._read_post('/v2/inventory/counts/batch-retrieve',{})
    with pytest.raises(EvidenceError,match='Inventory changed'):g._read_post('/v2/inventory/counts/batch-retrieve',{})

def test_catalog_version_changes_across_pages_fail_closed():
    calls=[0]
    def post(path,payload):
        calls[0]+=1
        return {'items':[{'id':'I','version':calls[0],'item_data':{'variations':[{'id':'V','version':calls[0],'item_variation_data':{}}]}}],**({'cursor':'NEXT'} if calls[0]==1 else {})}
    with pytest.raises(EvidenceError,match='Catalog changed'):EvidenceGateway(post=post).fetch_active_catalog()

@pytest.mark.parametrize('response',[{'errors':[{'category':'API_ERROR'}]},{'changes':'invalid'}])
def test_malformed_history_page_cannot_complete(response):
    g=EvidenceGateway(post=lambda *a:response)
    with pytest.raises(EvidenceError,match='Incomplete'):g.fetch_verified_inventory_events(['L'],['V'],datetime.now(timezone.utc),datetime.now(timezone.utc))
