"""Owner input remains separate from supplier changes and survives live recommendation refresh."""
from dataclasses import replace
from sqlalchemy.orm import Session
from sqlalchemy import select
from test_purchasing_phase2 import engine,receiving_db,database_template,Gateway
from app.config import settings
from app.models import FundingAccount,PurchasingSessionLine,PurchasingSessionGroup
from app.services import purchasing_attention_service as attention,purchasing_session_service as purchasing,purchasing_classification_service as classification


def test_confirmed_financing_survives_generation_quantity_override_and_refresh(engine,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_big_square_vendor_id','V1');monkeypatch.setattr(settings,'purchasing_streamline_square_vendor_id','V2')
    g=Gateway();g.catalog['VAR1']=replace(g.catalog['VAR1'],vendor_ids=('V1','V2'))
    with Session(engine) as db:
        db.add(FundingAccount(id=11,account_type='CONSIGNMENT',vendor_id=1,display_name='Big',created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        from datetime import datetime
        from app.services.purchasing_evidence_service import BUSINESS_TIMEZONE
        classification.save_classification(db,actor_id=1,variation_id='VAR1',category='BIG',scope='FORWARD',
            start_date=str(datetime.now(BUSINESS_TIMEZONE).date()),reason='Owner chose Big',version=0,request_key='confirmed-big');db.commit()
        session=purchasing.generate(db,actor_id=1,request_key='new',gateway=g);db.commit()
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,PurchasingSessionLine.variation_id=='VAR1'))
        # Financing classification does not choose the purchasing supplier.
        assert row.supplier_id is None and row.decisions['intended_account_id']==11
        purchasing.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=40,supplier_id=1);db.commit()
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='new-evidence',gateway=g);db.commit()
        db.refresh(row);assert row.quantity==40 and row.decisions['intended_account_id']==11
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id,PurchasingSessionGroup.supplier_id==1))
        # Moving the product retains its financing but never silently confirms
        # the destination purchasing group's account.
        assert group.intended_account_id is None
        purchasing.set_account(db,session_id=session.id,supplier_id=1,actor_id=1,account_id=11);db.commit()
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='confirmed-group',gateway=g);db.commit()
        db.refresh(group);db.refresh(row)
        assert group.intended_account_id==11 and row.decisions['intended_account_id']==11
