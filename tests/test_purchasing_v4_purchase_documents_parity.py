"""Canonical production composition, equivalent legacy PDF, and receiving regressions."""
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.orm import Session
from pdf_test_reader import PdfReader
from test_purchasing_v4_production_submission import approved,sheet,ready,engine,template,pytestmark
from test_purchasing_v4_submission import review
from app.purchasing_v4.production_target import attach
from app.purchasing_v4.production_submission import ProductionSubmissionTarget,authorize
from app.purchasing_v4 import purchase_documents as documents
from app.services.purchase_order_admin_service import _generate_purchase_order_pdf
from app.services import purchase_order_pdf_storage


def test_actual_persisted_v4_po_matches_legacy_without_order_changes(approved,monkeypatch):
    e,state=approved
    attach(e,ProductionSubmissionTarget(e.url.database,"127.0.0.1",1,e.url.username))
    r=review((e,state))
    assert not r["blocking"],r["blocking"]
    with e.begin() as c:
        result=authorize(c,actor_id=1,command_id=str(uuid4()),review_id=r["id"],acknowledged=True,source_namespace="synthetic-fixture-source")
    po=result["children"][0]["purchase_order_id"]
    def records():
        with e.connect() as c:
            return {t:list(c.scalars(text("SELECT row_to_json(x)::text FROM "+t+" x ORDER BY row_to_json(x)::text"))) for t in
                ["purchase_orders","purchase_order_lines","purchase_order_store_allocations","ordering_current_inventory","purchasing_v4.purchase_authorization"]}
    before=records()
    with e.connect() as c:
        v4=documents.issued(c,po,1)
    generated={}
    def capture(content,**kwargs):
        generated["legacy"]=content
        return "isolated-captured-pdf"
    monkeypatch.setattr(purchase_order_pdf_storage,"store_pdf",capture)
    with Session(e) as db:
        assert _generate_purchase_order_pdf(db,purchase_order_id=po)=="isolated-captured-pdf"
    assert records()==before
    new=documents.pdf_bytes(v4)
    legacy_text=" ".join(p.extract_text() for p in PdfReader(BytesIO(generated["legacy"])).pages)
    v4_text=" ".join(p.extract_text() for p in PdfReader(BytesIO(new)).pages)
    assert "Vendor: "+v4["vendor"] in v4_text
    # The V4-approved supplier label may differ from the older public registry name.
    assert "Vendor:" in legacy_text
    for common in [v4["lines"][0]["product"],v4["lines"][0]["variation"],
                   v4["lines"][0]["sku"],documents.money(v4["lines"][0]["unit_cost"],unit=True),
                   documents.money(v4["total_cost"]),"Erupted Vapor LTD","6303 NE HWY 99","6 pk @ 5"]:
        assert common in legacy_text and common in v4_text,common
    assert "Total units: 30" in v4_text
    assert v4["date"] in v4_text
    Path("docs/development/v4-equivalent-legacy.pdf").write_bytes(generated["legacy"])
    Path("docs/development/v4-equivalent-native.pdf").write_bytes(new)
