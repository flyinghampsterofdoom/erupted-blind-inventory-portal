"""Real PostgreSQL qualification. Every created DB is confined to loopback/prefix."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from app.purchasing_v4.evidence import ReadOnlyEvidence
from app.purchasing_v4.contracts import EvidenceState, Availability
from app.purchasing_v4.identity import build_plan, IdentityPlan, canonical
from app.purchasing_v4.isolation import require_isolated, require_isolated_url, readonly_snapshot
from app.purchasing_v4.schema import migrate, downgrade
from app.purchasing_v4.storage import apply_plan, IdentityCollisionError
from app.purchasing_v4.reconciliation import public_fingerprints, public_structure


ADMIN = os.getenv('TEST_POSTGRES_ADMIN_URL')
pytestmark = pytest.mark.skipif(not ADMIN, reason='TEST_POSTGRES_ADMIN_URL required for PostgreSQL')
AS_OF = datetime(2026,10,27,12,tzinfo=timezone.utc)
AGE = timedelta(days=2)


@contextmanager
def reading(engine):
    with readonly_snapshot(engine) as conn:
        yield conn


@pytest.fixture(scope='module')
def template():
    from app.schema_contract import upgrade_database
    admin = create_engine(ADMIN, isolation_level='AUTOCOMMIT')
    assert admin.url.host == '127.0.0.1', 'Tests only create loopback disposable databases'
    name = 'erupted_v4_phase1_template_'+uuid4().hex[:10]
    with admin.connect() as conn:conn.execute(text('CREATE DATABASE '+name))
    url = admin.url.set(database=name)
    upgrade_database(url.render_as_string(hide_password=False))
    engine=create_engine(url)
    with engine.begin() as conn:
        # Static synthetic historical fixture, created before migration baseline.
        # No purchase authorization/creation service or external workflow is called.
        conn.exec_driver_sql("""
INSERT INTO principals(id,username,password_hash,role,active) VALUES (1,'v4-fixture','not-login-capable','ADMIN',true);
INSERT INTO stores(id,name,square_location_id,active) VALUES (1,'Fixture One','FIXTURE_LOC1',true),(2,'Fixture Two','FIXTURE_LOC2',true);
INSERT INTO vendors(id,square_vendor_id,name,active) VALUES (1,'FIXTURE_SUPPLIER1','Fixture One',true),(2,'FIXTURE_SUPPLIER2','Fixture Two',true);
INSERT INTO ordering_catalog_identity(square_variation_id,square_item_id,sku,item_name,variation_name,last_seen_at)
 VALUES ('FIXTURE_V1','FIXTURE_I1','SAME','Fixture Product','First','2026-10-27T10:00Z'),
        ('FIXTURE_V2','FIXTURE_I1','SAME','Fixture Product','Second','2026-10-27T10:00Z');
INSERT INTO vendor_sku_configs(id,vendor_id,sku,square_variation_id,unit_cost,pack_size,is_default_vendor,active)
 VALUES (1,1,'SAME','FIXTURE_V1',10,5,true,true);
INSERT INTO purchase_orders(id,vendor_id,status,created_by_principal_id) VALUES (129,1,'IN_TRANSIT',1);
INSERT INTO purchase_order_lines(id,purchase_order_id,variation_id,item_name,variation_name,ordered_qty,received_qty_total)
 VALUES (1,129,'FIXTURE_V1','Fixture Product','First',100,40);
INSERT INTO purchase_order_store_allocations(id,purchase_order_line_id,store_id,expected_qty,allocated_qty,store_received_qty)
 VALUES (1,1,1,60,60,30),(2,1,2,40,40,10);
INSERT INTO ordering_inventory_refresh_runs(id,correlation_id,result,expected_variation_count,active_store_count,
 expected_pair_count,covered_pair_count,missing_pair_count,square_request_count,started_at,completed_at,refreshed_by_principal_id)
 VALUES (1,'fixture-refresh','COMPLETE',1,1,1,1,0,1,'2026-10-27T11:50Z','2026-10-27T11:55Z',1);
INSERT INTO ordering_current_inventory(square_variation_id,store_id,square_location_id,counted_quantity,
 source_calculated_at,refreshed_at,freshness_state,refresh_run_id)
 VALUES ('FIXTURE_V1',1,'FIXTURE_LOC1',0,'2026-10-27T11:00Z','2026-10-27T11:55Z','FRESH',1);
INSERT INTO purchasing_history_coverage(source,location_id,start_at,through_at,verified_at)
 VALUES ('SALES','FIXTURE_LOC1','2026-10-01T00:00Z','2026-10-27T12:00Z','2026-10-27T12:00Z'),
 ('MOVEMENTS','FIXTURE_LOC1','2026-10-01T00:00Z','2026-10-27T12:00Z','2026-10-27T12:00Z');
INSERT INTO consignment_sale_facts(id,square_order_id,square_line_item_uid,square_variation_id,square_location_id,
 store_id,business_date,transacted_at,quantity_sold,gross_sales_amount,discount_amount,tax_amount,net_sales_amount,
 currency,product_name_snapshot,attribution_status,attribution_source,source_synchronized_at)
 VALUES (1,'FIXTURE_ORDER','FIXTURE_LINE','FIXTURE_V1','FIXTURE_LOC1',1,'2026-10-26','2026-10-26T16:00Z',
 2,20,0,0,20,'USD','Fixture Product','NON_CONSIGNMENT','FIXTURE','2026-10-27T10:00Z');
""")
    engine.dispose()
    yield admin,name
    with admin.connect() as conn:conn.execute(text('DROP DATABASE '+name))
    admin.dispose()


@pytest.fixture
def engine(template):
    admin,base=template
    name='erupted_v4_phase1_test_'+uuid4().hex[:10]
    with admin.connect() as conn:conn.execute(text(f'CREATE DATABASE {name} TEMPLATE {base}'))
    db=create_engine(admin.url.set(database=name),pool_size=4)
    yield db
    db.dispose()
    with admin.connect() as conn:conn.execute(text('DROP DATABASE '+name))


def get_plan(engine):
    with reading(engine) as conn:return ReadOnlyEvidence(conn).identity_plan('synthetic-fixture-source')


def test_migration_public_schema_and_historical_identities_unchanged(engine):
    from app.schema_contract import assert_supported_schema
    with reading(engine) as conn:
        before=public_fingerprints(conn); structure=public_structure(conn)
    plan=get_plan(engine)
    with engine.begin() as conn:
        migrate(conn); receipt=apply_plan(conn,plan)
        assert not receipt['replayed']
    with reading(engine) as conn:
        assert public_fingerprints(conn)==before
        assert public_structure(conn)==structure
        assert conn.execute(text('SELECT version_num FROM public.alembic_version')).scalar_one()=='20261027_0053'
        assert conn.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()=='p1_0001'
        assert conn.execute(text('SELECT legacy_po_id FROM purchasing_v4.legacy_procurement_reference')).scalar_one()==129
        assert not conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='purchasing_v4' AND tablename IN ('commitment','authorization','supplier_assignment')")).all()
    assert_supported_schema(engine)  # Existing public web/cron startup contract.


def test_transactional_migration_interruption_and_forward_resume(engine):
    with pytest.raises(RuntimeError,match='interruption'):
        with engine.begin() as conn:
            migrate(conn)
            raise RuntimeError('simulated interruption')
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regnamespace('purchasing_v4')")).scalar_one() is None
    with engine.begin() as conn:migrate(conn)
    with engine.begin() as conn:migrate(conn)  # Same version, no repeated DDL.
    with engine.begin() as conn:
        with pytest.raises(RuntimeError,match='retained'):downgrade(conn)


def test_import_interruption_replay_and_two_concurrent_retries(engine):
    plan=get_plan(engine)
    with engine.begin() as conn:migrate(conn)
    with pytest.raises(RuntimeError,match='interrupted'):
        with engine.begin() as conn:
            apply_plan(conn,plan)
            raise RuntimeError('interrupted before commit')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM purchasing_v4.import_run')).scalar_one()==0
    def apply():
        with engine.begin() as conn:return apply_plan(conn,plan)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:apply(),range(2)))
    assert sorted(r['replayed'] for r in results)==[False,True]
    with engine.begin() as conn:
        assert apply_plan(conn,plan)['replayed']
        assert conn.execute(text('SELECT count(*) FROM purchasing_v4.variation')).scalar_one()==2


@pytest.mark.parametrize('operation', ['UPDATE purchasing_v4.variation SET product_id=NULL',
    'DELETE FROM purchasing_v4.source_observation','TRUNCATE purchasing_v4.identity_exception'])
def test_immutable_database_enforcement(engine,operation):
    plan=get_plan(engine)
    with engine.begin() as conn:migrate(conn);apply_plan(conn,plan)
    with pytest.raises(DBAPIError,match='immutable'):
        with engine.begin() as conn:conn.execute(text(operation))


def test_conflicting_parent_requires_review_and_cannot_leave_half_import(engine):
    plan=get_plan(engine)
    with engine.begin() as conn:migrate(conn);apply_plan(conn,plan)
    with engine.begin() as conn:
        conn.execute(text("UPDATE ordering_catalog_identity SET square_item_id='CONFLICTING_ITEM' WHERE square_variation_id='FIXTURE_V1'"))
    changed=get_plan(engine)
    with engine.begin() as conn:
        with pytest.raises(IdentityCollisionError):apply_plan(conn,changed)
        assert conn.execute(text('SELECT count(*) FROM purchasing_v4.import_run')).scalar_one()==1
        assert conn.execute(text('SELECT count(*) FROM purchasing_v4.product')).scalar_one()==1


def test_read_contract_rejects_rw_and_incoherent_snapshot_and_rechecks(engine):
    with engine.connect() as conn:
        with pytest.raises(PermissionError,match='read-only'):ReadOnlyEvidence(conn)
    with engine.connect().execution_options(postgresql_readonly=True) as conn:
        with pytest.raises(PermissionError,match='repeatable'):ReadOnlyEvidence(conn)
    with reading(engine) as conn:
        reader=ReadOnlyEvidence(conn)
        assert reader.inventory('FIXTURE_V1',1,as_of=AS_OF,max_age=AGE).value==0
        with pytest.raises(DBAPIError):conn.execute(text('UPDATE public.vendors SET name=name'))


def test_readonly_inventory_sales_availability_and_incoming(engine):
    with reading(engine) as conn:
        before=public_fingerprints(conn)
        reader=ReadOnlyEvidence(conn)
        assert reader.inventory('FIXTURE_V1',1,as_of=AS_OF,max_age=AGE).state==EvidenceState.KNOWN
        assert reader.inventory('FIXTURE_V2',1,as_of=AS_OF,max_age=AGE).state==EvidenceState.UNAVAILABLE
        sales=reader.sales('FIXTURE_V1',1,date(2026,10,25),date(2026,10,27),as_of=AS_OF,max_age=AGE)
        assert sales[date(2026,10,25)].value==0 and sales[date(2026,10,26)].value==2
        assert reader.sales('missing',1,date(2026,10,25),date(2026,10,27),as_of=AS_OF,max_age=AGE)[date(2026,10,25)].state==EvidenceState.UNKNOWN
        assert reader.incoming('FIXTURE_V1',1).value==30
        assert reader.incoming('FIXTURE_V2',1).value==0
        assert reader.incoming('missing',1).state==EvidenceState.UNKNOWN
        availability=reader.availability('FIXTURE_V1',1,date(2026,10,25),date(2026,10,27),as_of=AS_OF,max_age=AGE)
        assert availability[0].availability==Availability.STOCKOUT
        assert availability[1].availability==Availability.UNKNOWN
        assert availability[1].reason=='CONTRADICTORY_SALES_DURING_STOCKOUT'
        assert public_fingerprints(conn)==before


@pytest.mark.parametrize('mutation,expected', [
    ("DELETE FROM purchasing_history_coverage WHERE source='SALES'", EvidenceState.UNAVAILABLE),
    ("UPDATE purchasing_history_coverage SET through_at='2026-10-25T00:00Z' WHERE source='SALES'", EvidenceState.UNKNOWN),
    ("UPDATE purchasing_history_coverage SET verified_at='2026-10-01T00:00Z',through_at='2026-10-01T00:00Z',start_at='2026-09-01T00:00Z' WHERE source='SALES'", EvidenceState.UNAVAILABLE)])
def test_unavailable_incomplete_and_stale_sales_do_not_become_zero(engine,mutation,expected):
    with engine.begin() as conn:conn.execute(text(mutation))
    with reading(engine) as conn:
        result=ReadOnlyEvidence(conn).sales('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)
        assert result[date(2026,10,26)].state==expected and result[date(2026,10,26)].value is None


def test_corrected_sales_preserve_original_money_and_detect_future_revision(engine):
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO purchasing_sale_corrections(kind,square_order_id,square_line_item_uid,
            location_id,variation_id,business_date,quantity,reason,source_digest,synchronized_at)
            VALUES ('LINE','FIXTURE_ORDER','FIXTURE_LINE','FIXTURE_LOC1','FIXTURE_V1','2026-10-26',3,'FIXTURE',:sha,'2026-10-27T11:00Z')"""), {'sha':'a'*64})
    with reading(engine) as conn:
        reader=ReadOnlyEvidence(conn)
        assert reader.sales('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)[date(2026,10,26)].value==3
        assert conn.execute(text('SELECT net_sales_amount FROM consignment_sale_facts WHERE id=1')).scalar_one()==20
    with engine.begin() as conn:conn.execute(text("UPDATE purchasing_sale_corrections SET synchronized_at='2026-10-28T00:00Z'"))
    with reading(engine) as conn:
        result=ReadOnlyEvidence(conn).sales('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)
        assert result[date(2026,10,26)].reason=='SOURCE_CHANGED_AFTER_CUTOFF'


@pytest.mark.parametrize('mutation', [
    'UPDATE purchase_order_store_allocations SET store_received_qty=NULL WHERE store_id=1',
    'UPDATE purchase_order_store_allocations SET allocated_qty=90 WHERE store_id=1',
    "UPDATE purchase_order_lines SET variation_id='SKU::unresolved'",
    'DELETE FROM purchase_order_store_allocations'])
def test_incoming_conflicts_are_not_proportionally_resolved_or_zero_filled(engine,mutation):
    with engine.begin() as conn:conn.execute(text(mutation))
    with reading(engine) as conn:
        result=ReadOnlyEvidence(conn).incoming('FIXTURE_V1',1)
        assert result.state==EvidenceState.UNKNOWN and result.value is None


def test_endpoint_positive_does_not_prove_available_through_intraday_stockout(engine):
    with engine.begin() as conn:
        conn.execute(text('UPDATE ordering_current_inventory SET counted_quantity=5'))
        for identity,delta,stamp in [('out',-5,'2026-10-26T16:00Z'),('in',5,'2026-10-26T18:00Z')]:
            conn.execute(text("""INSERT INTO purchasing_inventory_events(event_type,event_id,location_id,variation_id,
               occurred_at,business_date,quantity_delta,physical_count,source_updated_at,calculated_after_at,
               calculated_before_at,source_digest,synchronized_at) VALUES ('ADJUSTMENT',:id,'FIXTURE_LOC1',
               'FIXTURE_V1',:stamp,'2026-10-26',:delta,false,:stamp,:stamp,:stamp,:sha,'2026-10-27T11:00Z')"""),
               dict(id=identity,delta=delta,stamp=stamp,sha='b'*64))
    with reading(engine) as conn:
        result=ReadOnlyEvidence(conn).availability('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)
        assert result[0].availability==Availability.UNKNOWN


def test_isolation_guard_rejects_named_database_outside_scope_before_ddl(template):
    admin,_=template
    with admin.connect() as conn:
        with pytest.raises(PermissionError):migrate(conn)


@pytest.mark.parametrize('url', [
    'postgresql+psycopg://user@production.example/erupted_v4_phase1_x',
    'postgresql+psycopg://user@127.0.0.1/production',
    'postgresql+psycopg://user@127.0.0.1/erupted_v4_phase1_x?host=production.example',
    'postgresql+psycopg://user@127.0.0.1/erupted_v4_phase1_x?service=production',
    'sqlite:///erupted_v4_phase1_x'])
def test_bad_endpoint_rejected_without_connecting(url):
    with pytest.raises(PermissionError):require_isolated_url(url)


def test_tampered_plan_cannot_fabricate_supplier_reference(engine):
    plan=get_plan(engine); data=plan.data
    data['suppliers'][0]['square_vendor_id']='INVENTED'
    with engine.begin() as conn:
        migrate(conn)
        with pytest.raises(ValueError,match='provenance derivation'):
            apply_plan(conn,IdentityPlan(canonical(data)))
        assert conn.execute(text('SELECT count(*) FROM purchasing_v4.supplier')).scalar_one()==0


def test_source_changed_between_plan_and_apply_fails_before_any_identity_write(engine):
    plan=get_plan(engine)
    with engine.begin() as conn:
        migrate(conn)
        conn.execute(text("UPDATE vendor_sku_configs SET unit_cost=12 WHERE id=1"))
        with pytest.raises(IdentityCollisionError):apply_plan(conn,plan)
        assert conn.execute(text('SELECT count(*) FROM purchasing_v4.import_run')).scalar_one()==0


def test_missing_open_identity_with_proven_no_remaining_supply_does_not_poison_catalog(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE purchase_order_lines SET variation_id='SKU::old',ordered_qty=0,received_qty_total=1"))
    with reading(engine) as conn:
        assert ReadOnlyEvidence(conn).incoming('FIXTURE_V2',1).value==0


def test_repository_cannot_reuse_cached_supply_across_transactions(engine):
    with readonly_snapshot(engine) as conn:
        reader=ReadOnlyEvidence(conn)
        assert reader.incoming('FIXTURE_V1',1).value==30
        conn.rollback()
        with pytest.raises(PermissionError,match='cross snapshot'):
            reader.incoming('FIXTURE_V1',1)


def test_positive_trajectory_is_confirmed_and_physical_count_is_unknown(engine):
    with engine.begin() as conn:conn.execute(text('UPDATE ordering_current_inventory SET counted_quantity=5'))
    with reading(engine) as conn:
        assert ReadOnlyEvidence(conn).availability('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)[0].availability==Availability.AVAILABLE
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO purchasing_inventory_events(event_type,event_id,location_id,variation_id,
            occurred_at,business_date,quantity_delta,physical_count,source_updated_at,calculated_after_at,
            calculated_before_at,source_digest,synchronized_at) VALUES ('PHYSICAL_COUNT','count','FIXTURE_LOC1',
            'FIXTURE_V1','2026-10-26T16:00Z','2026-10-26',0,true,'2026-10-26T16:00Z',
            '2026-10-26T16:00Z','2026-10-26T16:00Z',:sha,'2026-10-27T11:00Z')"""),{'sha':'c'*64})
    with reading(engine) as conn:
        assert ReadOnlyEvidence(conn).availability('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)[0].availability==Availability.UNKNOWN


def test_decimal_provenance_retains_full_legacy_cost_precision(engine):
    with engine.begin() as conn:conn.execute(text('UPDATE vendor_sku_configs SET unit_cost=9999999999.9999 WHERE id=1'))
    plan=get_plan(engine)
    observation=next(r for r in plan.data['observations'] if r['source_table']=='vendor_sku_configs')
    assert observation['evidence']['unit_cost']=='9999999999.9999'
    with engine.begin() as conn:migrate(conn);apply_plan(conn,plan)


def test_source_provenance_is_stable_across_connection_timezones(engine):
    original=get_plan(engine)
    tokyo=create_engine(engine.url,connect_args={'options':'-c timezone=Asia/Tokyo'})
    try:
        assert get_plan(tokyo).document==original.document
    finally:tokyo.dispose()


def test_sales_uses_real_certificate_time_and_inventory_has_source_references(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE purchasing_history_coverage SET verified_at='2026-10-27T11:00Z',through_at='2026-10-27T11:00Z' WHERE source='SALES'"))
    with reading(engine) as conn:
        reader=ReadOnlyEvidence(conn)
        row=reader.sales('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)[date(2026,10,26)]
        assert row.observed_at==AS_OF-timedelta(hours=1) and row.references
        assert reader.inventory('FIXTURE_V1',1,as_of=AS_OF,max_age=AGE).references
        assert reader.incoming('FIXTURE_V1',1).references==('purchase_order_lines:1',)


def test_impossible_coverage_verification_is_unknown_not_zero(engine):
    with engine.begin() as conn:
        conn.execute(text("UPDATE purchasing_history_coverage SET verified_at='2026-10-27T11:00Z' WHERE source='SALES'"))
    with reading(engine) as conn:
        row=ReadOnlyEvidence(conn).sales('FIXTURE_V1',1,date(2026,10,26),date(2026,10,27),as_of=AS_OF,max_age=AGE)[date(2026,10,26)]
        assert row.state==EvidenceState.UNKNOWN and row.reason=='INVALID_COVERAGE_CERTIFICATE'
