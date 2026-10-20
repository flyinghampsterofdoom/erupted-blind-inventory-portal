import pytest
from sqlalchemy import create_engine,update
from sqlalchemy.orm import Session
from app.models import Vendor
from app.services import purchasing_attention_summary as s


def test_summary_lifecycle_and_committed_invalidation(monkeypatch):
    engine=create_engine('sqlite://');Vendor.__table__.create(engine)
    with Session(engine) as db:
        assert s.summary(db,1)['state']=='unavailable'
        s.remember(db,1,{'sku_count':4,'configuration_count':2})
        assert s.summary(db,1)['state']=='current'
        assert s.summary(db,2)['state']=='unavailable'
        assert s.summary(db,1)['sku_count']==4
        db.add(Vendor(id=1,square_vendor_id='test',name='Supplier',active=True));db.commit()
        assert s.summary(db,1)['state']=='stale'
        s.remember(db,1,{'sku_count':0,'configuration_count':0})
        db.execute(update(Vendor).where(Vendor.id==1).values(name='Changed'));db.rollback()
        assert s.summary(db,1)['state']=='current'
        monkeypatch.setattr(s,'TTL_SECONDS',0)
        assert s.summary(db,1)['state']=='stale'
    engine.dispose()


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_savepoint_end_does_not_publish_outer_uncommitted_summary(finish):
    engine = create_engine('sqlite://')
    Vendor.__table__.create(engine)
    with Session(engine) as db:
        s.remember(db, 1, {'sku_count': 4, 'configuration_count': 2})
        db.add(Vendor(id=1, square_vendor_id='test', name='Outer uncommitted', active=True))
        db.flush()
        nested = db.begin_nested()
        getattr(nested, finish)()
        s.remember(db, 1, {'sku_count': 999, 'configuration_count': 999})
        assert s.summary(db, 1)['sku_count'] == 4
        db.commit()
        assert s.summary(db, 1)['state'] == 'stale'
    engine.dispose()
