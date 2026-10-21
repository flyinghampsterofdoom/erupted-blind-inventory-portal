"""Owner input remains separate from supplier changes and survives live recommendation refresh."""
from dataclasses import replace
from sqlalchemy.orm import Session
from sqlalchemy import select
from test_purchasing_phase2 import engine,receiving_db,database_template,Gateway
from app.config import settings
from app.models import FundingAccount,PurchasingSessionLine,PurchasingSessionGroup
from app.services import purchasing_attention_service as attention,purchasing_session_service as purchasing


def test_confirmed_financing_survives_generation_quantity_override_and_refresh(engine,monkeypatch):
    monkeypatch.setattr(settings,'purchasing_big_square_vendor_id','V1');monkeypatch.setattr(settings,'purchasing_streamline_square_vendor_id','V2')
    g=Gateway();g.catalog['VAR1']=replace(g.catalog['VAR1'],vendor_ids=('V1','V2'))
    with Session(engine) as db:
        db.add(FundingAccount(id=11,account_type='CONSIGNMENT',vendor_id=1,display_name='Big',created_by_principal_id=1,updated_by_principal_id=1));db.flush()
        r=next(r for r in attention.workspace(db,actor_id=1,gateway=g)['rows'] if r['kind']=='FINANCING')
        attention.save(db,actor_id=1,request_key='confirmed-big',changes=[{'kind':r['kind'],'key':r['key'],'version':r['version'],'token':r['token'],'account_id':11}],gateway=g);db.commit()
        session=purchasing.generate(db,actor_id=1,request_key='new',gateway=g);db.commit()
        row=db.scalar(select(PurchasingSessionLine).where(PurchasingSessionLine.session_id==session.id,PurchasingSessionLine.variation_id=='VAR1'))
        assert row.supplier_id==1 and row.decisions['intended_account_id']==11
        purchasing.update_line(db,session_id=session.id,line_id=row.id,actor_id=1,quantity=40,supplier_id=1);db.commit()
        purchasing.refresh(db,session_id=session.id,actor_id=1,request_key='new-evidence',gateway=g);db.commit()
        db.refresh(row);assert row.quantity==40 and row.decisions['intended_account_id']==11
        assert not any(r['kind']=='FINANCING' for r in attention.workspace(db,actor_id=1,gateway=g)['rows'])
        group=db.scalar(select(PurchasingSessionGroup).where(PurchasingSessionGroup.session_id==session.id,PurchasingSessionGroup.supplier_id==1))
        assert group.intended_account_id==11
