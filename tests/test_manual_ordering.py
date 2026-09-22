from decimal import Decimal
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from app.models import Base, Store, Vendor, VendorSkuConfig, PurchaseOrder, PurchaseOrderLine, PurchaseOrderStoreAllocation, PurchaseOrderStatus, OrderingProductLifecycle
from app.services.manual_ordering_service import add_catalog_product, search_products, purchase_cost
from app.services.purchase_order_generation_service import list_selected_vendor_skus
from app.services.purchase_order_admin_service import receive_purchase_order


@pytest.fixture
def db():
    engine = create_engine('sqlite://')
    @event.listens_for(engine, 'connect')
    def functions(conn, _):
        conn.create_function('char_length', 1, lambda value: len(value) if value is not None else None)
    tables = ['stores','vendors','vendor_sku_configs','purchase_orders','purchase_order_lines','purchase_order_store_allocations','ordering_product_lifecycle','square_sync_events','principal_permission_overrides','role_permission_overrides']
    # audit's PostgreSQL INET is intentionally stubbed for these focused unit integrations.
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[t] for t in tables])
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE audit_log (id INTEGER PRIMARY KEY, actor_principal_id BIGINT, action TEXT, session_id BIGINT, ip TEXT, metadata JSON, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)')
    session = Session(engine)
    seq = [100]
    @event.listens_for(session, 'before_flush')
    def ids(session, *_):
        for row in session.new:
            if hasattr(row, 'id') and row.id is None:
                seq[0] += 1; row.id = seq[0]
    session.add_all([Store(id=1,name='99',square_location_id='LOC',active=True),Vendor(id=1,name='M&M',square_vendor_id='SQ1',active=True),Vendor(id=2,name='Other',square_vendor_id='SQ2',active=True),PurchaseOrder(id=1,vendor_id=1,created_by_principal_id=1,status=PurchaseOrderStatus.DRAFT)])
    session.commit()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def catalog():
    with patch('app.services.manual_ordering_service.catalog_variations', return_value={
        'V1': SimpleNamespace(sku='SKU1',gtin='123',item_name='Juice Head Blueberry Lemon',variation_name='6mg',unit_price=Decimal('20')),
        'V2': SimpleNamespace(sku=None,gtin=None,item_name='Skittles',variation_name='Original',unit_price=None),
    }): yield


def test_search_unordered_partial_and_vendor_independent(db):
    assert search_products(db,vendor_id=1,query='6 blue')[0]['variation_id']=='V1'
    assert search_products(db,vendor_id=1,query='juice h')[0]['name'].startswith('Juice Head')
    assert search_products(db,vendor_id=1,query='123')[0]['variation_id']=='V1'
    assert search_products(db,vendor_id=1,query='skitt')[0]['variation_id']=='V2'


@pytest.mark.parametrize('mapped', [None, False, True])
def test_manual_add_without_configuration_side_effects_and_receive(db, mapped):
    if mapped is not None:
        db.add(VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='V1',unit_cost=Decimal('5'),active=True,is_default_vendor=mapped,pack_size=1,min_order_qty=0)); db.flush()
    db.add(OrderingProductLifecycle(square_variation_id='V1',status='NO_FUTURE_REORDER',no_future_reorder_at=datetime.now(timezone.utc),no_future_reorder_by_principal_id=1))
    line=add_catalog_product(db,purchase_order_id=1,variation_id='V1',initial_qty=3,unit_cost='0',actor_id=1)
    assert line.unit_cost == 0 and line.variation_id == 'V1'
    assert len(db.scalars(select(VendorSkuConfig)).all()) == (mapped is not None)
    assert db.get(OrderingProductLifecycle,'V1').status == 'NO_FUTURE_REORDER'
    allocation=db.scalar(select(PurchaseOrderStoreAllocation)); allocation.store_received_qty=3
    line.received_qty_total=3; db.get(PurchaseOrder,1).status=PurchaseOrderStatus.IN_TRANSIT; db.flush()
    with patch('app.services.purchase_order_admin_service._square_post', return_value={}) as push, patch('app.services.v2_order_payments_service.initialize_new_order_if_configured'):
        result=receive_purchase_order(db,purchase_order_id=1,actor_principal_id=1)
        assert result['succeeded']==1
        assert Decimal(push.call_args.args[1]['changes'][0]['adjustment']['quantity']) == 3


def test_identity_duplicates_cost_and_skuless(db):
    for invalid in ['', '-1', 'NaN', 'Infinity', '.00001']:
        with pytest.raises(ValueError): purchase_cost(invalid)
    with pytest.raises(ValueError): add_catalog_product(db,purchase_order_id=1,variation_id='missing',initial_qty=1,unit_cost='1',actor_id=1)
    line=add_catalog_product(db,purchase_order_id=1,variation_id='V2',initial_qty=1,unit_cost='2',actor_id=1)
    assert line.sku is None and line.variation_id=='V2'
    with pytest.raises(ValueError): add_catalog_product(db,purchase_order_id=1,variation_id='V2',initial_qty=1,unit_cost='3',actor_id=1)
    assert line.unit_cost==2


def test_automation_remains_mapped_and_honors_lifecycle(db):
    db.add_all([VendorSkuConfig(vendor_id=1,sku='SKU1',square_variation_id='V1',active=True,is_default_vendor=True),VendorSkuConfig(vendor_id=2,sku='SKU2',square_variation_id='V2',active=True,is_default_vendor=False)])
    db.flush()
    assert list_selected_vendor_skus(db,vendor_ids=[1,2]).keys()=={1}
    db.add(OrderingProductLifecycle(square_variation_id='V1',status='NO_FUTURE_REORDER',no_future_reorder_at=datetime.now(timezone.utc),no_future_reorder_by_principal_id=1)); db.flush()
    assert list_selected_vendor_skus(db,vendor_ids=[1])=={}


def test_removed_catalog_line_restores_without_changing_historical_cost(db):
    line=add_catalog_product(db,purchase_order_id=1,variation_id='V1',initial_qty=2,unit_cost='4',actor_id=1)
    line.removed=True;db.flush()
    with pytest.raises(ValueError): add_catalog_product(db,purchase_order_id=1,variation_id='V1',initial_qty=3,unit_cost='9',actor_id=1)
    restored=add_catalog_product(db,purchase_order_id=1,variation_id='V1',initial_qty=3,unit_cost='4',actor_id=1)
    assert restored.id==line.id and restored.unit_cost==4 and not restored.removed
    assert restored.ordered_qty==3 and len(db.scalars(select(PurchaseOrderLine)).all())==1


def test_ordering_capability_is_explicit_and_separate_from_admin(db):
    from fastapi import HTTPException
    from app.auth import Principal, Role
    from app.routers.management import ordering_access, admin_access
    from app.models import PrincipalPermissionOverride
    lead=Principal(1,'designated',Role.LEAD,None,True)
    with pytest.raises(HTTPException): ordering_access(principal=lead,db=db)
    db.add(PrincipalPermissionOverride(principal_id=1,permission_key='ordering.manage',allowed=True));db.flush()
    assert ordering_access(principal=lead,db=db)==lead
    with pytest.raises(HTTPException): admin_access(principal=lead,db=db)
