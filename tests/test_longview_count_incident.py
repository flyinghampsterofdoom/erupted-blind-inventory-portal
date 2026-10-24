"""Synthetic quantities under temporary Longview coverage; no production access."""
from datetime import time, timedelta
from unittest.mock import Mock
from uuid import uuid4
from decimal import Decimal
import pytest
from sqlalchemy import select
from app import models as m
from app.services import inventory_working_store_service as scope
from app.services.blind_count_service import save_draft, submit_round
from app.services.v2_scheduling_exception_service import record_commitment, cancel_commitment
from app.security.sessions import load_session_from_token
from test_coverage_operational_access import scheduling_db, _scenario, _now


def test_temporary_longview_count_expiration_reassignment_and_duplicate_submit(scheduling_db, monkeypatch):
    Session, actor, ids, _ = scheduling_db
    with Session() as db:
        _, shifts, people = _scenario(db, actor, ids)
        db.get(m.Store,ids['north']).name='Longview'
        commitment=record_commitment(db,principal=actor,shift_id=shifts[0].id,
            employee_id=ids['blair'],start_time=time(9),end_time=time(17))
        original_context=scope.working_context
        monkeypatch.setattr(scope,'working_context',lambda db,**kw:original_context(db,**{**kw,'now':_now(12)}))
        principal,context=scope.inventory_principal(db,principal=people['blair'])
        assert principal.store_id==ids['north']
        campaign=m.Campaign(label='Synthetic Freebase')
        db.add(campaign);db.flush()
        count=m.CountSession(store_id=ids['north'],campaign_id=campaign.id,
            employee_name='Synthetic Cameron',created_by_principal_id=principal.id)
        db.add(count);db.flush()
        db.add(m.SnapshotLine(session_id=count.id,variation_id='synthetic-v1',item_name='Synthetic Freebase',variation_name='6mg'))
        db.flush()
        token=uuid4().hex
        db.add(m.WebSession(session_token=token,principal_id=principal.id,expires_at=_now(12)+timedelta(minutes=30)))
        db.flush()
        monkeypatch.setattr('app.security.sessions._now',lambda:_now(12))
        assert load_session_from_token(db,token,renew=False)
        result=save_draft(db,principal=principal,session_id=count.id,revision=0,
            changes={'synthetic-v1':{'front_qty':'12','back_qty':'34'}},operation_id='original')
        db.commit();saved=count.id
        monkeypatch.setattr('app.security.sessions._now',lambda:_now(13))
        assert load_session_from_token(db,token,renew=False) is None
        assert db.get(m.Entry,(saved,'synthetic-v1')).counted_qty==Decimal('46')
        cancel_commitment(db,principal=actor,commitment_id=commitment.id);db.commit()
        with pytest.raises(scope.WorkingStoreConflict):
            save_draft(db,principal=principal,session_id=saved,revision=result['revision'],changes={},operation_id='revoked')
        db.rollback()
        with pytest.raises(scope.WorkingStoreConflict):
            submit_round(db,principal=principal,session_id=saved,revision=result['revision'],provider=Mock())
        db.rollback()
        assert db.get(m.Entry,(saved,'synthetic-v1')).counted_qty==Decimal('46')
        record_commitment(db,principal=actor,shift_id=shifts[0].id,employee_id=ids['blair'],start_time=time(9),end_time=time(17))
        principal,_=scope.inventory_principal(db,principal=people['blair'])
        provider=Mock();provider.fetch_current_on_hand.return_value={'synthetic-v1':Decimal('46')}
        submit_round(db,principal=principal,session_id=saved,revision=result['revision'],provider=provider);db.commit()
        submit_round(db,principal=principal,session_id=saved,revision=result['revision'],provider=provider);db.commit()
        observations=list(db.scalars(select(m.CountObservation).where(m.CountObservation.session_id==saved)))
        assert len(observations)==1 and observations[0].total_qty==Decimal('46')
        assert provider.fetch_current_on_hand.call_count==1
