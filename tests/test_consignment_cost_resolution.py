"""Real disposable PostgreSQL: cost resolution, durable choices, atomic audit."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace as Obj
import copy
import pytest
from sqlalchemy import select,text,func
from sqlalchemy.orm import Session
from test_purchasing_receiving_extension import database_template,receiving_db
from test_consignment_reporting import Reader,order
from app.models import (VendorSkuConfig,OrderingCatalogIdentity,ConsignmentReportingDraft,
    ConsignmentReportingPreference,AuditLog,Principal)
from app.services import consignment_reporting_service as reporting,consignment_report_resolution_service as service

@pytest.fixture
def scenario(receiving_db):
    with Session(receiving_db) as db:
        db.add(VendorSkuConfig(id=501,vendor_id=1,sku='G635369',square_variation_id='BLUE',unit_cost=0,active=True,is_default_vendor=True))
        db.add(VendorSkuConfig(id=502,vendor_id=1,sku='GOOD',square_variation_id='GOOD',unit_cost=10,active=True,is_default_vendor=True));db.commit()
    return receiving_db

def raw(db,orders=None):
    items=orders if orders is not None else [order('a','2',vid='BLUE',location='L1',stamp='2026-10-12T12:00:00Z'),order('b','1',vid='BLUE',location='L2',stamp='2026-10-12T12:00:00Z'),order('c','4',vid='GOOD',location='L3',stamp='2026-10-12T12:00:00Z')]
    items=copy.deepcopy(items)
    for o in items:
        for line in o['line_items']:
            if line['catalog_object_id']=='BLUE':line.update(name='Juice Head Flex Freeze 50k Disposable',variation_name='Blue Razz')
    return reporting.sales_report(db,vendor_id=1,start_date=date(2026,10,11),end_date=date(2026,10,17),reader=Reader(items))

def draft(db,orders=None):
    d=service.create(db,report=raw(db,orders),actor_id=1);db.commit();return d

def choice(action='cost',cost='12.5',remember=False,**kw):return {'501':dict(action=action,cost=cost,remember=remember,**kw)}
def save(db,d,decisions,**kw):
    result=service.save(db,draft_id=d.id,actor_id=1,revision=d.revision,decisions=decisions,request_key=kw.pop('request_key','test-request-0001'),**kw);db.commit();return result


def test_zero_is_unresolved_and_square_name_fallback(scenario):
    with Session(scenario) as db:
        d=draft(db);v=service.calculate(d)
        assert not v['complete'] and v['cogs'] is None and v['known_cogs']=='40.0000' and v['net_units']=='7'
        assert v['resolutions'][0]['name']=='Juice Head Flex Freeze 50k Disposable — Blue Razz'
        assert len(v['resolutions'][0]['stores'])==2
        assert db.get(VendorSkuConfig,501).unit_cost==0


def test_one_override_all_stores_and_report_only_persists(scenario):
    with Session(scenario) as db:
        d=draft(db);v=service.calculate(d,choice());assert v['cogs']=='77.5000' and v['net_units']=='7'
        save(db,d,choice(),finalize=True);rid=d.id;assert db.get(VendorSkuConfig,501).unit_cost==0
    with Session(scenario) as db:
        d=db.get(ConsignmentReportingDraft,rid);v=service.calculate(d)
        assert v['cogs']=='77.5000' and v['status']=='FINALIZED'
        assert not service.calculate(draft(db))['complete']
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='CONSIGNMENT_REPORT_FINALIZED'))==1


def test_leave_unresolved_save_allowed_finalize_blocked(scenario):
    with Session(scenario) as db:
        d=draft(db);save(db,d,choice('unresolved'))
        with pytest.raises(ValueError,match='Resolve or exclude'):save(db,d,choice('unresolved'),finalize=True,request_key='test-finalize-0001')
        db.rollback();assert db.get(ConsignmentReportingDraft,d.id).status=='DRAFT'


@pytest.mark.parametrize('cost',['-1','NaN','Infinity','1.12345','10000000000',''])
def test_invalid_cost_rejected(scenario,cost):
    with Session(scenario) as db:
        d=draft(db)
        with pytest.raises(ValueError):service.calculate(d,choice(cost=cost))


def test_explicit_zero_confirmation_remembered_and_invalidated_by_new_edit(scenario):
    with Session(scenario) as db:
        d=draft(db)
        with pytest.raises(ValueError,match='Explicitly confirm'):service.calculate(d,choice(cost='0'))
        save(db,d,choice(cost='0',remember=True,confirm_zero=True),finalize=True)
        assert service.calculate(d)['cogs']=='40.0000'
        new=draft(db);assert service.calculate(new)['complete']
        db.execute(text('update vendor_sku_configs set unit_cost=0 where id=501'));db.commit()
        assert not service.calculate(draft(db))['complete']


def test_saved_cost_applies_to_future_reports_and_exclusions_are_reversible(scenario):
    with Session(scenario) as db:
        d=draft(db);save(db,d,choice(cost='13.08',remember=True),finalize=True)
        assert db.get(VendorSkuConfig,501).unit_cost==D('13.08')
        nxt=draft(db);assert service.calculate(nxt)['cogs']=='79.2400'
        save(db,nxt,choice('exclude',remember=True),finalize=True)
        v=service.calculate(nxt);assert v['net_units']=='4' and v['cogs']=='40.0000' and v['excluded_net_units']=='3'
        new=draft(db);assert service.calculate(new)['excluded_net_units']=='3'
        save(db,new,choice('include',remember=True),finalize=True)
        assert service.calculate(draft(db))['net_units']=='7'
        # The old final report retains its original explicit exclusion.
        assert service.calculate(nxt)['excluded_net_units']=='3'


def test_remembered_exclusion_can_be_reversed_in_zero_sales_period(scenario):
    with Session(scenario) as db:
        d=draft(db);save(db,d,choice('exclude',remember=True),finalize=True)
        empty=draft(db,[]);v=service.calculate(empty);assert v['net_units']=='0' and v['resolutions'][0]['stores']==[]
        save(db,empty,choice('include',remember=True),finalize=True)
        assert not db.get(ConsignmentReportingPreference,501).excluded


def test_save_is_idempotent_and_finalized_is_immutable(scenario):
    with Session(scenario) as db:
        d=draft(db);original_revision=d.revision;payload=choice('exclude',remember=True)
        save(db,d,payload,finalize=True);audit=db.scalar(select(func.count()).select_from(AuditLog))
        service.save(db,draft_id=d.id,actor_id=1,revision=original_revision,decisions=payload,request_key='test-request-0001',finalize=True);db.commit()
        assert db.scalar(select(func.count()).select_from(AuditLog))==audit
        with pytest.raises(service.Conflict):save(db,d,payload,request_key='different-key')


def test_conflicting_reports_cannot_overwrite_future_cost(scenario):
    with Session(scenario) as db:
        first=draft(db);second=draft(db);save(db,first,choice(cost='15',remember=True))
        with pytest.raises(service.Conflict):save(db,second,choice(cost='20',remember=True))
        db.rollback();assert db.get(VendorSkuConfig,501).unit_cost==15


def test_concurrent_saves_one_wins(scenario):
    with Session(scenario) as db:d=draft(db);rid=d.id;revision=d.revision
    def attempt(n):
        with Session(scenario) as db:
            try:
                service.save(db,draft_id=rid,actor_id=1,revision=revision,decisions=choice(cost=str(n)),request_key=f'concurrent-key-{n}');db.commit();return 'saved'
            except service.Conflict:db.rollback();return 'conflict'
    with ThreadPoolExecutor(2) as pool:assert sorted(pool.map(attempt,[12,13]))==['conflict','saved']


def test_csv_exclusions_unresolved_and_override_provenance(scenario):
    with Session(scenario) as db:
        d=draft(db);data=service.export_csv(service.calculate(d));assert 'INCOMPLETE' in data and 'UNRESOLVED' in data
        save(db,d,choice('exclude'),finalize=True);data=service.export_csv(service.calculate(d))
        assert 'FINALIZED' in data and 'EXCLUDED,G635369' in data and 'TOTAL INCLUDED,,,,4,0,4' in data and 'TOTAL EXCLUDED,,,,,,3' in data


def test_permission_and_vendor_scope_fail_closed(scenario):
    with Session(scenario) as db:
        d=draft(db);db.get(Principal,1).active=False;db.flush()
        with pytest.raises(PermissionError):service.save(db,draft_id=d.id,actor_id=1,revision=1,decisions={},request_key='unauthorized')
        db.rollback()
        with pytest.raises(ValueError,match='not part'):service.calculate(d,{'999':{'action':'exclude'}})


def test_four_decimal_cost_not_rounded_before_total(scenario):
    with Session(scenario) as db:
        d=draft(db);v=service.calculate(d,choice(cost='12.3456'))
        assert v['cogs']=='77.0368'
        save(db,d,choice(cost='12.3456',remember=True),finalize=True)
        assert db.get(VendorSkuConfig,501).unit_cost==D('12.3456')
        assert '77.0368' in service.export_csv(service.calculate(d))


def test_multi_sku_future_update_rolls_back_on_conflict(scenario):
    with Session(scenario) as db:
        d=draft(db)
        db.execute(text('update vendor_sku_configs set unit_cost=11 where id=502'));db.commit()
        choices=choice(cost='12.50',remember=True);choices['502']={'action':'cost','cost':'15','remember':True}
        with pytest.raises(service.Conflict):save(db,d,choices)
        db.rollback()
        assert db.get(VendorSkuConfig,501).unit_cost==0
        assert db.get(VendorSkuConfig,502).unit_cost==11
        assert db.get(ConsignmentReportingPreference,501) is None


def test_configuration_change_during_square_read_requires_fresh_report(scenario):
    with Session(scenario) as db:
        old_source=raw(db)
        db.execute(text('update vendor_sku_configs set unit_cost=15 where id=501'));db.commit()
        with pytest.raises(service.Conflict,match='while this report was loading'):
            service.create(db,report=old_source,actor_id=1)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(ConsignmentReportingDraft))==0
