"""Layout/content checks for the native V4 owner document, on the existing PDF engine."""
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from pathlib import Path
import os
import pytest
from pdf_test_reader import PdfReader
from app.purchasing_v4 import purchase_documents as documents
from app.purchasing_v4.controls import InvalidPolicy


def example(count=1, vendor="Atlas Supply"):
    return dict(number="PO #901", date="2026-11-16", vendor=vendor,
        purchaser="\n".join(documents.COMPANY), ship_to="6303 NE HWY 99", currency="USD",
        draft=False, note="Saved authorized quantities.", disclaimer="Vendor terms: retain pack labels.",
        lines=[dict(product="Product " + str(i) + " with a long readable label " * 5,
            variation="Variation " + str(i), sku="SKU-" + str(i), vendor_sku="V-" + str(i),
            units=15, pack_size=5, unit_cost="0.1234") for i in range(count)])


def test_vendor_pdf_preserves_quantities_and_totals_without_line_costs():
    data=example()
    data["lines"].append(dict(data["lines"][0],product="ZERO-ROW-EXCLUDED",units=0))
    before=deepcopy(data);doc=documents.finish(data)
    assert doc["total_units"]==15 and doc["total_cost"]==Decimal("1.85")
    assert data==before and len(doc["lines"])==1
    content=documents.pdf_bytes(doc)
    text="\n".join(page.extract_text() for page in PdfReader(BytesIO(content)).pages)
    for required in ["Atlas Supply","PO #901","2026-11-16","Product 0","Variation 0","SKU-0","V-0",
                     "15 units","3 pk @ 5","$1.85","Total units: 15","Vendor terms"]:
        assert required in text
    html=documents.html_preview(doc,"/pdf")
    for rendered in (text, html):
        assert "Unit cost" not in rendered and "Extended cost" not in rendered
        assert "$0.1234" not in rendered and "ZERO-ROW-EXCLUDED" not in rendered
    assert documents.table_rows(doc)[0]==["SKU / Vendor SKU","Product / Variation","Quantity"]
    assert all(len(row)==3 for row in documents.table_rows(doc))
    assert doc["lines"][0]["unit_cost"]==Decimal("0.1234")
    assert doc["lines"][0]["extended"]==Decimal("1.85")
    assert content==documents.pdf_bytes(doc)


@pytest.mark.parametrize("vendor",["Atlas Supply","Boreal Imports"])
def test_multipage_all_rows_readable_headers_repeat_and_totals_once(vendor):
    doc=documents.finish(example(85,vendor))
    reader=PdfReader(BytesIO(documents.pdf_bytes(doc)))
    assert len(reader.pages)>=5
    texts=[p.extract_text() for p in reader.pages]
    whole="\n".join(texts)
    for i in range(85):
        assert "SKU-"+str(i) in whole and "Variation "+str(i) in whole
    for i,page in enumerate(texts,1):
        assert "Page "+str(i) in page
        if "SKU-" in page:assert "Product / Variation" in page and "Quantity" in page
    assert "Unit cost" not in whole and "Extended cost" not in whole
    assert whole.count("Total purchase order cost:")==1
    assert "Total units: 1275" in whole and "$157.34" in whole
    # Keep only small review outputs in the authorized retained docs directory.
    output=Path("docs/development/v4-pdf-"+vendor.split()[0].lower()+".pdf")
    output.write_bytes(documents.pdf_bytes(doc))
    output.with_suffix(".html").write_text(documents.html_preview(doc,"/api/purchasing-v4/orders/901/documents/pdf"))


def test_unknown_cost_is_not_zero_and_all_zero_or_invalid_quantities_fail():
    data=example();data["lines"][0]["unit_cost"]=None
    doc=documents.finish(data)
    assert doc["total_cost"] is None
    assert "Total purchase order cost: Unknown" in documents.html_preview(doc,"/pdf")
    data["lines"][0]["units"]=0
    with pytest.raises(InvalidPolicy):documents.finish(data)
    data["lines"][0]["units"]=16
    with pytest.raises(InvalidPolicy):documents.finish(data)


def test_extreme_labels_escape_markup_and_do_not_overflow_pdf():
    data=example();data["lines"][0].update(product="<script>alert(1)</script>"+"X"*1900,variation="Y"*2000)
    doc=documents.finish(data)
    reader=PdfReader(BytesIO(documents.pdf_bytes(doc)))
    assert len(reader.pages)>=2
    html=documents.html_preview(doc,"/pdf?version=1")
    assert "&lt;script&gt;" in html and 'href="/pdf?version=1&amp;download=true"' in html
    assert "window.print()" in html


def test_preserves_v4_aggregate_half_up_rounding_and_quote_financial_snapshot():
    data=example(2)
    for row in data["lines"]:row.update(units=1,pack_size=1,unit_cost="0.005")
    doc=documents.finish(data)
    assert doc["total_cost"]==Decimal("0.01")
    assert ("Aggregate rounding adjustment","$-0.01") in documents.total_rows(doc)
    data=example()
    data["lines"][0].update(unit_cost="0.3333",line_total="5.00")
    data["financial"]=dict(merchandise="5.00",freight="1.00",tax="0.50",discount="0.25",total="6.25")
    doc=documents.finish(data)
    assert doc["lines"][0]["extended"]==Decimal("5.00") and doc["total_cost"]==Decimal("6.25")
    content=documents.pdf_bytes(doc)
    whole=" ".join(p.extract_text() for p in PdfReader(BytesIO(content)).pages)
    assert "Freight: $1.00" in whole and "Tax: $0.50" in whole and "Discount: $0.25" in whole
