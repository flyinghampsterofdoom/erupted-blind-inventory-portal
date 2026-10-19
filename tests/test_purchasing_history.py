"""Real disposable PostgreSQL, mocked source pages, no live integrations."""
import copy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import database_template,receiving_db
from app.models import ConsignmentSaleFact,PurchasingSaleCorrection,PurchasingInventoryEvent,PurchasingHistoryCoverage
from app.services.purchasing_history_service import (certify,covered,daily_sales,import_movement_page,
    purchasing_source_orders,synchronize_movements,HistoryUnavailable,DatabaseHistoryGateway)
from app.services.v2_consignment_facts_service import import_square_orders
from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE as TZ
NOW=datetime(2026,10,19,15,tzinfo=timezone.utc)
END=datetime(2026,10,19,tzinfo=TZ)
START=END-timedelta(days=84)


def order(version=1,qty='4',state='COMPLETED'):
    return {'id':'O1','version':version,'state':state,'location_id':'L1','closed_at':'2026-10-18T12:00:00Z',
        'updated_at':'2026-10-19T12:00:00Z','line_items':[{'uid':'I1','catalog_object_id':'VAR1','name':'Sample','quantity':qty}]}


def event(identity='E1',qty='4',kind='ADJUSTMENT'):
    item={'id':identity,'location_id':'L1','catalog_object_id':'VAR1','quantity':qty,
        'occurred_at':'2026-10-18T12:00:00.79Z','created_at':'2026-10-18T12:00:01Z',
        'from_state':'IN_STOCK','to_state':'SOLD','state':'IN_STOCK'}
    return {'type':kind, 'adjustment' if kind=='ADJUSTMENT' else 'physical_count':item}


def test_source_coverage_independent_of_financial_missing_vendor_and_zero_days(receiving_db):
    with Session(receiving_db) as db:
        import_square_orders(db,orders=[order()]);db.flush()
        with pytest.raises(HistoryUnavailable):daily_sales(db,locations=['L1'],variations=['VAR1'],start_at=START,end_at=END,as_of=NOW)
        certify(db,source='SALES',locations=['L1'],start_at=START,through_at=NOW,verified_at=NOW)
        result=daily_sales(db,locations=['L1'],variations=['VAR1','ZERO'],start_at=START,end_at=END,as_of=NOW)
        assert sum(result.values())==4 and len(result)==1
        with pytest.raises(HistoryUnavailable):daily_sales(db,locations=['L1','L2'],variations=['VAR1'],start_at=START,end_at=END,as_of=NOW)
        with pytest.raises(HistoryUnavailable):daily_sales(db,locations=['L1'],variations=['VAR1'],start_at=START,end_at=END,as_of=NOW+timedelta(days=2))


def test_corrections_cancellations_removals_preserve_financial_facts_and_idempotency(receiving_db):
    with Session(receiving_db) as db:
        import_square_orders(db,orders=[order()]);db.flush()
        original=db.scalar(select(ConsignmentSaleFact));saved=(original.quantity_sold,original.extended_cogs_snapshot,original.attribution_status)
        certify(db,source='SALES',locations=['L1'],start_at=START,through_at=NOW,verified_at=NOW)
        def units():return sum(daily_sales(db,locations=['L1'],variations=['VAR1'],start_at=START,end_at=END,as_of=NOW).values())
        for _ in range(2):
            financial,changed=purchasing_source_orders(db,[order(2,'7')],synchronized_at=NOW)
            assert changed==1 and financial[0]['line_items']==[] and units()==7
        assert db.scalar(select(func.count()).select_from(PurchasingSaleCorrection))==1
        purchasing_source_orders(db,[order(3,'7','CANCELED')],synchronized_at=NOW);assert units()==0
        purchasing_source_orders(db,[order(2,'7')],synchronized_at=NOW);assert units()==0  # older source cannot replay
        removed=order(4);removed['line_items']=[]
        purchasing_source_orders(db,[removed],synchronized_at=NOW);assert units()==0
        purchasing_source_orders(db,[order(5,'4')],synchronized_at=NOW);assert units()==4
        assert (original.quantity_sold,original.extended_cogs_snapshot,original.attribution_status)==saved


def test_event_upserts_physical_count_uncertainty_and_exact_decimal(receiving_db):
    with Session(receiving_db) as db:
        for _ in range(2):import_movement_page(db,[event(qty='0.123456789')],synchronized_at=NOW)
        assert db.scalar(select(func.count()).select_from(PurchasingInventoryEvent))==1
        row=db.scalar(select(PurchasingInventoryEvent));assert row.quantity_delta==Decimal('-0.123456789')
        changed=event(qty='9');changed['adjustment']['updated_at']='2026-10-19T13:00:00Z'
        import_movement_page(db,[changed],synchronized_at=NOW);assert row.quantity_delta==-9
        import_movement_page(db,[event(qty='2')],synchronized_at=NOW);assert row.quantity_delta==-9
        import_movement_page(db,[event('P1','20','PHYSICAL_COUNT')],synchronized_at=NOW)
        from app.services.purchasing_history_service import movement_evidence
        certify(db,source='MOVEMENTS',locations=['L1'],start_at=START,through_at=NOW,verified_at=NOW)
        values,uncertain,_=movement_evidence(db,locations=['L1'],variations=['VAR1'],start_at=START,as_of=NOW)
        assert sum(values.values())==-9 and uncertain=={('L1','VAR1')}


def test_failed_page_transaction_does_not_advance_watermark_and_retry_is_safe(receiving_db):
    with Session(receiving_db) as db:
        def failing(path,payload):
            if payload.get('cursor'):raise RuntimeError('Network interruption')
            return {'changes':[event()],'cursor':'next'}
        with pytest.raises(RuntimeError),db.begin_nested():
            synchronize_movements(db,locations=['L1'],start_at=START,end_at=NOW,post=failing)
        assert db.get(PurchasingHistoryCoverage,('MOVEMENTS','L1')) is None
        assert db.scalar(select(func.count()).select_from(PurchasingInventoryEvent))==0
        calls=[]
        def good(path,payload):calls.append(payload);return {'changes':[event()]}
        synchronize_movements(db,locations=['L1'],start_at=START,end_at=NOW,post=good)
        synchronize_movements(db,locations=['L1'],start_at=START,end_at=NOW,post=good)
        assert db.scalar(select(func.count()).select_from(PurchasingInventoryEvent))==1
        assert calls[1]['updated_after']==(NOW-timedelta(hours=72,microseconds=1)).isoformat().replace('+00:00','Z')


def test_disjoint_scope_does_not_fill_gap(receiving_db):
    with Session(receiving_db) as db:
        certify(db,source='SALES',locations=['L1'],start_at=START,through_at=START+timedelta(days=7),verified_at=NOW)
        certify(db,source='SALES',locations=['L1'],start_at=END-timedelta(days=7),through_at=NOW,verified_at=NOW)
        with pytest.raises(HistoryUnavailable):covered(db,source='SALES',locations=['L1'],start_at=START,through_at=END,as_of=NOW)


def test_interactive_gateway_reads_only_catalog_and_current_counts(receiving_db):
    with Session(receiving_db) as db:
        import_square_orders(db,orders=[order()]);import_movement_page(db,[event()],synchronized_at=NOW)
        for source in ['SALES','MOVEMENTS']:certify(db,source=source,locations=['L1'],start_at=START,through_at=NOW,verified_at=NOW)
        calls=[]
        def post(path,payload):
            calls.append(path)
            assert path.endswith('/counts/batch-retrieve')
            return {'counts':[{'catalog_object_id':'VAR1','location_id':'L1','state':'IN_STOCK','quantity':'10','calculated_at':'2026-10-19T14:00:00Z'}]}
        g=DatabaseHistoryGateway(db,post)
        data=g.fetch(location_by_store={1:'L1'},variation_ids=['VAR1'],catalog={},history_days=84,as_of=NOW,include_today=True)
        row=data.by_store_variation[(1,'VAR1')]
        assert len(row.daily_sales)==1 and len(row.daily_inventory_deltas)==1
        assert row.current_on_hand==10 and row.current_day_inventory_delta==0
        assert calls==['/v2/inventory/counts/batch-retrieve']


def test_return_changes_do_not_net_gross_sales_or_rewrite_financial_history(receiving_db):
    from app.models import ConsignmentReturnFact,AuditLog
    with Session(receiving_db) as db:
        source=order();source['returns']=[{'uid':'R1','source_order_id':'O1','return_line_items':[{'uid':'RL1','source_line_item_uid':'I1','quantity':'1','total_money':{'amount':500,'currency':'USD'}}]}]
        import_square_orders(db,orders=[source]);db.flush()
        original=db.scalar(select(ConsignmentReturnFact));saved=(original.quantity_returned,original.refund_amount)
        updated=copy.deepcopy(source);updated['version']=2;updated['returns'][0]['return_line_items'][0]['quantity']='2'
        purchasing_source_orders(db,[updated],synchronized_at=NOW)
        certify(db,source='SALES',locations=['L1'],start_at=START,through_at=NOW,verified_at=NOW)
        assert sum(daily_sales(db,locations=['L1'],variations=['VAR1'],start_at=START,end_at=END,as_of=NOW).values())==4
        assert (original.quantity_returned,original.refund_amount)==saved
        assert db.scalar(select(PurchasingSaleCorrection.reason))=='RETURN_SOURCE_CHANGED'
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_SOURCE_CORRECTION'))==1
        purchasing_source_orders(db,[updated],synchronized_at=NOW)
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='PURCHASING_SOURCE_CORRECTION'))==1


def test_ambiguous_provider_calculation_bounds_are_never_treated_as_known(receiving_db):
    from app.services.purchasing_history_service import movement_evidence
    with Session(receiving_db) as db:
        moved=event();moved['adjustment']['occurred_at']='2026-07-01T12:00:00Z';moved['adjustment']['created_at']='2026-07-01T12:00:01Z'
        import_movement_page(db,[moved],synchronized_at=NOW,scope_start_at=START-timedelta(days=7))
        certify(db,source='MOVEMENTS',locations=['L1'],start_at=START-timedelta(days=7),through_at=NOW,verified_at=NOW)
        delta,unknown,_=movement_evidence(db,locations=['L1'],variations=['VAR1'],start_at=START,as_of=NOW)
        assert delta=={} and unknown=={('L1','VAR1')}


def test_historical_read_retries_rate_limit_but_not_bad_credentials(monkeypatch):
    from urllib.error import HTTPError
    from app.services import purchasing_history_service as h
    calls=[]
    def post(path,payload,**kw):
        calls.append(kw)
        if len(calls)<3:
            try:raise HTTPError('https://example.invalid',429,'Rate limited',{'Retry-After':'0'},None)
            except HTTPError as cause:raise RuntimeError('rate limit') from cause
        return {'orders':[]}
    monkeypatch.setattr('app.services.square_ordering_data_service._square_post',post)
    assert h.history_post('/v2/orders/search',{})=={'orders':[]}
    assert len(calls)==3 and all(c['timeout_seconds']==30 for c in calls)
    calls.clear()
    def denied(*a,**kw):
        calls.append(1)
        try:raise HTTPError('https://example.invalid',401,'Denied',{},None)
        except HTTPError as cause:raise RuntimeError('credentials rejected') from cause
    monkeypatch.setattr('app.services.square_ordering_data_service._square_post',denied)
    with pytest.raises(RuntimeError):h.history_post('/v2/orders/search',{})
    assert len(calls)==1


def test_background_refresh_resumes_committed_scope_without_restarting_backfill(receiving_db,monkeypatch):
    from app.services import v2_square_data_service as refresh
    from app.services import purchasing_history_service as history
    from app.services.v2_consignment_facts_service import synchronize_square_facts as original
    from app.models import ConsignmentSalesSyncState
    monkeypatch.setattr(refresh,'SessionLocal',lambda:Session(receiving_db))
    monkeypatch.setattr(refresh,'_now',lambda:NOW)
    start=datetime.combine(NOW.astimezone(TZ).date(),datetime.min.time(),TZ)-timedelta(days=84)
    calls=[];failure=[True]
    class EmptyReader:
        def search(self,**kw):yield []
    def sync(db,**kw):
        calls.append(kw['start_at'])
        if failure[0] and len(calls)==2:raise RuntimeError('Interrupted source read')
        return original(db,**kw,reader=EmptyReader())
    monkeypatch.setattr(refresh,'synchronize_square_facts',sync)
    movement=history.synchronize_movements
    monkeypatch.setattr(history,'synchronize_movements',lambda db,**kw:movement(db,**kw,post=lambda *a:{'changes':[]}))
    first=refresh.refresh_square_sales_data(actor_id=1,force=True,start_at=start,end_at=NOW)
    assert first.state=='failed'
    with Session(receiving_db) as db:
        scope=db.get(PurchasingHistoryCoverage,('SALES','L1'));assert scope is not None
        assert scope.through_at==start+timedelta(days=7)
        assert db.get(ConsignmentSalesSyncState,1).last_result=='FAILED'
    failure[0]=False
    second=refresh.refresh_square_sales_data(actor_id=1,force=True,end_at=NOW)
    assert second.state=='current'
    assert calls[2]==start+timedelta(days=4)  # exactly the three-day overlap
    with Session(receiving_db) as db:
        assert db.get(ConsignmentSalesSyncState,1).last_result=='COMPLETE'
        assert db.get(PurchasingHistoryCoverage,('MOVEMENTS','L4')).through_at==NOW


def test_current_counts_newer_than_history_disable_backcast_without_zeroing_stock(receiving_db):
    with Session(receiving_db) as db:
        for source in ['SALES','MOVEMENTS']:certify(db,source=source,locations=['L1'],start_at=START,through_at=NOW,verified_at=NOW)
        def post(*args):return {'counts':[{'catalog_object_id':'VAR1','location_id':'L1','state':'IN_STOCK','quantity':'15','calculated_at':(NOW+timedelta(seconds=1)).isoformat()}]}
        g=DatabaseHistoryGateway(db,post)
        data=g.fetch(location_by_store={1:'L1'},variation_ids=['VAR1'],catalog={},history_days=84,as_of=NOW+timedelta(seconds=2),include_today=True)
        row=data.by_store_variation[(1,'VAR1')]
        assert row.current_on_hand==15 and row.inventory_valid and row.current_day_inventory_delta is None
        assert not next(s for s in row.required_sources if s.source=='stockout_history').complete
