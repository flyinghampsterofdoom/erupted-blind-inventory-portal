from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from app.services import read_scope
from app.services.v2_funding_reports_service import account_summary,report_position
from test_v2_funding_reports import db,_map,_sale,_report


def normalized(value):
    if isinstance(value,Decimal):return value  # Decimal equality, never float tolerance.
    if isinstance(value,dict):return {k:normalized(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [normalized(v) for v in value]
    if hasattr(value,'__table__'):return {c.key:getattr(value,c.key) for c in value.__table__.columns}
    return value


def test_exact_financial_summary_and_position_equivalence(db,monkeypatch):
    _map(db);_sale(db);report=_report(db)
    with monkeypatch.context() as patch:
        patch.setattr(read_scope,'reuse_reads',lambda db:nullcontext())
        original=normalized(account_summary(db,account_id=1))
        original_position=normalized(report_position(db,report_id=report.id))
    assert normalized(account_summary(db,account_id=1))==original
    assert normalized(report_position(db,report_id=report.id))==original_position
