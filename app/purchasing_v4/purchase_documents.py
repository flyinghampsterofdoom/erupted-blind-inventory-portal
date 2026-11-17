"""Read-only V4 purchase documents; no issuance, delivery or purchasing writes."""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from html import escape
from io import BytesIO
from sqlalchemy import text
from fastapi import HTTPException
from . import controls, worksheets
# Reuse V4's ReportLab engine, layout primitives and repeated table headings.
# Worker-safe vendor projections and issued email artifacts remain unchanged.
from .vendor_documents import (colors, getSampleStyleSheet, ParagraphStyle,
    TA_RIGHT, SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle)

COMPANY = ("Erupted Vapor LTD", "6303 NE HWY 99", "Vancouver, WA 98665",
           "Phone: (503) 730 - 4236", "Email: Admin@EruptedVapor.com")


def amount(value):
    if value is None:
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise controls.InvalidPolicy("Invalid saved purchase order cost")
    return result


def money(value, *, unit=False):
    if value is None:
        return "Unknown"
    if unit:
        raw = format(value, ",f")
        whole, _, fraction = raw.partition(".")
        return "$" + whole + "." + fraction.rstrip("0").ljust(2, "0")
    return "$" + format(value, ",.2f")


def finish(document):
    rows = []
    with localcontext() as context:
        context.prec = 60
        for line in document["lines"]:
            units = line["units"]
            if type(units) is not int or units < 0:
                raise controls.InvalidPolicy("Unresolved saved purchase quantities")
            if units == 0:
                continue
            size = line["pack_size"]
            if type(size) is not int or size < 1 or units % size:
                raise controls.InvalidPolicy("Unresolved saved pack conversion")
            unit = amount(line["unit_cost"])
            extended = amount(line["line_total"]) if line.get("line_total") is not None else (unit * units).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if unit is not None else None
            rows.append(dict(line, unit_cost=unit, extended=extended))
        if not rows:
            raise controls.InvalidPolicy("This vendor has no positive saved purchase quantities")
        return dict(document, lines=rows, total_units=sum(r["units"] for r in rows),
                    total_cost=amount(document["financial"]["total"]) if document.get("financial") else sum((r["unit_cost"] * r["units"] for r in rows), Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if all(r["unit_cost"] is not None for r in rows) else None)


def disclaimer(c, vendor_id):
    return c.scalar(text("""SELECT legal_disclaimer FROM public.purchase_order_pdf_templates
        WHERE active AND (vendor_id=:vendor OR is_generic)
        ORDER BY CASE WHEN vendor_id=:vendor THEN 0 ELSE 1 END, id LIMIT 1"""),
        {"vendor": vendor_id}) or ""


def draft(c, session_id, supplier_id, owner_id, version, namespace):
    state = worksheets.snapshot(c, controls.uuid(session_id), owner_id)
    if state["session"]["version"] != version:
        raise controls.Conflict("Worksheet changed. Save and reopen Print / PDF to review the current quantities.")
    supplier_id = controls.uuid(supplier_id)
    rows = [r for r in state["rows"] if r["supplier_id"] == supplier_id and not r["removed"] and not r.get("sealed")]
    if not rows:
        raise HTTPException(404, "No editable vendor purchase rows")
    if any(any(p is None for p in r["packs"]) or (any(r["packs"]) and r["pack_size"] is None) for r in rows):
        raise controls.InvalidPolicy("Resolve this vendor's quantities and pack sizes before generating its document")
    offer = rows[0]["offers"][supplier_id]
    vendors = c.execute(text("SELECT DISTINCT legacy_vendor_id FROM purchasing_v4.supplier_reference WHERE supplier_id=:id AND source_namespace=:ns"),
        {"id": supplier_id, "ns": namespace}).scalars().all()
    return finish(dict(number="Draft " + session_id, date=datetime.now(timezone.utc).date().isoformat(),
        vendor=offer["name"], purchaser="\n".join(COMPANY), ship_to="", draft=True,
        currency="USD", note="Draft worksheet revision " + str(version) + ". No purchase order has been issued. Costs are saved expected unit costs.",
        disclaimer=disclaimer(c, vendors[0] if len(vendors) == 1 else None),
        lines=[dict(product=r["name"], variation=r["variation"], sku=r["sku"],
            vendor_sku=r["offers"][supplier_id].get("vendor_sku"), pack_size=r["pack_size"] or 1,
            units=sum(r["packs"]) * (r["pack_size"] or 1), unit_cost=r["offers"][supplier_id].get("expected_unit_cost")) for r in rows]))


def issued(c, purchase_order_id, owner_id):
    row = c.execute(text("""SELECT v.snapshot, p.vendor_id FROM purchasing_v4.authorized_vendor v
        JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id
        JOIN public.purchase_orders p ON p.id=v.purchase_order_id
        WHERE v.purchase_order_id=:po AND v.kind='PO' AND a.owner_id=:owner"""),
        {"po": purchase_order_id, "owner": owner_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Unknown owner-authorized V4 purchase order")
    saved = row["snapshot"]
    return finish(dict(number="PO #" + str(purchase_order_id), date=saved["authorized_at"][:10],
        vendor=saved["supplier"]["name"], purchaser="\n".join(COMPANY) + "\nPurchaser: " + saved["header"]["purchaser"], ship_to=saved["header"]["ship_to"],
        draft=False, currency=saved["currency"], note="Saved authorized quantities. Unit costs are approved committed unit valuations; invoice and payment remain separate.",
        disclaimer=disclaimer(c, row["vendor_id"]), financial=(saved.get("quotation") or {}).get("financial"), lines=[dict(product=r["name"], variation=r["variation"],
            sku=r["sku"], vendor_sku=r.get("vendor_sku"), pack_size=r["pack_size"], units=r["total_units"],
            unit_cost=r["valuation"]["unit_value"], line_total=r.get("line_total")) for r in saved["lines"]]))


def table_rows(document):
    rows = [["SKU / Vendor SKU", "Product / Variation", "Quantity", "Unit cost", "Extended cost"]]
    for r in document["lines"]:
        quantity = str(r["units"]) + " units"
        if r["pack_size"] > 1:
            quantity += "\n" + str(r["units"] // r["pack_size"]) + " pk @ " + str(r["pack_size"])
        rows.append(["\n".join(v for v in (r["sku"] or "Not recorded", r["vendor_sku"]) if v),
            r["product"] + ("\n" + r["variation"] if r["variation"] else ""), quantity,
            money(r["unit_cost"], unit=True), money(r["extended"])])
    return rows


def total_rows(document):
    rows = [("Total units", str(document["total_units"]))]
    financial = document.get("financial")
    if financial:
        rows.extend((label, money(amount(financial[key]))) for key, label in
            (("merchandise", "Merchandise"), ("freight", "Freight"), ("tax", "Tax"), ("discount", "Discount")))
    elif document["total_cost"] is not None:
        delta = document["total_cost"] - sum((r["extended"] for r in document["lines"]), Decimal(0))
        if delta:
            rows.append(("Aggregate rounding adjustment", money(delta)))
    rows.append(("Total purchase order cost", money(document["total_cost"]) + " " + document["currency"]))
    return rows


def pdf_bytes(document):
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("POCell", fontName="Helvetica", fontSize=8, leading=10, splitLongWords=True))
    styles.add(ParagraphStyle("PONumber", parent=styles["POCell"], alignment=TA_RIGHT))
    def para(value, style="POCell"):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])
    flow = [para(document["purchaser"]), Spacer(1, 12), Paragraph("DRAFT PURCHASE ORDER" if document["draft"] else "PURCHASE ORDER", styles["Heading2"]),
        para(document["number"]), para("Vendor: " + document["vendor"]), para("Date: " + document["date"]),
        para("Currency: " + document["currency"]), Spacer(1, 8)]
    if document["ship_to"]:
        flow.extend([para("Ship to: " + document["ship_to"]), Spacer(1, 8)])
    flow.extend([para(document["note"]), Spacer(1, 12)])
    cells = [[para(cell, "PONumber" if i > 1 else "POCell") for i, cell in enumerate(row)] for row in table_rows(document)]
    table = Table(cells, colWidths=[90, 182, 86, 76, 94], repeatRows=1, hAlign="LEFT", splitInRow=1)
    table.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,0), colors.HexColor("#e7edf2")), ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f0f6ff")]),
        ("VALIGN", (0,0), (-1,-1), "TOP"), ("LEFTPADDING", (0,0), (-1,-1), 6), ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 7), ("BOTTOMPADDING", (0,0), (-1,-1), 7), ("LINEBELOW", (0,0), (-1,-1), .4, colors.HexColor("#dfe5ed"))]))
    flow.extend([table, Spacer(1, 14)])
    flow.extend(para(label + ": " + value) for label, value in total_rows(document))
    flow.extend([Spacer(1, 10), para(document["disclaimer"])])
    output = BytesIO()
    def page(canvas, doc):
        canvas.setFont("Helvetica", 7)
        canvas.drawString(42, 24, document["number"])
        canvas.drawRightString(570, 24, "Page " + str(doc.page))
    SimpleDocTemplate(output, pagesize=(612,792), leftMargin=42, rightMargin=42, topMargin=40, bottomMargin=48,
        title=document["number"], author="Erupted Vapor LTD", invariant=1).build(flow, onFirstPage=page, onLaterPages=page)
    return output.getvalue()


def html_preview(document, pdf_url):
    def e(value):
        return escape(str(value), quote=True)
    def lines(value):
        return e(value).replace("\n", "<br>")
    rows = table_rows(document)
    head = "".join("<th>" + e(v) + "</th>" for v in rows[0])
    body = "".join("<tr>" + "".join("<td>" + lines(v) + "</td>" for v in row) + "</tr>" for row in rows[1:])
    totals = "<br>".join(e(label + ": " + value) for label, value in total_rows(document))
    download_url = pdf_url + ("&" if "?" in pdf_url else "?") + "download=true"
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(document["number"])}</title>
<style>body{{font:14px Arial,sans-serif;color:#172033;background:#f3f5f8;margin:0}}nav,main{{max-width:960px;margin:20px auto;padding:24px;background:white}}nav{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}button,a{{font:inherit}}button{{cursor:pointer;padding:10px 18px}}table{{width:100%;table-layout:fixed;border-collapse:collapse;font-size:12px}}th,td{{text-align:left;padding:8px;vertical-align:top;border-bottom:1px solid #ddd;overflow-wrap:anywhere}}th{{background:#e7edf2}}th:first-child{{width:16%}}th:nth-child(2){{width:34%}}td:nth-child(n+3),th:nth-child(n+3){{text-align:right}}thead{{display:table-header-group}}tr{{break-inside:avoid;page-break-inside:avoid}}.totals{{break-inside:avoid}}p{{overflow-wrap:anywhere}}@page{{size:letter;margin:14mm}}@media print{{body{{background:white}}nav{{display:none}}main{{margin:0;padding:0;max-width:none}}h1{{font-size:20px}}table,thead,tbody{{display:block}}tr{{display:grid;grid-template-columns:18% 34% 16% 14% 18%;break-inside:avoid;page-break-inside:avoid}}th,td{{display:block;width:auto!important;box-sizing:border-box}}}}</style>
<nav aria-label="Purchase order document actions"><button id="print">Print</button><a href="{e(download_url)}">Download PDF</a><a href="{e(pdf_url)}" target="_blank" rel="noopener">Preview PDF</a><span>Printing does not submit or change this order.</span></nav>
<main><p>{lines(document["purchaser"])}</p><h1>{"Draft purchase order" if document["draft"] else "Purchase order"}</h1><p>{e(document["number"])}<br>Vendor: {e(document["vendor"])}<br>Date: {e(document["date"])}<br>Currency: {e(document["currency"])}</p><p>{lines(document["ship_to"])}</p><p>{e(document["note"])}</p>
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table><div class="totals"><p>{totals}</p></div><p>{lines(document["disclaimer"])}</p></main>
<script>document.getElementById("print").addEventListener("click",()=>window.print());</script></html>"""


def register(app, engine, actor, namespace):
    from fastapi import Depends
    from fastapi.responses import Response, HTMLResponse
    from .isolation import readonly_snapshot
    def response(document, pdf, url, download):
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        if pdf:
            name = ("draft-purchase-order" if document["draft"] else "purchase-order-" + document["number"].replace("PO #", "")) + ".pdf"
            headers["Content-Disposition"] = ("attachment" if download else "inline") + '; filename="' + name + '"'
            return Response(pdf_bytes(document), media_type="application/pdf", headers=headers)
        return HTMLResponse(html_preview(document, url), headers=headers)

    @app.get("/api/worksheets/{session_id}/purchase-documents/{supplier_id}/{format}")
    def worksheet_document(session_id: str, supplier_id: str, format: str, version: int, download: bool=False, owner_id=Depends(actor)):
        if format not in ("preview", "pdf"):
            raise HTTPException(404, "Unknown document format")
        with readonly_snapshot(engine) as c:
            document = draft(c, session_id, supplier_id, owner_id, version, namespace)
        url = "/api/worksheets/" + session_id + "/purchase-documents/" + supplier_id + "/pdf?version=" + str(version)
        return response(document, format == "pdf", url, download)

    @app.get("/api/purchasing-v4/orders/{purchase_order_id}/documents/{format}")
    def order_document(purchase_order_id: int, format: str, download: bool=False, owner_id=Depends(actor)):
        if format not in ("preview", "pdf"):
            raise HTTPException(404, "Unknown document format")
        with readonly_snapshot(engine) as c:
            document = issued(c, purchase_order_id, owner_id)
        return response(document, format == "pdf", "/api/purchasing-v4/orders/" + str(purchase_order_id) + "/documents/pdf", download)
