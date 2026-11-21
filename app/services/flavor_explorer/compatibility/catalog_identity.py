"""Identity-only Square catalog reader; no preview server, credentials or persistence."""
from __future__ import annotations
import time
from datetime import datetime,timezone

def _stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value,timezone.utc).isoformat().replace("+00:00","Z")

def _stores(rows):
    if not isinstance(rows,list) or len(rows)!=4:raise ValueError("Four verified stores are required")
    result=[]
    for row in rows:
        sid=row.get("id");location=row.get("squareLocationId")
        if not isinstance(sid,(str,int)) or isinstance(sid,bool) or not isinstance(location,str) or not location:
            raise ValueError("Verified store identity is required")
        if row.get("verified") is False:raise ValueError("Store is not verified")
        result.append({"id":sid,"name":str(row["name"]),"squareLocationId":location})
    if len({r["id"] for r in result})!=4 or len({r["squareLocationId"] for r in result})!=4:
        raise ValueError("Store mappings are ambiguous")
    return result

def capture_identity(post,stores,*,clock=time.time,cancelled=lambda:False):
    """Read Square identity only; no costs, prices or inventory are projected."""
    from app.services.square_request_policy import enforce_square_request_policy
    rows=[];cursor=None;seen=set()
    for _ in range(256):
        if cancelled():raise InterruptedError("Observer stopped")
        payload={"limit":100}
        if cursor:payload["cursor"]=cursor
        enforce_square_request_policy("POST","/v2/catalog/search-catalog-items",payload)
        result=post("/v2/catalog/search-catalog-items",payload)
        if not isinstance(result,dict) or result.get("errors") or not isinstance(result.get("items"),list):
            raise ValueError("Catalog identity response was incomplete")
        for item in result["items"]:
            data=item.get("item_data") or {}
            if item.get("is_deleted") or data.get("is_archived"):continue
            for variant in data.get("variations") or []:
                fields=variant.get("item_variation_data") or {}
                if variant.get("is_deleted") or fields.get("sellable") is False:continue
                if item.get("id") and variant.get("id"):
                    rows.append({"id":str(variant["id"]),"itemId":str(item["id"]),"sku":str(fields.get("sku") or ""),
                        "upc":str(fields.get("upc") or ""),"itemName":str(data.get("name") or ""),"variationName":str(fields.get("name") or "")})
        cursor=result.get("cursor")
        if not cursor:break
        if not isinstance(cursor,str) or cursor in seen:raise ValueError("Repeated catalog cursor")
        seen.add(cursor)
    else:raise ValueError("Catalog exceeded bounded pages")
    if not rows or len(rows)>15000:raise ValueError("Catalog identity was empty or exceeded bounds")
    return {"observedAt":_stamp(clock()),"stores":_stores(stores),"variations":rows}
