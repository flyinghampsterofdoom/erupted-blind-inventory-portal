"""Batch/single adapter parity using real disposable PostgreSQL, not mocks."""
from datetime import timedelta
import pytest
from sqlalchemy import text
from app.purchasing_v4.calculation_source import load_snapshot
from app.purchasing_v4.schema import migrate
from app.purchasing_v4.storage import apply_plan
from app.purchasing_v4.evidence import ReadOnlyEvidence
from app.purchasing_v4.reconciliation import public_fingerprints
from test_purchasing_v4_postgres import template,engine,reading,get_plan,AS_OF,AGE,pytestmark
from test_purchasing_v4_calculation import configuration


def initialize(engine):
    plan=get_plan(engine)
    with engine.begin() as conn:migrate(conn);apply_plan(conn,plan)


def test_batch_matches_accepted_single_adapter(engine):
    initialize(engine);config=configuration(lookback_days=20,recent_days=10)
    with reading(engine) as conn:
        before=public_fingerprints(conn)
        batch=load_snapshot(conn,config,as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        mapping=dict(conn.execute(text('SELECT variation_id::text,square_variation_id FROM purchasing_v4.variation_reference')).all())
        single=ReadOnlyEvidence(conn)
        for row in batch.rows:
            vid=mapping[row.variation_id];sid=int(row.store_id);start=min(d.day for d in row.history);end=AS_OF.date()
            assert row.on_hand==single.inventory(vid,sid,as_of=AS_OF,max_age=AGE)
            assert row.supply.incoming[0].quantity==single.incoming(vid,sid)
            sales=single.sales(vid,sid,start,end,as_of=AS_OF,max_age=AGE)
            availability=single.availability(vid,sid,start,end,as_of=AS_OF,max_age=AGE)
            assert [(d.day,d.availability) for d in row.history]==[(d.day,d.availability) for d in availability]
            assert [(d.day,d.sales.value,d.sales.state,d.sales.reason) for d in row.history]==[(d,q.value,q.state,q.reason) for d,q in sales.items()]
        assert batch.data_queries==14
        assert public_fingerprints(conn)==before


def test_ambiguous_manual_does_not_convert_to_automatic(engine):
    initialize(engine)
    with engine.begin() as conn:
        # Manual supplier/SKU no longer has an exact identity reference.
        conn.execute(text("INSERT INTO par_levels(vendor_id,sku,store_id,par_source,manual_par_level,manual_stock_up_level) VALUES (2,'SAME',1,'MANUAL',5,20)"))
    with reading(engine) as conn:
        batch=load_snapshot(conn,configuration(),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        assert all('UNRESOLVED_LEGACY_MANUAL_REFERENCE' in r.policy_issues for r in batch.rows if r.store_id=='1' and r.pack_size==5)
        assert all(r.manual is None for r in batch.rows)


def test_query_count_independent_of_catalog_size(engine):
    initialize(engine)
    with reading(engine) as conn:
        first=load_snapshot(conn,configuration(),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO ordering_catalog_identity(square_variation_id,square_item_id,sku,item_name,variation_name,last_seen_at) VALUES ('FIXTURE_V3','FIXTURE_I1','NEW','Fixture Product','Third','2026-10-27T10:00Z')"))
    plan=get_plan(engine)
    with engine.begin() as conn:apply_plan(conn,plan)
    with reading(engine) as conn:
        second=load_snapshot(conn,configuration(),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        assert len(second.rows)==len(first.rows)+2
        assert first.data_queries==second.data_queries==14


@pytest.mark.parametrize('status,count_time,expected',[
    ('PENDING','2026-10-27T11:40Z',False),
    ('SUCCESS','2026-10-27T11:00Z',False),
    ('SUCCESS','2026-10-27T11:40Z',True)])
def test_receipt_success_requires_posting_in_count_watermark(engine,status,count_time,expected):
    initialize(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("""
INSERT INTO purchase_order_receipts(id,purchase_order_id,received_by_principal_id) VALUES (1,129,1);
INSERT INTO purchase_order_receipt_lines(id,receipt_id,purchase_order_line_id) VALUES (1,1,1);
""")
        conn.execute(text("INSERT INTO square_sync_events(id,sync_type,idempotency_key,status,updated_at) VALUES (1,'FIXTURE','fixture-posting',:status,'2026-10-27T11:30Z')"),{'status':status})
        conn.execute(text("""INSERT INTO purchase_order_receipt_allocations(id,receipt_line_id,purchase_order_line_id,store_id,square_variation_id,event_kind,quantity_delta,square_sync_event_id,created_by_principal_id,created_at)
VALUES (1,1,1,1,'FIXTURE_V1','DELIVERY',1,1,1,'2026-10-27T11:30Z')"""))
        conn.execute(text("UPDATE ordering_current_inventory SET source_calculated_at=:stamp"),{'stamp':count_time})
    with reading(engine) as conn:
        batch=load_snapshot(conn,configuration(),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        row=next(r for r in batch.rows if r.store_id=='1' and r.pack_size==5)
        assert row.supply.coherent_with_on_hand is expected
        if not expected:assert row.supply.coherence_reasons==('UNRECONCILED_RECEIPT_POSTING:1',)


def test_category_reference_from_retained_catalog_shape(engine):
    initialize(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ordering_catalog_identity SET catalog_attributes=CAST(:attrs AS jsonb) WHERE square_variation_id='FIXTURE_V1'"),{'attrs':'{"category_ids":["CATEGORY_ONE"]}'})
    with reading(engine) as conn:
        batch=load_snapshot(conn,configuration(),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        assert all(r.category_id=='CATEGORY_ONE' for r in batch.rows if r.pack_size==5)


@pytest.mark.parametrize('variation,observed_at',[
    ('FIXTURE_V1','2026-10-27T11:00Z'),
    ('FIXTURE_V2','2026-10-27T11:00Z'),
    ('FIXTURE_V2','2026-10-28T00:00Z')])
def test_corrected_sales_batch_parity_with_cutoff_and_reassignment(engine,variation,observed_at):
    initialize(engine)
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO purchasing_sale_corrections(kind,square_order_id,square_line_item_uid,
            location_id,variation_id,business_date,quantity,reason,source_digest,synchronized_at)
            VALUES ('LINE','FIXTURE_ORDER','FIXTURE_LINE','FIXTURE_LOC1',:vid,'2026-10-26',3,'FIXTURE',:sha,:stamp)"""),
            {'vid':variation,'sha':'a'*64,'stamp':observed_at})
    with reading(engine) as conn:
        batch=load_snapshot(conn,configuration(lookback_days=20,recent_days=10),as_of=AS_OF,max_age=AGE,source_namespace='synthetic-fixture-source')
        mapping=dict(conn.execute(text('SELECT variation_id::text,square_variation_id FROM purchasing_v4.variation_reference')).all())
        single=ReadOnlyEvidence(conn)
        for row in batch.rows:
            reference=single.sales(mapping[row.variation_id],int(row.store_id),row.history[0].day,AS_OF.date(),as_of=AS_OF,max_age=AGE)
            assert [(d.sales.state,d.sales.value,d.sales.reason) for d in row.history]==[(q.state,q.value,q.reason) for q in reference.values()]
        assert conn.execute(text('SELECT net_sales_amount FROM consignment_sale_facts WHERE id=1')).scalar_one()==20
