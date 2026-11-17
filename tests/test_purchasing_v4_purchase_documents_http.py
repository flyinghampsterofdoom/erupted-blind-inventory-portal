"""Real persisted V4 worksheets/authorized POs on supervised disposable PostgreSQL."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text
from pdf_test_reader import PdfReader
from test_purchasing_v4_submission_http import api
from test_purchasing_v4_submission import purchasing,sheet,ready,engine,template,pytestmark,review
from test_purchasing_v4_worksheets import edit,save
from app.purchasing_v4 import purchase_documents as documents


def state_fingerprint(e):
    tables=["public.purchase_orders","public.purchase_order_lines","public.purchase_order_store_allocations",
        "public.ordering_current_inventory","purchasing_v4.worksheet","purchasing_v4.worksheet_row",
        "purchasing_v4.purchase_authorization","purchasing_v4.vendor_artifact","purchasing_v4.integration_state"]
    with e.connect() as c:
        return {table:list(c.scalars(text("SELECT row_to_json(x)::text FROM "+table+" x ORDER BY row_to_json(x)::text"))) for table in tables}


def test_saved_manual_edits_and_stale_version_auth_and_no_writes(api):
    client,(e,state)=api
    save(e,state,edit(state,9));sid=state["session"]["id"];supplier=state["rows"][0]["supplier_id"]
    base="/api/worksheets/"+sid+"/purchase-documents/"+supplier+"/"
    before=state_fingerprint(e)
    assert client.get(base+"pdf?version=0").status_code==409
    preview=client.get(base+"preview?version=1")
    assert preview.status_code==200,preview.text
    assert "180 units" in preview.text and "Draft purchase order" in preview.text
    assert "Download PDF" in preview.text and "Preview PDF" in preview.text
    pdf=client.get(base+"pdf?version=1");assert pdf.status_code==200,pdf.text[:100]
    assert pdf.headers["content-disposition"].startswith("inline;")
    assert client.get(base+"pdf?version=1&download=true").headers["content-disposition"].startswith("attachment;")
    assert "Total units: 180" in PdfReader(BytesIO(pdf.content)).pages[-1].extract_text()
    assert "no-store" in pdf.headers["cache-control"]
    assert client.get(base+"unknown?version=1").status_code==404
    assert client.get(base+"pdf").status_code==422
    assert state_fingerprint(e)==before
    client.cookies.delete("v4_isolated_session")
    assert client.get(base+"pdf?version=1").status_code==401


def test_authorized_po_uses_exact_snapshot_and_works_without_legacy_pdf(api):
    client,(e,state)=api
    r=review((e,state))
    result=client.post("/api/purchase-authorizations",json=dict(command_id=str(uuid4()),review_id=r["id"],acknowledged=True),
        headers={"X-CSRF-Token":client.cookies["csrf_token"]})
    assert result.status_code==200,result.text
    po=result.json()["children"][0]["purchase_order_id"]
    url="/api/purchasing-v4/orders/"+str(po)+"/documents/"
    with e.begin() as c:
        # Test vendor-specific disclaimer precedence; templates are only read.
        c.execute(text("INSERT INTO purchase_order_pdf_templates(vendor_id,is_generic,name,legal_disclaimer,active) VALUES (NULL,true,'Generic fixture','Generic terms',true),(1,false,'Vendor fixture','Vendor-specific pack terms',true)"))
    before=state_fingerprint(e)
    html=client.get(url+"preview");assert html.status_code==200,html.text
    assert "Vendor-specific pack terms" in html.text and "Generic terms" not in html.text
    pdf=client.get(url+"pdf?download=true")
    assert pdf.status_code==200,pdf.text[:100]
    text_value="\n".join(p.extract_text() for p in PdfReader(BytesIO(pdf.content)).pages)
    assert "PO #"+str(po) in text_value and "Total units: 30" in text_value
    assert "6 pk @ 5" in text_value and "Vendor-specific pack terms" in text_value
    assert state_fingerprint(e)==before
    with e.connect() as c:
        assert c.scalar(text("SELECT pdf_path FROM purchase_orders WHERE id=:id"),{"id":po}) is None
        document=documents.issued(c,po,1)
        lines=list(c.execute(text("SELECT ordered_qty,unit_cost,item_name,variation_name FROM purchase_order_lines WHERE purchase_order_id=:id"),{"id":po}).mappings())
        assert document["total_units"]==sum(line["ordered_qty"] for line in lines)
        assert document["lines"][0]["unit_cost"]==lines[0]["unit_cost"]
    Path("docs/development/v4-pdf-persisted-po.pdf").write_bytes(pdf.content)
    assert client.get("/api/purchasing-v4/orders/129/documents/pdf").status_code==404
    assert client.get("/api/purchasing-v4/orders/999999/documents/pdf").status_code==404
