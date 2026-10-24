"""Existing continuation data remains safe even on the compatible rollback."""
from decimal import Decimal
from unittest.mock import Mock
from sqlalchemy import select, func
from app import models as m
from app.auth import Principal, Role
from app.services.blind_count_service import save_draft, submit_round
from test_blind_count_reliability import engine, ctx


def test_saved_continuation_breaks_automatic_correction_qualification(ctx):
    c=ctx;provider=Mock();provider.fetch_current_on_hand.return_value={'V1':Decimal('10')}
    first=None
    for i in range(3):
        source=None
        if i==2:
            source=m.StoreForcedCount(store_id=c.store.id,campaign_id=c.campaign.id,
                source_session_id=first,reason='BLIND_COUNT_UNLOCK_CONTINUATION',
                created_by_principal_id=c.lead.id,active=False)
            c.db.add(source);c.db.flush()
        count=m.CountSession(store_id=c.store.id,campaign_id=c.campaign.id,employee_name='Employee',
            created_by_principal_id=c.principal.id,source_forced_count_id=source.id if source else None)
        c.db.add(count);c.db.flush()
        c.db.add(m.SnapshotLine(session_id=count.id,variation_id='V1',item_name='Product',variation_name='Test'));c.db.flush()
        save_draft(c.db,principal=c.principal,session_id=count.id,revision=0,operation_id=f'save-{i}',changes={'V1':{'front_qty':'2','back_qty':'3'}})
        _,ids=submit_round(c.db,principal=c.principal,session_id=count.id,revision=count.draft_revision,provider=provider)
        assert ids==[];c.db.commit()
        if first is None:first=count.id
    assert c.db.scalar(select(func.count()).select_from(m.CountCorrection))==0
