from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.models import PurchasingHistoryCoverage
from app.forecast_validation.weekly import source_preflight
from app.forecast_validation.contracts import EvidenceError,TZ
from test_purchasing_receiving_extension import database_template,receiving_db

@pytest.mark.parametrize('fault',[None,'missing','stale','incomplete'])
def test_source_preflight_rejects_missing_stale_or_incomplete_before_collection(receiving_db,fault):
    now=datetime(2026,10,22,18,tzinfo=timezone.utc)
    through=datetime.combine(now.astimezone(TZ).date(),datetime.min.time(),TZ)
    with Session(receiving_db) as db:
        for source in ('SALES','MOVEMENTS'):
            for i in range(1,5):
                if fault=='missing' and source=='SALES' and i==1:continue
                db.add(PurchasingHistoryCoverage(source=source,location_id=f'L{i}',
                    start_at=through-timedelta(days=84),
                    through_at=through-timedelta(days=1) if fault=='incomplete' else through,
                    verified_at=now-timedelta(days=2) if fault=='stale' else now))
        db.commit()
    engine=create_engine(receiving_db.url,isolation_level='REPEATABLE READ',pool_size=1,max_overflow=0,
                         connect_args={'options':'-c default_transaction_read_only=on'})
    try:
        if fault:
            with pytest.raises(EvidenceError):source_preflight(engine,now=now)
        else:assert source_preflight(engine,now=now)['status']=='COMPLETE'
        assert engine.pool.checkedout()==0
    finally:engine.dispose()
