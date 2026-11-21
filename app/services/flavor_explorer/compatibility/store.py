"""Compatibility identity, evidence graph and transactional staff review.

Catalog identity is read-only input. Inventory is an injected on-demand reader;
neither search nor this database stores quantities or changes Square records.
"""
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlencode
import hashlib, json, re, uuid
from sqlalchemy import select, insert, update, or_, func
from sqlalchemy.orm import Session
from app.services.access_control_service import principal_has_permission
from .schema import metadata_for
from .identifiers import (identifier, identity_fields, gtin_key, GTIN_KINDS,
                          identifier_intent, words, resistance, query_resistance)

ROLES = {"ADMIN", "MANAGER", "LEAD", "STORE"}
REVIEWERS = {"ADMIN", "MANAGER", "LEAD"}
PRODUCT_KINDS = {"device", "pod", "coil", "tank", "family"}
SUGGESTION_KINDS = {"addition", "correction", "identifier", "relationship",
                    "manufacturer_alias", "catalog_association"}
RELATIONS = {"accepts_pod", "accepts_coil", "includes_tank"}
def now(): return datetime.now(timezone.utc).isoformat()
def serial(value): return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
def digest(value): return hashlib.sha256(serial(value).encode()).hexdigest()
def text(value, limit=200, required=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c)<32 and c not in "\n\t" for c in value):
        raise ValueError("A text field is invalid or too long.")
    result = value.strip()
    if required and not result: raise ValueError("Complete the required product information.")
    return result
def evidence(values, required=True):
    if not isinstance(values, list) or len(values)>20: raise ValueError("Evidence must be a bounded list.")
    result=[]
    for item in values:
        if not isinstance(item,dict): raise ValueError("Evidence must identify its source.")
        url=text(item.get("url",""),2000,True); parsed=urlsplit(url)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Evidence needs an HTTP or HTTPS documentation URL.")
        result.append({"url":url,"title":text(item.get("title") or parsed.hostname,240,True),
            "publisher":text(item.get("publisher") or parsed.hostname,200),
            "claim":text(item.get("claim") or item.get("claims") or item.get("quote") or "Verified supporting documentation",2000),
            "fetchedAt":text(item.get("fetchedAt",""),80),
            "limits":text(item.get("limits","") if isinstance(item.get("limits",""),str) else serial(item["limits"]),3000)})
    if required and not result: raise ValueError("Add the verification source before publishing.")
    return result

class CompatibilityService:
    def __init__(self,storage,catalog_loader=None,stock_loader=None):
        self.storage=storage; self.engine=storage.engine; self.metadata=metadata_for(storage)
        self.catalog_loader=catalog_loader or (lambda:{"observedAt":None,"variations":[],"stores":[]})
        self.stock_loader=stock_loader

    def table(self,name): return self.metadata.tables["cp_"+name]
    def _actor(self,c,principal,capability="view",review=False):
        row=self.storage._principal(c,principal.id)
        role=getattr(row["role"],"value",row["role"])
        if role not in (REVIEWERS if review else ROLES):
            raise PermissionError("A Lead or administrator is required." if review else "Staff access is required.")
        with Session(bind=c) as db:
            if not principal_has_permission(db,principal=principal,permission_key="compatibility."+capability,fallback_allowed=True):
                raise PermissionError("Your account does not have this compatibility permission.")
        return row

    def _products(self,c):
        manufacturers={r["id"]:dict(r) for r in c.execute(select(self.table("manufacturers"))).mappings()}
        return {r["id"]:self._summary(dict(r),manufacturers[r["manufacturer_id"]])
                for r in c.execute(select(self.table("products")).where(self.table("products").c.active.is_(True))).mappings()}
    def _summary(self,row,manufacturer):
        return {"id":row["id"],"name":row["name"],"manufacturerId":row["manufacturer_id"],
            "manufacturer":manufacturer["name"],"kind":row["kind"],"model":row["model"],
            "family":row["family"],"generation":row["generation"],"resistanceOhm":row["resistance_ohm"],
            "capacityMl":row["capacity_ml"],"configuration":row["configuration"],
            "integratedCoil":row["integrated_coil"],"aliases":row["aliases"],
            "manufacturerAliases":manufacturer["aliases"],"version":row["version"],"evidence":row["evidence"]}
    def _product_values(self,c,body,existing=None):
        if not isinstance(body,dict): raise ValueError("Product information is required.")
        allowed={"name","manufacturerId","manufacturerName","kind","model","family","generation","resistanceOhm",
                 "capacityMl","configuration","integratedCoil","aliases","evidence","identifiers","relationships"}
        if set(body)-allowed: raise ValueError("Unsupported product information.")
        result=dict(existing or {})
        mapping={"name":"name","manufacturerId":"manufacturer_id","kind":"kind","model":"model",
            "family":"family","generation":"generation","resistanceOhm":"resistance_ohm",
            "capacityMl":"capacity_ml","configuration":"configuration","integratedCoil":"integrated_coil","aliases":"aliases"}
        for public,private in mapping.items():
            if public in body: result[private]=deepcopy(body[public])
        manufacturer=result.get("manufacturer_id")
        if not manufacturer and body.get("manufacturerName"):
            name=text(body["manufacturerName"],200,True)
            manufacturer="manufacturer-"+digest(words(name))[:20]
            known=c.execute(select(self.table("manufacturers")).where(self.table("manufacturers").c.id==manufacturer)).first()
            if not known: c.execute(insert(self.table("manufacturers")).values(id=manufacturer,name=name,aliases=[]))
            result["manufacturer_id"]=manufacturer
        if not c.scalar(select(self.table("manufacturers").c.id).where(self.table("manufacturers").c.id==manufacturer)):
            raise ValueError("Choose an existing manufacturer or propose its name.")
        for key in ("name","model","family","generation","configuration"):
            result[key]=text(result.get(key) or "",400,required=key=="name")
        if result.get("kind") not in PRODUCT_KINDS: raise ValueError("Choose Device, Coil, Pod, Tank or Family.")
        result["resistance_ohm"]=resistance(result.get("resistance_ohm"))
        capacity=result.get("capacity_ml")
        if capacity not in (None,""):
            try: number=Decimal(str(capacity))
            except Exception: raise ValueError("Capacity must be a positive decimal.")
            if not number.is_finite() or not 0<number<=100: raise ValueError("Capacity must be a positive decimal.")
            result["capacity_ml"]=format(number.normalize(),"f")
        else: result["capacity_ml"]=None
        if result["kind"] in {"pod","tank"} and (not existing or existing.get("kind")!=result["kind"]) and "integratedCoil" not in body:
            raise ValueError("Specify whether this pod or tank has an integrated or replaceable coil.")
        if type(result.get("integrated_coil",False)) is not bool: raise ValueError("Integrated coil must be a boolean.")
        result["integrated_coil"]=result.get("integrated_coil",False)
        aliases=result.get("aliases",[])
        if not isinstance(aliases,list) or len(aliases)>40: raise ValueError("Aliases must be a bounded list.")
        result["aliases"]=list(dict.fromkeys(text(v,200,True) for v in aliases))
        result["search_text"]=words(" ".join(str(result.get(k,"") or "") for k in
            ("name","model","family","generation","resistance_ohm"))+" "+" ".join(result["aliases"]))
        result["evidence"]=evidence(body.get("evidence",result.get("evidence",[])),required=False)
        return {k:v for k,v in result.items() if k in self.table("products").c.keys()}

    def bootstrap(self,principal):
        with self.engine.connect() as c:
            actor=self._actor(c,principal)
            try:self._actor(c,principal,"review",True);can_review=True
            except PermissionError:can_review=False
            people=self.storage.table("stores")
            store=c.execute(select(people).where(people.c.id==actor["store_id"])).mappings().first() if actor["store_id"] else None
            return {"principal":{"id":actor["id"],"name":actor["username"],"store":store["name"] if store else None},
                "capabilities":{"review":can_review},
                "manufacturers":[{"id":r["id"],"name":r["name"],"aliases":r["aliases"]}
                    for r in c.execute(select(self.table("manufacturers")).order_by(self.table("manufacturers").c.name)).mappings()],
                "counts":{n:c.scalar(select(func.count()).select_from(self.table(n))) for n in ("products","relationships")}}

    def _catalog(self):
        value=self.catalog_loader()
        if not isinstance(value,dict): raise ValueError("Catalog identity is unavailable.")
        return value

    @staticmethod
    def _identifier_matches(record,variation):
        # SKU and GTIN are distinct namespaces. A numeric SKU never acquires
        # UPC equivalence merely because its digits happen to match.
        kind=record.get("kind");value=record.get("value")
        if kind=="sku":return bool(value and value==variation.get("sku"))
        if kind not in GTIN_KINDS:return False
        upc=variation.get("upc")
        if not upc:return False
        if value==upc:return True
        try:key=record.get("gtin_key") or gtin_key(value);other=gtin_key(upc)
        except ValueError:return False
        return bool(key and key==other)

    def catalog_search(self,principal,query):
        if not isinstance(query,str) or len(query)>512:raise ValueError("Enter a complete SKU, barcode or catalog name of at most 512 characters.")
        with self.engine.connect() as c:self._actor(c,principal)
        catalog=self._catalog();query=query.strip(" \t\r\n")
        if not catalog.get("observedAt"):return {"available":False,"items":[]}
        if not query:return {"available":True,"items":[]}
        barcode_key=gtin_key(query);ranked=[];needle=words(query)
        for variation in catalog.get("variations",[]):
            exact=(query==variation.get("sku") or query==variation.get("upc")
                or (barcode_key and variation.get("upc") and gtin_key(variation["upc"])==barcode_key))
            hay=words(variation.get("itemName","")+" "+variation.get("variationName",""))
            name_match=(all(token in hay for token in needle.split()) or needle.replace(" ","") in hay.replace(" ",""))
            if exact or (not identifier_intent(query) and name_match):
                ranked.append((0 if exact else 1,{key:variation.get(key,"") for key in ("id","itemName","variationName","sku","upc")}))
        ranked.sort(key=lambda row:(row[0],row[1]["itemName"],row[1]["variationName"],row[1]["id"]))
        return {"available":True,"items":[row for _,row in ranked[:60]]}

    def _links(self,c,product_id,catalog):
        variations={v["id"]:v for v in catalog.get("variations",[]) if isinstance(v.get("id"),str)}
        associations=[dict(r) for r in c.execute(select(self.table("catalog_associations")).where(self.table("catalog_associations").c.active.is_(True))).mappings()]
        manual_owners={r["square_variation_id"]:r["product_id"] for r in associations}
        manual=[r for r in associations if r["product_id"]==product_id]
        result={r["square_variation_id"]:{**r,"variation":variations.get(r["square_variation_id"]),"basis":"reviewed_association"} for r in manual}
        ids=self.table("identifiers");products=self.table("products")
        records=[dict(r) for r in c.execute(select(ids).join(products,products.c.id==ids.c.product_id)
            .where(products.c.active.is_(True),ids.c.active.is_(True))).mappings()]
        ambiguous=False
        for record in (r for r in records if r["product_id"]==product_id):
            matches=[v for v in variations.values() if self._identifier_matches(record,v)]
            if len(matches)>1:
                if not all(manual_owners.get(v["id"])==product_id for v in matches):ambiguous=True
                continue
            if not matches:continue
            variation=matches[0];vid=variation["id"];owner=manual_owners.get(vid)
            if owner==product_id:continue
            if owner is not None:
                ambiguous=True;continue
            owners={r["product_id"] for r in records if self._identifier_matches(r,variation)}
            if owners-{product_id}:
                ambiguous=True;continue
            result.setdefault(vid,{"square_variation_id":vid,"package_configuration":record["package_configuration"],
                "verification_note":"Exact verified product identifier","evidence":record["evidence"],
                "variation":variation,"basis":"exact_identifier"})
        # A conflicting automatic candidate cannot revoke an independently
        # reviewed current association. Only the reviewed/safe IDs are returned.
        reviewed_current=any(r["variation"] and r["basis"]=="reviewed_association" for r in result.values())
        return list(result.values()),ambiguous and not reviewed_current

    def _identifiers(self,c,product_id,catalog):
        result=[{"id":r["id"],"kind":r["kind"],"value":r["value"],
                 "packageConfiguration":r["package_configuration"],"evidence":r["evidence"]}
                for r in c.execute(select(self.table("identifiers")).where(self.table("identifiers").c.product_id==product_id,self.table("identifiers").c.active.is_(True))).mappings()]
        links,ambiguous=self._links(c,product_id,catalog)
        allowed={r["square_variation_id"]:r for r in links if r["variation"]}
        variations=catalog.get("variations",[])
        for record in result:
            candidates=[v for v in variations if self._identifier_matches(record,v)]
            safe=[v for v in candidates if v["id"] in allowed]
            if len(candidates)==1 and len(safe)==1:
                record["squareVariationId"]=safe[0]["id"]
                record["catalogVariant"]=safe[0].get("variationName","")
            elif candidates:
                record["catalogVariantAmbiguous"]=True
                record["squareVariationIds"]=[v["id"] for v in safe]
        if not ambiguous:
            for link in links:
                variation=link["variation"]
                if not variation:continue
                for key in ("sku","upc"):
                    value=variation.get(key)
                    if not value:continue
                    result.append({"id":"square:"+variation["id"]+":"+key,"kind":key,"value":value,
                        "squareVariationId":variation["id"],"catalogVariant":variation.get("variationName",""),
                        "packageConfiguration":link["package_configuration"] or variation.get("variationName",""),
                        "evidence":link["evidence"]})
        return result

    def search(self,principal,query,mode="auto"):
        if mode not in {"auto","identifier","name"}: raise ValueError("Choose a valid search mode.")
        if not isinstance(query,str) or len(query)>512: raise ValueError("Search is limited to 512 characters; the scanned value was not truncated.")
        query=query.strip(" \t\r\n")
        with self.engine.connect() as c:
            self._actor(c,principal); products=self._products(c)
            if not query:return {"query":query,"mode":"name","matchType":"name","items":list(products.values())[:30]}
            raw=identifier(query)
            table=self.table("identifiers"); key=gtin_key(raw)
            condition=table.c.exact_key==raw
            if key:condition=or_(condition,table.c.gtin_key==key)
            records=[dict(r) for r in c.execute(select(table).where(condition,table.c.active.is_(True))).mappings()]
            found=[];catalog=self._catalog()
            for record in records:
                if record["product_id"] in products:
                    public=next(row for row in self._identifiers(c,record["product_id"],catalog) if row["id"]==record["id"])
                    # Fully reviewed multiple catalog variants are emitted below
                    # as distinct exact choices, never one package-label result.
                    if len(public.get("squareVariationIds",[]))>1:continue
                    found.append((record["product_id"],public))
            # Exact current Square identities are joined only through reviewed
            # associations or an unambiguous existing component identifier.
            for variation in catalog.get("variations",[]):
                sku,upc=variation.get("sku"),variation.get("upc")
                if not (raw in (sku,upc) or (key and upc and gtin_key(upc)==key)):continue
                associations=[dict(r) for r in c.execute(select(self.table("catalog_associations")).where(
                    self.table("catalog_associations").c.square_variation_id==variation["id"],self.table("catalog_associations").c.active.is_(True))).mappings()]
                if not associations:
                    terms=[v for v in (sku,upc) if v]
                    condition=table.c.exact_key.in_(terms)
                    barcode_key=gtin_key(upc) if upc else None
                    if barcode_key:condition=or_(condition,table.c.gtin_key==barcode_key)
                    for record in c.execute(select(table).where(condition,table.c.active.is_(True))).mappings():
                        links,ambiguous=self._links(c,record["product_id"],catalog)
                        if not ambiguous and any(r["square_variation_id"]==variation["id"] for r in links):
                            associations.append({"product_id":record["product_id"],"package_configuration":record["package_configuration"]})
                for association in associations:
                    product_id=association["product_id"]
                    if product_id in products:
                        kind="sku" if sku==raw else "upc"
                        found.append((product_id,{"id":"square:"+variation["id"]+":"+kind,"kind":kind,
                            "value":sku if kind=="sku" else upc,"squareVariationId":variation["id"],
                            "catalogVariant":variation.get("variationName",""),
                            "packageConfiguration":association["package_configuration"] or variation.get("variationName","")}))
            unique={}
            for product_id,match in found:
                variant=match.get("squareVariationId")
                key=(product_id,"square",variant) if variant else (product_id,"package",match["packageConfiguration"])
                label="Exact "+match["kind"].upper()+" match"
                if match.get("catalogVariant"):label+=" · "+match["catalogVariant"]
                unique.setdefault(key,{**products[product_id],"matchedIdentifier":match,"match":{"kind":"exact_identifier","label":label}})
            if unique:
                rows=list(unique.values());certain=len(rows)==1 and not rows[0]["matchedIdentifier"].get("catalogVariantAmbiguous")
                return {"query":query,"mode":"identifier","matchType":"exact_identifier" if certain else "ambiguous",
                    "items":rows,"exactProductId":rows[0]["id"] if certain else None}
            compact=words(raw).replace(" ","")
            known_name=any(compact==words(value).replace(" ","") for p in products.values() for value in (p["name"],p["model"],*p["aliases"]) if value)
            if mode=="identifier" or (mode=="auto" and identifier_intent(raw) and not known_name):
                return {"query":query,"mode":"identifier","matchType":"none","items":[]}
            needle=words(query); tokens=needle.split(); requested_resistance=query_resistance(query); ranked=[]
            for product in products.values():
                if requested_resistance and product["resistanceOhm"]!=requested_resistance:continue
                names=[product["name"],product["model"],product["family"],*product["aliases"]]
                hay=words(" ".join([product["manufacturer"],*product["manufacturerAliases"],*names,product["resistanceOhm"] or ""]))
                exact=any(needle==words(n) for n in names)
                coverage=sum(t in hay for t in tokens)/max(1,len(tokens))
                ratio=max(SequenceMatcher(None,needle,words(n)).ratio() for n in names if n) if any(names) else 0
                score=(2 if exact else 0)+coverage+ratio*.5
                if "replacement" in tokens and product["kind"] in {"pod","coil"}:score+=.15
                if exact or coverage>=.6 or ratio>=.63:
                    ranked.append((score,{**product,"match":{"kind":"name","label":"Exact model name" if exact else "Approximate name match"}}))
            ranked.sort(key=lambda x:(-x[0],x[1]["name"],x[1]["resistanceOhm"] or ""))
            return {"query":query,"mode":"name","matchType":"name" if ranked else "none","items":[p for _,p in ranked[:60]]}

    def _relationships(self,c,product_id,products):
        edges=[dict(r) for r in c.execute(select(self.table("relationships")).where(self.table("relationships").c.active.is_(True))).mappings()]
        origin=products[product_id]; result={}
        forward=origin["kind"] in {"device","pod","tank"}
        # Only directed manufacturer-supported component paths are composed:
        # device→pod/tank→coil, or their exact reverse. Never sibling devices.
        queue=[(product_id,[],set())]
        while queue:
            current,path,seen=queue.pop(0)
            for edge in edges:
                if edge["id"] in seen:continue
                other=(edge["to_id"] if forward and edge["from_id"]==current else
                       edge["from_id"] if not forward and edge["to_id"]==current else None)
                if not other or other not in products:continue
                chain=path+[edge]; target=products[other]
                basis=[source for e in chain for source in e["evidence"]]
                conditions=" · ".join(dict.fromkeys(e["conditions"] for e in chain if e["conditions"]))
                item={"id":edge["id"],"target":target,"relationship":edge["kind"],"conditions":conditions,
                      "evidence":list({serial(e):e for e in basis}.values()),
                      "direction":"outgoing" if forward else "incoming",
                      "via":[products[e["to_id"]] for e in chain[:-1]] if forward else [products[e["from_id"]] for e in chain[:-1]]}
                result.setdefault((other,conditions),item)
                if len(chain)<3 and ((forward and target["kind"] in {"pod","tank"} and not target["integratedCoil"]) or (not forward and target["kind"] in {"pod","tank"})):
                    queue.append((other,chain,seen|{edge["id"]}))
        # Pods/tanks also need every directly verified accepting device.
        if origin["kind"] in {"pod","tank"}:
            for edge in edges:
                if edge["to_id"]==product_id and edge["from_id"] in products:
                    result.setdefault((edge["from_id"],edge["conditions"]),{"id":edge["id"],
                        "target":products[edge["from_id"]],"relationship":edge["kind"],"conditions":edge["conditions"],
                        "evidence":edge["evidence"],"via":[],"direction":"incoming"})
        return sorted(result.values(),key=lambda r:(r["target"]["kind"],r["target"]["name"],r["target"]["resistanceOhm"] or ""))

    def product(self,principal,product_id,identifier_id=None):
        with self.engine.connect() as c:
            self._actor(c,principal); products=self._products(c)
            if product_id not in products:raise LookupError("That product is unavailable.")
            catalog=self._catalog(); identifiers=self._identifiers(c,product_id,catalog)
            match=next((row for row in identifiers if row["id"]==identifier_id),None) if identifier_id else None
            if identifier_id and not match:raise ValueError("That identifier does not belong to this product.")
            links,ambiguous=self._links(c,product_id,catalog)
            all_links=links
            if match:
                exact_variant=match.get("squareVariationId")
                if exact_variant:
                    links=[link for link in links if link["square_variation_id"]==exact_variant]
                    ambiguous=not bool(links and links[0]["variation"])
                elif match["kind"]=="sku" or match["kind"] in GTIN_KINDS:
                    candidates={v["id"] for v in catalog.get("variations",[]) if self._identifier_matches(match,v)}
                    selected=[link for link in links if link["square_variation_id"] in candidates and link["variation"]]
                    if candidates:
                        ambiguous=len(candidates)!=1 or not selected
                        links=selected
                    else:
                        # A reviewed component association is not proof that a
                        # different scanned package is the same current SKU.
                        ambiguous=ambiguous or any(link["variation"] for link in links)
                        links=[link for link in links if not link["variation"]]
                elif match["packageConfiguration"] and len([r for r in links if r["variation"]])>1:
                    ambiguous=True
            status=("unavailable" if not catalog.get("observedAt") or (links and not any(r["variation"] for r in links)) else "ambiguous" if ambiguous else "verified" if links else "unmatched")
            query=urlencode({"identifierId":identifier_id}) if identifier_id else ""
            result={**products[product_id],"identifiers":identifiers,"matchedIdentifier":match,
                "relationships":self._relationships(c,product_id,products),
                "catalogAssociation":{"verified":status=="verified","status":status,
                    "variationIds":[r["square_variation_id"] for r in links if r["variation"]],
                    "unavailableMatches":[r["square_variation_id"] for r in links if not r["variation"]],
                    "allMatches":[{"variationId":r["square_variation_id"],"associationId":r.get("id"),
                        "label":(r["variation"] or {}).get("variationName") or r["package_configuration"],
                        "packageConfiguration":r["package_configuration"],"available":bool(r["variation"]),"basis":r["basis"]} for r in all_links],
                    "matches":[{"variationId":r["square_variation_id"],"label":(r["variation"] or {}).get("variationName") or r["package_configuration"],
                        "packageConfiguration":r["package_configuration"],"basis":r["basis"]} for r in links if r["variation"]]},
                "customerRequestUrl":"/v2/compatibility-explorer/customer-request/"+product_id+("?"+query if query else "")}
            return result

    def stock(self,principal,product_id,identifier_id=None):
        with self.engine.connect() as c:self._actor(c,principal,"check_stock")
        product=self.product(principal,product_id,identifier_id)
        association=product["catalogAssociation"]
        if not association["verified"]:raise ValueError("A verified catalog association is required before checking stock.")
        if not self.stock_loader:raise ValueError("The current inventory reader is unavailable.")
        result=self.stock_loader(association["variationIds"])
        # Provider work happens outside the write transaction. A correction or
        # permission change during that wait must not publish the old binding.
        from types import SimpleNamespace
        with self.engine.connect() as c:
            actor=self.storage._principal(c,principal.id)
            current_actor=SimpleNamespace(id=actor["id"],role=actor["role"],
                store_id=actor["store_id"],active=actor["active"])
            self._actor(c,current_actor,"check_stock")
        try:latest=self.product(current_actor,product_id,identifier_id)
        except (ValueError,LookupError):
            raise ValueError("The catalog association changed. Refresh the product before checking stock.") from None
        current=latest["catalogAssociation"]
        if (latest["version"]!=product["version"] or not current["verified"]
                or set(current["variationIds"])!=set(association["variationIds"])):
            raise ValueError("The catalog association changed. Refresh the product before checking stock.")
        labels={r["variationId"]:r for r in current["matches"]}
        for store in result.get("stores",[]):
            if store.get("state")=="available":store["state"]="in_stock"
            for row in store.get("variations",[]):
                context=labels.get(row["id"],{})
                row["label"]=context.get("label") or row.get("label") or row["id"]
                row["packageConfiguration"]=context.get("packageConfiguration","")
        return result

    def customer_request_prefill(self,principal,product_id,identifier_id=None):
        p=self.product(principal,product_id,identifier_id)
        label=" · ".join(v for v in (p["manufacturer"],p["name"],p["resistanceOhm"]+"Ω" if p["resistanceOhm"] else "",(p["matchedIdentifier"] or {}).get("packageConfiguration","")) if v)
        ids=[p["matchedIdentifier"]] if p["matchedIdentifier"] else p["identifiers"]
        notes="\n".join(["Compatibility Explorer customer request","Type: "+p["kind"],"Model: "+p["model"],
            "Family: "+p["family"],"Generation: "+p["generation"],
            "Identifiers: "+"; ".join(r["kind"].upper()+": "+r["value"] for r in ids),
            "Compatible products: "+"; ".join(r["target"]["name"] for r in p["relationships"])])
        return {"requested_items_raw":label.replace(","," ").replace("\n"," "),"notes":notes,"product":p}

    def _audit(self,c,event,actor=None,suggestion=None,product=None,before=None,after=None):
        c.execute(insert(self.table("audit")).values(event=event,actor_id=actor,suggestion_id=suggestion,
            product_id=product,before=before,after=after,created_at=now()))

    def suggest(self,principal,body):
        allowed={"kind","productId","targetProductId","baseVersion","proposed","comment","scannedIdentifier"}
        if not isinstance(body,dict) or set(body)-allowed:raise ValueError("Unsupported suggestion fields.")
        kind=body.get("kind")
        if kind not in SUGGESTION_KINDS or not isinstance(body.get("proposed"),dict):raise ValueError("Choose a suggestion type and describe its changes.")
        if len(serial(body["proposed"]))>32000:raise ValueError("The proposed change is too large.")
        comment=text(body.get("comment",""),2000)
        scanned=identifier(body["scannedIdentifier"]) if body.get("scannedIdentifier") else None
        with self.storage._write() as c:
            actor=self._actor(c,principal,"suggest")
            product=None
            if body.get("productId"):
                product=c.execute(select(self.table("products")).where(self.table("products").c.id==body["productId"])).mappings().one_or_none()
                if not product:raise LookupError("Product not found.")
                if body.get("baseVersion")!=product["version"]:raise ValueError("Product information changed. Reload it before submitting.")
            if kind not in {"addition","manufacturer_alias"} and product is None:raise ValueError("Choose the product being corrected.")
            original=dict(product) if product else {}
            target_ids={v for v in [body.get("targetProductId"),body["proposed"].get("targetProductId")] if isinstance(v,str)}
            for link in body["proposed"].get("relationships",[]):
                if isinstance(link,dict) and isinstance(link.get("targetProductId"),str):target_ids.add(link["targetProductId"])
            if target_ids:
                original["_relatedProducts"]=[dict(r) for r in c.execute(select(self.table("products")).where(self.table("products").c.id.in_(target_ids))).mappings()]
                if len(original["_relatedProducts"])!=len(target_ids):raise ValueError("A proposed related product is unavailable.")
            relationship_id=body["proposed"].get("relationshipId")
            if relationship_id:
                relation=c.execute(select(self.table("relationships")).where(self.table("relationships").c.id==relationship_id)).mappings().one_or_none()
                if not relation or body.get("productId") not in {relation["from_id"],relation["to_id"]}:raise ValueError("That relationship does not belong to this product.")
                original["_relationshipBefore"]=dict(relation)
            self._management_original(c,kind,body.get("productId"),body["proposed"],original)
            fingerprint_data={k:body.get(k) for k in ("kind","productId","targetProductId","baseVersion","proposed","scannedIdentifier")}
            management={key:original[key] for key in ("_identifierBefore","_catalogAssociationBefore","_catalogOwnerBefore","_catalogVariationBefore") if key in original}
            if management:fingerprint_data["managementBase"]=management
            fingerprint=digest(fingerprint_data)
            table=self.table("suggestions")
            prior=c.scalar(select(table.c.id).where(table.c.employee_id==principal.id,table.c.fingerprint==fingerprint,table.c.status=="PENDING"))
            if prior:return {"id":prior,"status":"PENDING","duplicate":True}
            identity=c.execute(insert(table).values(employee_id=principal.id,store_id=actor["store_id"],kind=kind,
                product_id=body.get("productId"),target_product_id=body.get("targetProductId"),base_version=body.get("baseVersion"),
                proposed=body["proposed"],original=original,scanned_identifier=scanned,
                comment=comment,fingerprint=fingerprint,status="PENDING",submitted_at=now())).inserted_primary_key[0]
            self._audit(c,"SUBMITTED",principal.id,identity,body.get("productId"),after=body["proposed"])
            return {"id":identity,"status":"PENDING","duplicate":False}

    def suggestions(self,principal,status="PENDING"):
        if status not in {"PENDING","APPROVED","REJECTED","MODIFIED_APPROVED","ALL",""}:raise ValueError("Unknown review status.")
        with self.engine.connect() as c:
            self._actor(c,principal,"review",True)
            people={r["id"]:r["username"] for r in c.execute(select(self.storage.table("principals"))).mappings()}
            stores={r["id"]:r["name"] for r in c.execute(select(self.storage.table("stores"))).mappings()}
            products=self._products(c); table=self.table("suggestions"); statement=select(table).order_by(table.c.id.desc())
            if status not in {"","ALL"}:statement=statement.where(table.c.status==status)
            return {"items":[{"id":r["id"],"kind":r["kind"],"productId":r["product_id"],
                "product":products.get(r["product_id"]),"targetProductId":r["target_product_id"],
                "baseVersion":r["base_version"],"proposed":r["proposed"],"original":r["original"],
                "comment":r["comment"],"scannedIdentifier":r["scanned_identifier"],"status":r["status"],
                "employee":people.get(r["employee_id"],"Existing employee"),"store":stores.get(r["store_id"],"Company-wide"),
                "submittedAt":r["submitted_at"],"reviewer":people.get(r["decided_by"]),"reviewedAt":r["decided_at"],
                "reviewNote":r["decision_reason"],"published":r["published"]} for r in c.execute(statement).mappings()]}

    def _management_original(self,c,kind,product_id,proposed,original):
        for flag in ("remove","reassign"):
            if flag in proposed and type(proposed[flag]) is not bool:raise ValueError("Choose an explicit review action.")
        if kind=="identifier" and (proposed.get("identifierId") or proposed.get("remove")):
            identity=text(proposed.get("identifierId",""),255,True)
            if identity.startswith("square:"):
                raise ValueError("Square identifiers are read-only. Suggest a catalog association correction.")
            table=self.table("identifiers")
            row=c.execute(select(table).where(table.c.id==identity,table.c.product_id==product_id)).mappings().one_or_none()
            if not row:raise ValueError("Choose an identifier belonging to this product.")
            if proposed.get("remove") and not row["active"]:raise ValueError("That identifier is already retired.")
            original["_identifierBefore"]=dict(row)
        if kind=="catalog_association":
            variation=text(proposed.get("squareVariationId",""),255,True);table=self.table("catalog_associations")
            row=c.execute(select(table).where(table.c.square_variation_id==variation)).mappings().one_or_none()
            if proposed.get("remove"):
                if not row or row["product_id"]!=product_id or not row["active"]:
                    raise ValueError("Only this product's active reviewed association may be removed.")
            elif row and proposed.get("reassign") is not True:
                raise ValueError("This variation has a reviewed association. Explicitly propose its correction or reassignment.")
            elif proposed.get("reassign") is True and not row:
                raise ValueError("Choose an existing reviewed association to reassign.")
            identity=next((v for v in self._catalog().get("variations",[]) if v["id"]==variation),None)
            original["_catalogVariationBefore"]=deepcopy(identity)
            original["_catalogAssociationBefore"]=None
            if row:
                owner=c.execute(select(self.table("products")).where(self.table("products").c.id==row["product_id"])).mappings().one()
                original["_catalogAssociationBefore"]={**dict(row),"product":self._products(c).get(row["product_id"])}
                original["_catalogOwnerBefore"]={"id":owner["id"],"version":owner["version"]}

    def _validate_management(self,c,suggestion,proposed):
        kind=suggestion["kind"];original=suggestion["original"]
        reference="identifierId" if kind=="identifier" else "squareVariationId" if kind=="catalog_association" else None
        if reference and proposed.get(reference)!=suggestion["proposed"].get(reference):
            raise ValueError("Submit a fresh suggestion before selecting a different identifier or catalog variation.")
        before=original.get("_identifierBefore")
        if before is not None:
            current=c.execute(select(self.table("identifiers")).where(self.table("identifiers").c.id==before["id"])).mappings().one_or_none()
            if current is None or dict(current)!=before:raise ValueError("The identifier changed; submit a fresh suggestion.")
        if "_catalogAssociationBefore" in original:
            table=self.table("catalog_associations")
            current=c.execute(select(table).where(table.c.square_variation_id==proposed["squareVariationId"])).mappings().one_or_none()
            before=original["_catalogAssociationBefore"]
            expected={k:v for k,v in before.items() if k in table.c.keys()} if before else None
            if (dict(current) if current else None)!=expected:raise ValueError("The reviewed association changed; submit a fresh suggestion.")
            owner=original.get("_catalogOwnerBefore")
            if owner and c.scalar(select(self.table("products").c.version).where(self.table("products").c.id==owner["id"]))!=owner["version"]:
                raise ValueError("The previous catalog owner changed; submit a fresh suggestion.")

        if kind=="catalog_association" and not proposed.get("remove"):
            before=original.get("_catalogVariationBefore")
            current=next((v for v in self._catalog().get("variations",[]) if v["id"]==proposed["squareVariationId"]),None)
            if current!=before or current is None:
                raise ValueError("Catalog product information changed; submit a fresh verified association.")

    def _put_identifier(self,c,product_id,proposed,activate=False):
        fields=identity_fields(proposed.get("value"),proposed.get("kind","sku"))
        config=text(proposed.get("packageConfiguration") or proposed.get("packageKey") or "",400)
        sources=evidence(proposed.get("evidence",[]),required=False)
        identity="identifier-"+digest([product_id,fields["kind"],fields["value"],config])[:32]
        table=self.table("identifiers")
        saved=c.execute(select(table).where(table.c.id==identity)).mappings().one_or_none()
        if not saved:
            c.execute(insert(table).values(id=identity,product_id=product_id,**fields,package_configuration=config,evidence=sources,active=True))
        elif activate:
            c.execute(update(table).where(table.c.id==identity).values(active=True,evidence=sources or saved["evidence"]))
        return identity

    def _put_relationship(self,c,from_id,to_id,kind,conditions,sources,identity=None):
        table=self.table("products")
        ends={r["id"]:r for r in c.execute(select(table).where(table.c.id.in_([from_id,to_id]),table.c.active.is_(True))).mappings()}
        if len(ends)!=2 or from_id==to_id:raise ValueError("Choose two distinct published products.")
        source,target=ends[from_id],ends[to_id]
        valid=((kind=="accepts_pod" and source["kind"]=="device" and target["kind"]=="pod")
            or (kind=="accepts_coil" and source["kind"] in {"device","pod","tank"} and target["kind"]=="coil" and not source["integrated_coil"])
            or (kind=="includes_tank" and source["kind"]=="device" and target["kind"]=="tank"))
        if not valid:raise ValueError("That relationship is not a valid device/component path.")
        sources=evidence(sources);conditions=text(conditions,3000)
        identity=identity or "relationship-"+digest([from_id,to_id,kind,conditions])[:32]
        rel=self.table("relationships")
        existing=c.execute(select(rel).where(rel.c.id==identity)).mappings().one_or_none()
        values=dict(from_id=from_id,to_id=to_id,kind=kind,conditions=conditions,evidence=sources,active=True)
        if existing:
            c.execute(update(rel).where(rel.c.id==identity).values(**values,version=existing["version"]+1))
        else:c.execute(insert(rel).values(id=identity,**values,version=1))
        return identity

    def _publish(self,c,suggestion,proposed):
        if not isinstance(proposed,dict):raise ValueError("Published changes must be an object.")
        kind=suggestion["kind"]; product_id=suggestion["product_id"]; product_table=self.table("products")
        old=c.execute(select(product_table).where(product_table.c.id==product_id)).mappings().one_or_none() if product_id else None
        if old and old["version"]!=suggestion["base_version"]:raise ValueError("The original product changed; review a fresh suggestion.")
        for expected in suggestion["original"].get("_relatedProducts",[]):
            current=c.execute(select(product_table.c.version).where(product_table.c.id==expected["id"])).scalar_one_or_none()
            if current!=expected["version"]:raise ValueError("A related product changed; submit a fresh compatibility suggestion.")
        prior_relation=suggestion["original"].get("_relationshipBefore")
        if prior_relation:
            current=c.scalar(select(self.table("relationships").c.version).where(self.table("relationships").c.id==prior_relation["id"]))
            if current!=prior_relation["version"]:raise ValueError("The compatibility relationship changed; review a fresh suggestion.")
        self._validate_management(c,suggestion,proposed)
        if kind=="addition":
            product_id="product-"+uuid.uuid4().hex
            values=self._product_values(c,proposed);values.update(id=product_id,version=1,active=True)
            c.execute(insert(product_table).values(**values))
            for record in proposed.get("identifiers",[]):self._put_identifier(c,product_id,record)
            for link in proposed.get("relationships",[]):
                target=link.get("targetProductId")
                source,destination=(target,product_id) if link.get("direction")=="incoming" else (product_id,target)
                self._put_relationship(c,source,destination,link.get("relationship"),link.get("conditions",""),link.get("evidence",[]))
            return {"productId":product_id,"product":values}
        if kind=="manufacturer_alias":
            table=self.table("manufacturers");mid=proposed.get("manufacturerId")
            row=c.execute(select(table).where(table.c.id==mid)).mappings().one()
            alias=text(proposed.get("alias",""),200,True);aliases=list(dict.fromkeys([*row["aliases"],alias]))
            c.execute(update(table).where(table.c.id==mid).values(aliases=aliases))
            return {"manufacturerId":mid,"aliases":aliases}
        if not old:raise ValueError("The original product is unavailable.")
        if kind=="correction":
            patch=proposed.get("patch")
            if not isinstance(patch,dict) or not patch:raise ValueError("Choose the product fields to correct.")
            if proposed.get("evidence"):patch={**patch,"evidence":proposed["evidence"]}
            values=self._product_values(c,patch,dict(old));values["version"]=old["version"]+1
            physical=("manufacturer_id","kind","model","family","generation","resistance_ohm","capacity_ml","integrated_coil")
            if any(values.get(key)!=old[key] for key in physical):
                rel=self.table("relationships");ids=self.table("identifiers");associations=self.table("catalog_associations")
                linked=(c.scalar(select(rel.c.id).where(rel.c.active.is_(True),
                    or_(rel.c.from_id==product_id,rel.c.to_id==product_id)).limit(1))
                    or c.scalar(select(ids.c.id).where(ids.c.product_id==product_id,ids.c.active.is_(True)).limit(1))
                    or c.scalar(select(associations.c.id).where(associations.c.product_id==product_id,associations.c.active.is_(True)).limit(1)))
                if linked:
                    raise ValueError("Create the distinct variant or review/remove existing relationships and identifier associations before changing physical identity.")
            c.execute(update(product_table).where(product_table.c.id==product_id).values(**{k:v for k,v in values.items() if k!="id"}))
            return {"productId":product_id,"product":values}
        if kind=="identifier":
            table=self.table("identifiers");before=suggestion["original"].get("_identifierBefore")
            if before:
                sources=evidence(proposed.get("evidence",[]))
                c.execute(update(table).where(table.c.id==before["id"]).values(active=False))
                if proposed.get("remove") is True:
                    after={**before,"active":False}
                    published={"productId":product_id,"removedIdentifierId":before["id"],"before":before,"after":after}
                else:
                    record={"kind":proposed.get("kind",before["kind"]),"value":proposed.get("value",before["value"]),
                        "packageConfiguration":proposed.get("packageConfiguration",before["package_configuration"]),"evidence":sources}
                    identity=self._put_identifier(c,product_id,record,activate=True)
                    after=dict(c.execute(select(table).where(table.c.id==identity)).mappings().one())
                    published={"productId":product_id,"identifierId":identity,"before":before,"after":after}
            else:
                if proposed.get("identifierId") or proposed.get("remove"):raise ValueError("Choose the retained identifier being corrected.")
                identity=self._put_identifier(c,product_id,proposed,activate=True)
                after=dict(c.execute(select(table).where(table.c.id==identity)).mappings().one())
                published={"productId":product_id,"identifierId":identity,"after":after}
        elif kind=="relationship":
            sources=evidence(proposed.get("evidence",[]))
            relation_id=proposed.get("relationshipId")
            if relation_id:
                rel=self.table("relationships");current=c.execute(select(rel).where(rel.c.id==relation_id)).mappings().one()
                if product_id not in {current["from_id"],current["to_id"]}:raise ValueError("That relationship belongs to another product.")
                if proposed.get("remove") is True or proposed.get("action")=="remove":
                    c.execute(update(rel).where(rel.c.id==relation_id).values(active=False,version=current["version"]+1))
                    published={"productId":product_id,"removedRelationshipId":relation_id}
                else:
                    target=proposed.get("targetProductId") or suggestion["target_product_id"]
                    source,destination=(target,product_id) if proposed.get("direction")=="incoming" else (product_id,target)
                    identity=self._put_relationship(c,source,destination,proposed.get("relationship"),proposed.get("conditions",""),sources,relation_id)
                    published={"productId":product_id,"relationshipId":identity}
            else:
                target=proposed.get("targetProductId") or suggestion["target_product_id"]
                source,destination=(target,product_id) if proposed.get("direction")=="incoming" else (product_id,target)
                identity=self._put_relationship(c,source,destination,proposed.get("relationship"),proposed.get("conditions",""),sources)
                published={"productId":product_id,"relationshipId":identity}
        elif kind=="catalog_association":
            variation_id=text(proposed.get("squareVariationId",""),255,True);table=self.table("catalog_associations")
            row=c.execute(select(table).where(table.c.square_variation_id==variation_id)).mappings().one_or_none()
            before=dict(row) if row else None;sources=evidence(proposed.get("evidence",[]))
            if proposed.get("remove") is True:
                if not row or row["product_id"]!=product_id or not row["active"]:
                    raise ValueError("Only this product's active reviewed association may be removed.")
                c.execute(update(table).where(table.c.id==row["id"]).values(active=False))
                identity=row["id"]
            else:
                variation=next((v for v in self._catalog().get("variations",[]) if v["id"]==variation_id),None)
                if variation is None:raise ValueError("Select a current, verified Square catalog variation.")
                values=dict(product_id=product_id,square_variation_id=variation_id,active=True,
                    package_configuration=text(proposed.get("packageConfiguration") or variation.get("variationName",""),400),
                    verification_note="Lead-reviewed exact catalog association",evidence=sources)
                if row:
                    if proposed.get("reassign") is not True:
                        raise ValueError("Explicitly propose reassignment of this reviewed association.")
                    identity=row["id"];c.execute(update(table).where(table.c.id==identity).values(**values))
                else:
                    if proposed.get("reassign") is True:raise ValueError("That reviewed association no longer exists.")
                    identity="association-"+uuid.uuid4().hex;c.execute(insert(table).values(id=identity,**values))
            after=dict(c.execute(select(table).where(table.c.id==identity)).mappings().one())
            if before and before["product_id"]!=product_id:
                c.execute(update(product_table).where(product_table.c.id==before["product_id"]).values(version=product_table.c.version+1))
            published={"productId":product_id,"squareVariationId":variation_id,"associationId":identity,"before":before,"after":after}
        else:raise ValueError("Unsupported proposed change.")
        if kind=="relationship":
            rid=published.get("relationshipId") or published.get("removedRelationshipId")
            edge=c.execute(select(self.table("relationships")).where(self.table("relationships").c.id==rid)).mappings().one()
            for related_id in {edge["from_id"],edge["to_id"]}-{product_id}:
                c.execute(update(product_table).where(product_table.c.id==related_id).values(version=product_table.c.version+1))
        c.execute(update(product_table).where(product_table.c.id==product_id).values(version=old["version"]+1))
        return published

    def review(self,principal,suggestion_id,decision,proposed=None,reason=""):
        if decision not in {"APPROVED","REJECTED","MODIFIED_APPROVED"}:raise ValueError("Choose Approve, Reject or Modify & approve.")
        if (decision=="MODIFIED_APPROVED")!=(proposed is not None):raise ValueError("Only modified approval accepts replacement information.")
        reason=text(reason,2000)
        with self.storage._write() as c:
            self._actor(c,principal,"review",True);table=self.table("suggestions")
            row=c.execute(select(table).where(table.c.id==suggestion_id)).mappings().one_or_none()
            if not row:raise LookupError("Suggestion not found.")
            if row["status"]!="PENDING":raise ValueError("That suggestion has already been reviewed.")
            published=None if decision=="REJECTED" else self._publish(c,row,proposed if proposed is not None else row["proposed"])
            if published is not None:published["publishedChanges"]=deepcopy(proposed if proposed is not None else row["proposed"])
            c.execute(update(table).where(table.c.id==suggestion_id).values(status=decision,decided_at=now(),
                decided_by=principal.id,decision_reason=reason,published=published))
            self._audit(c,decision,principal.id,suggestion_id,row["product_id"],row["original"],published)
            return {"id":suggestion_id,"status":decision,"published":published}

    def import_seed(self,path):
        raw=Path(path).read_bytes()
        if len(raw)>2000000:raise ValueError("Seed exceeds the bounded import size.")
        data=json.loads(raw);fingerprint=hashlib.sha256(raw).hexdigest()
        sources={r["key"]:r for r in data.get("evidence",[])}
        def source_values(keys):return evidence([sources[k] for k in keys])
        counts={key:len(data.get(key,[])) for key in ("manufacturers","products","relationships","catalogAssociations")}
        with self.storage._write() as c:
            imports=self.table("seed_imports")
            if c.scalar(select(imports.c.digest).where(imports.c.digest==fingerprint)):return counts
            for row in data["manufacturers"]:
                table=self.table("manufacturers");identity=row["key"]
                if not c.scalar(select(table.c.id).where(table.c.id==identity)):
                    c.execute(insert(table).values(id=identity,name=text(row["name"],200,True),aliases=row.get("aliases",[])))
            for row in data["products"]:
                table=self.table("products");identity=row["key"]
                if c.scalar(select(table.c.id).where(table.c.id==identity)):continue
                body={key:row[key] for key in ("name","kind","model","family","generation","resistanceOhm","capacityMl","configuration","integratedCoil","aliases") if key in row}
                body["manufacturerId"]=row["manufacturerKey"]
                body["evidence"]=source_values(row.get("sourceKeys",[])) if row.get("sourceKeys") else []
                values=self._product_values(c,body);values.update(id=identity,version=1,active=True)
                c.execute(insert(table).values(**values))
                for record in row.get("identifiers",[]):
                    record=deepcopy(record)
                    package=next((r.get("packageConfiguration","") for r in data.get("catalogAssociations",[])
                        if r["productKey"]==identity and r.get("identifier",{}).get("value")==record["value"]),None)
                    if package is not None:record["packageConfiguration"]=package
                    record["evidence"]=source_values([record["sourceKey"]]) if record.get("sourceKey") else []
                    self._put_identifier(c,identity,record)
            for row in data["relationships"]:
                if row.get("status","verified")!="verified":continue
                identity=row.get("key") or "relationship-"+digest([row["fromKey"],row["toKey"],row["kind"],row.get("conditions","")])[:32]
                if c.scalar(select(self.table("relationships").c.id).where(self.table("relationships").c.id==identity)):continue
                self._put_relationship(c,row["fromKey"],row["toKey"],row["kind"],row.get("conditions",""),source_values(row["sourceKeys"]),identity)
            for row in data.get("catalogAssociations",[]):
                table=self.table("catalog_associations")
                if c.scalar(select(table.c.id).where(table.c.square_variation_id==row["squareVariationId"])):continue
                c.execute(insert(table).values(id="association-"+digest(row)[:32],product_id=row["productKey"],
                    square_variation_id=row["squareVariationId"],package_configuration=row.get("packageConfiguration",""),
                    verification_note=text(row.get("verificationNote","Reviewed existing catalog identity"),2000,True),
                    evidence=source_values([row["sourceKey"]]) if row.get("sourceKey") else []))
            c.execute(insert(imports).values(digest=fingerprint,counts=counts,created_at=now()))
            self._audit(c,"SEED_IMPORTED",after={"digest":fingerprint,"counts":counts})
        return counts
