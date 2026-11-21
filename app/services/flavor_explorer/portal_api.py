"""Authenticated portal transport for the existing Flavor Intelligence authority."""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,threading,time
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError,NoResultFound
from sqlalchemy.orm import Session
from app.auth import Principal,Role,get_current_principal
from app.services.flavor_explorer.staff_access import require_staff_view
from app.db import get_db
from app.security.csrf import verify_csrf
from app.services.access_control_service import principal_has_permission
from app.services.flavor_explorer.cache import read_snapshot
from app.services.flavor_explorer.square_catalog import normalize_catalog

EMPTY={"description":"","notes":[],"categories":[],"sensory":{},"similarities":[]}
AXES=[("sweetness","Sweetness"),("tartness","Tartness"),("creaminess","Creaminess"),
      ("richness","Richness"),("brightness","Brightness"),("cooling","Cooling"),("crispness","Crispness")]
CATEGORY_ALIASES={"fruit":"fruity","ice":"mint-menthol"}
API="/v2/flavor-intelligence/api"

def utc_stamp(value):
    if isinstance(value,datetime):
        if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return value

def fingerprint(flavor_id,version_id,snapshot):
    raw=json.dumps([flavor_id,version_id,snapshot],sort_keys=True,separators=(",",":"),allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()

class PrivateRoute(APIRoute):
    def get_route_handler(self):
        original=super().get_route_handler()
        async def handler(request):
            response=await original(request)
            response.headers["Cache-Control"]="no-store"
            response.headers["X-Content-Type-Options"]="nosniff"
            return response
        return handler

def administrator(principal:Principal=Depends(get_current_principal),db:Session=Depends(get_db)):
    if principal.role not in {Role.ADMIN,Role.MANAGER}:
        raise HTTPException(403,"Administrator access is required.")
    for key in ("touchscreen.view","touchscreen.manage_flavors","touchscreen.publish"):
        if not principal_has_permission(db,principal=principal,permission_key=key,fallback_allowed=True):
            raise HTTPException(403,"Flavor administration permission is required.")
    return principal

def service(request):
    result=getattr(request.app.state,"flavor_intelligence",None)
    if result is None:raise HTTPException(503,"Flavor Intelligence is not configured for this environment.")
    return result

async def document(request,allowed,*,max_bytes=262144):
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>max_bytes:raise HTTPException(413,"The selected operation is too large.")
    try:value=json.loads(raw)
    except (ValueError,TypeError):raise HTTPException(422,"A valid operation is required.")
    if not isinstance(value,dict) or set(value)-set(allowed):
        raise HTTPException(422,"The operation contains unsupported fields.")
    return value

def guarded(callback):
    try:return callback()
    except PermissionError:raise HTTPException(403,"Your account cannot perform this action.")
    except NoResultFound:raise HTTPException(404,"That classification record is unavailable.")
    except (ValueError,KeyError,TypeError) as error:
        # Domain messages describe conflicts and validation, never provider data.
        message=str(error)
        raise HTTPException(409,message if len(message)<300 else "The classification changed. Review it again.")
    except SQLAlchemyError:
        raise HTTPException(503,"The change could not be completed. No partial operation was published.")

class PortalService:
    def __init__(self,storage,snapshot_loader=read_snapshot,*,product_enricher=None):
        from app.services.flavor_explorer.administration import FlavorAdministration
        self.storage=storage;self.domain=FlavorAdministration(storage);self.snapshot_loader=snapshot_loader;self.product_enricher=product_enricher
        from app.services.flavor_explorer.discovery import DiscoveryVisibility
        self.discovery=DiscoveryVisibility(storage);self.discovery_plans={}
        from app.services.flavor_explorer.product_photos import ProductPhotos
        self.photos=ProductPhotos(storage)
        self.plans={};self.plan_lock=threading.Lock()

    def catalog(self,principal):
        products=normalize_catalog(self.snapshot_loader())["products"]
        catalog_images={product["id"]:product.get("imageUrl") for product in products}
        if self.product_enricher is not None:self.product_enricher(products)
        originals={p["id"]:p.get("originalImageUrl") for p in products}
        self.photos.overlay(products)
        state=self.domain.catalog_state([v["id"] for p in products for v in p["variations"]],actor_id=principal.id)
        links=state["byVariation"];profiles=state["profiles"]
        visibility_states=self.discovery.states([v["id"] for p in products for v in p["variations"]])
        categories=state["taxonomy"]["categories"]
        by_slug={row["slug"]:row["id"] for row in categories}
        groups=self.storage.table("fi_suggestion_review_groups")
        with self.storage.engine.connect() as connection:
            pending=Counter(connection.execute(select(groups.c.flavor_id).where(groups.c.status=="PENDING")).scalars())
        result=[]
        for product in products:
            identities={links.get(v["id"]) for v in product["variations"]}
            if None in identities or len(identities)!=1:continue
            flavor_id=identities.pop()
            profile=profiles.get(flavor_id) or profiles.get(str(flavor_id)) or {}
            current=deepcopy(profile.get("snapshot") or EMPTY)
            version=profile.get("versionId")
            verified=bool(profile.get("verified") and current!=EMPTY)
            inferred={"description":"","notes":product.get("notes",[]),
                "categories":sorted({by_slug[CATEGORY_ALIASES.get(c,c)] for c in product.get("categories",[]) if CATEGORY_ALIASES.get(c,c) in by_slug}),
                "sensory":{key:value/5 for key,value in (product.get("sensory") or {}).items()
                           if product.get("notes") and type(value) in (int,float)},"similarities":[]}
            confidence="verified" if verified else "provisional" if pending[flavor_id] else "inferred" if inferred["notes"] else "unclassified"
            variation_ids=[v["id"] for v in product["variations"]]
            excluded_count=sum(visibility_states[identity]["excluded"] for identity in variation_ids)
            visibility="included" if not excluded_count else "excluded" if excluded_count==len(variation_ids) else "partial"
            result.append({"id":product["id"],"flavorId":flavor_id,"brand":product["brand"],
                "name":product["name"].lstrip("*").replace(" · *"," · "),"type":product["type"],
                "strengths":product["strengths"],"seriesName":product.get("seriesName",""),
                "sizeLabel":product.get("sizeLabel",""),"imageUrl":product.get("imageUrl"),
                "catalogImageUrl":catalog_images.get(product["id"]),"originalImageUrl":originals.get(product["id"]),"imageSource":product.get("imageSource"),"photoMode":product.get("photoMode","catalog"),
                "squareVariationIds":variation_ids,"explorerVisibility":visibility,
                "excludedVariationCount":excluded_count,"variationCount":len(variation_ids),
                "visibilityVersion":hashlib.sha256(json.dumps(sorted((identity,visibility_states[identity]["version"]) for identity in variation_ids)).encode()).hexdigest(),
                "confidence":confidence,"publishedVersionId":version,"classification":current,
                "inferredClassification":inferred,"pendingCount":pending[flavor_id],
                "classificationFingerprint":fingerprint(flavor_id,version,current)})
        return result,state["taxonomy"]


    def discovery_products(self,principal,q="",product_type="",visibility="all"):
        if visibility not in {"all","included","excluded"}:
            raise ValueError("Choose All, Included or Excluded Explorer products.")
        if product_type not in {"","freebase","salt","disposable"}:
            raise ValueError("Choose an available product type.")
        rows,_=self.catalog(principal)
        tokens=q.casefold().split()
        keys=("id","brand","name","type","strengths","squareVariationIds","imageUrl","imageSource","photoMode","sizeLabel","explorerVisibility",
              "excludedVariationCount","variationCount","visibilityVersion")
        return [{key:row[key] for key in keys} for row in rows
            if (not product_type or row["type"]==product_type)
            and (visibility=="all" or (row["explorerVisibility"]=="excluded")== (visibility=="excluded"))
            and all(token in (row["brand"]+" "+row["name"]+" "+row.get("seriesName","")).casefold()
                    for token in tokens)]

    def preview_discovery(self,principal,body):
        selected=body.get("productIds")
        if (not isinstance(selected,list) or not 0<len(selected)<=500
                or any(not isinstance(value,str) or not value or len(value)>160 for value in selected)
                or len(set(selected))!=len(selected)):
            raise ValueError("Choose up to 500 distinct products.")
        excluded=body.get("excluded")
        if type(excluded) is not bool:raise ValueError("Choose whether to exclude or restore the products.")
        reason=body.get("reason")
        if reason is None:reason="Products no longer carried" if excluded else "Restore products to E-liquid Explorer"
        if not isinstance(reason,str) or not reason.strip() or len(reason)>500:
            raise ValueError("Use a short change note of up to 500 characters.")
        rows,_=self.catalog(principal);by_id={row["id"]:row for row in rows}
        if any(value not in by_id for value in selected):
            raise ValueError("A selected product is no longer in the catalog. Refresh the selection.")
        plan=self.discovery.preview(actor_id=principal.id,products=[by_id[value] for value in selected],
                                    excluded=excluded,reason=reason.strip())
        now=time.time()
        with self.plan_lock:
            self.discovery_plans={key:item for key,item in self.discovery_plans.items() if item[0]>now}
            if len(self.discovery_plans)>=256:raise ValueError("Finish an existing setup preview first.")
            self.discovery_plans[plan["id"]]=(now+600,deepcopy(plan))
        return {"previewId":plan["id"],"expiresAt":plan["expiresAt"],"excluded":excluded,
            "selectedCount":len(plan["products"]),"changedCount":plan["changedProductCount"],
            "unchangedCount":plan["unchangedProductCount"],"variationChangedCount":plan["changedCount"],
            "products":[{**{key:row[key] for key in ("id","brand","name","type","variationCount",
                         "excludedVariationCount","afterExcludedVariationCount")},
                         "before":row["beforeVisibility"],"after":row["afterVisibility"]} for row in plan["products"]],
            "warnings":[]}

    def commit_discovery(self,principal,preview_id):
        if not isinstance(preview_id,str) or not preview_id or len(preview_id)>80:
            raise ValueError("Choose a valid Explorer setup preview.")
        with self.plan_lock:item=self.discovery_plans.get(preview_id)
        if not item or item[0]<=time.time():
            raise ValueError("This Explorer setup preview expired. Review the selection again.")
        rows,_=self.catalog(principal)
        result=self.discovery.commit(actor_id=principal.id,plan=deepcopy(item[1]),currentproducts=rows)
        return {"operationId":result["id"],"previewId":preview_id,"excluded":result["excluded"],
            "changedCount":result["changedProductCount"],"unchangedCount":result["unchangedProductCount"],
            "variationChangedCount":result["changedCount"],"replayed":result["replayed"]}


    def product_photo(self,principal,product_id):
        row=self.product(principal,product_id)
        return {"product":{key:row.get(key) for key in ("id","brand","name","type","sizeLabel","strengths")},
                "photo":self.photos.describe(row)}

    def change_product_photo(self,principal,product_id,body):
        import base64,binascii
        row=self.product(principal,product_id)
        mode=body.get("mode","catalog")
        action=body.get("action");source_kind=body.get("sourceKind")
        if mode=="custom" and body.get("restoreEventId") is None and source_kind not in {"manufacturer","own"}:
            raise ValueError("Choose Manufacturer photo or Our photo.")
        if action=="remove_candidate" and any(key in body for key in ("mode","dataBase64","restoreEventId","sourcePageUrl","sourceImageUrl","sourceLabel","rightsNote")):
            raise ValueError("Remove a retained candidate without replacing its bytes or source.")
        token=body.get("expectedToken")
        if not isinstance(token,str) or len(token)!=64:
            raise ValueError("Refresh this product photo before changing it.")
        restore=body.get("restoreEventId")
        if restore is not None and (type(restore) is not int or restore<=0):
            raise ValueError("Choose a valid retained photo.")
        data=None
        if "dataBase64" in body:
            encoded=body["dataBase64"]
            if not isinstance(encoded,str) or len(encoded)>2_796_204:
                raise ValueError("Upload an image no larger than 2 MiB.")
            try:data=base64.b64decode(encoded,validate=True)
            except (ValueError,binascii.Error):raise ValueError("The uploaded photo is invalid.")
            if not data or len(data)>2*1024*1024:raise ValueError("Upload an image no larger than 2 MiB.")
        metadata_keys=("sourcePageUrl","sourceImageUrl","sourceLabel","rightsNote")
        if restore is not None:
            if data is not None or any(key in body for key in metadata_keys):
                raise ValueError("A retained photo must keep its original bytes and source.")
            metadata=None
        else:metadata=None if action=="remove_candidate" else {key:body[key] for key in metadata_keys if key in body}
        def current_product():
            # Called after raster normalization while the photo write is serialized.
            # Read source facts directly; self.product/catalog would recurse into DB projections.
            candidates=normalize_catalog(self.snapshot_loader())["products"]
            if self.product_enricher is not None:self.product_enricher(candidates)
            return next((product for product in candidates if product["id"]==product_id),None)
        changed=self.photos.apply(actor_id=principal.id,product=row,expected_token=token,mode=mode,
                                  data=data,metadata=metadata,restore_event_id=restore,source_kind=source_kind,action=action,
                                  current_product_loader=current_product)
        return {"product":{key:row.get(key) for key in ("id","brand","name","type","sizeLabel","strengths")},
                "photo":changed,"operationId":changed["operationId"],"changedCount":changed["changedCount"]}

    def unavailable_product(self,principal,flavor_id):
        # Private retained-record lookup only. Never add these placeholders to
        # normalized catalog results or the public customer projection.
        if principal.role not in {Role.ADMIN,Role.MANAGER}:raise PermissionError("Administrator access is required.")
        if type(flavor_id) is not int or flavor_id<=0:raise HTTPException(404,"That classification record is unavailable.")
        links=self.storage.table("fi_square_flavor_links");flavors=self.storage.table("touchscreen_flavors")
        groups=self.storage.table("fi_suggestion_review_groups")
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,principal.id,admin=True)
            if not connection.execute(select(flavors.c.id).where(flavors.c.id==flavor_id)).first():
                raise HTTPException(404,"That classification record is unavailable.")
            identities=[dict(row) for row in connection.execute(select(links).where(links.c.flavor_id==flavor_id)).mappings()]
            head=self.storage._current(connection,flavor_id)
            pending=sum(1 for status in connection.execute(select(groups.c.status).where(groups.c.flavor_id==flavor_id)).scalars() if status=="PENDING")
        current=deepcopy(head["snapshot"] if head else EMPTY)
        version=head["version_id"] if head else None
        product_id="unavailable-flavor-"+str(flavor_id)
        return {"id":product_id,"flavorId":flavor_id,"brand":"Catalog unavailable",
            "name":"Previously catalogued product","type":"unavailable","strengths":[],"seriesName":"",
            "squareItemIds":sorted({row["square_item_id"] for row in identities}),
            "squareVariationIds":sorted({row["square_variation_id"] for row in identities}),
            "confidence":"verified" if head and not head["is_baseline"] and current!=EMPTY else "unclassified",
            "publishedVersionId":version,"classification":current,"inferredClassification":deepcopy(EMPTY),
            "pendingCount":pending,"classificationFingerprint":fingerprint(flavor_id,version,current),
            "catalogUnavailable":True,"historyReadOnly":True,
            "historyUrl":API+"/products/"+product_id+"/history"}

    def product(self,principal,product_id):
        rows,_=self.catalog(principal)
        row=next((row for row in rows if row["id"]==product_id),None)
        if row is None and isinstance(product_id,str) and product_id.startswith("unavailable-flavor-"):
            suffix=product_id.removeprefix("unavailable-flavor-")
            if not suffix.isascii() or not suffix.isdigit() or suffix!=str(int(suffix)):
                raise HTTPException(404,"That classification record is unavailable.")
            flavor_id=int(suffix)
            if principal.role not in {Role.ADMIN,Role.MANAGER}:raise PermissionError("Administrator access is required.")
            row=next((row for row in rows if row["flavorId"]==flavor_id),None) or self.unavailable_product(principal,flavor_id)
        if row is None:raise HTTPException(404,"That catalog product is unavailable.")
        return row

    def remember(self,plan,rows):
        now=time.time()
        with self.plan_lock:
            self.plans={key:value for key,value in self.plans.items() if value[0]>now}
            if len(self.plans)>=256:raise ValueError("Finish an existing preview before creating another.")
            preview_id=plan["id"]
            self.plans[preview_id]=(now+600,deepcopy(plan))
        by_flavor={row["flavorId"]:row for row in rows}
        changes=plan.get("changes",[])
        if not changes and plan.get("reviewGroups"):
            ids={group["flavorId"] for group in plan["reviewGroups"]}
            changes=[{"flavorId":identity,"before":by_flavor[identity]["classification"],"after":by_flavor[identity]["classification"]} for identity in ids if identity in by_flavor]
        products=[]
        for change in changes:
            row=by_flavor.get(change["flavorId"],{})
            products.append({"id":row.get("id"),"flavorId":change["flavorId"],
                "name":(row.get("brand","")+" "+row.get("name","")).strip(),
                "before":change["before"],"after":change["after"]})
        changed=sum(p["before"]!=p["after"] for p in products)
        return {"previewId":preview_id,"expiresAt":plan.get("expiresAt"),"changedCount":changed,
                "unchangedCount":len(products)-changed,"products":products,"kind":plan["kind"],"warnings":["Published classifications stay unchanged; the selected review groups will be rejected."] if plan.get("reviewGroups") and all(g["decision"]=="REJECTED" for g in plan["reviewGroups"]) else []}

    def preview(self,principal,body,db):
        rows,_=self.catalog(principal)
        by_id={row["id"]:row for row in rows}
        selected=body.get("productIds") or []
        if not isinstance(selected,list) or len(selected)>500 or len(set(selected))!=len(selected):
            raise ValueError("Choose up to 500 distinct products.")
        chosen=[by_id[item] for item in selected]
        reason=body.get("reason") or "Reviewed Flavor Intelligence classification"
        kind=body.get("kind")
        if kind=="publish":
            if len(chosen)!=1:raise ValueError("Choose one product.")
            row=chosen[0]
            plan=self.domain.preview_publish(actor_id=principal.id,flavor_id=row["flavorId"],
                snapshot=body["classification"],expected_version_id=row["publishedVersionId"],reason=reason)
        elif kind=="bulk":
            if not chosen:raise ValueError("Choose products for the bulk operation.")
            plan=self.domain.preview_bulk(actor_id=principal.id,flavor_ids=[row["flavorId"] for row in chosen],
                patch=body["operation"],expected_versions={row["flavorId"]:row["publishedVersionId"] for row in chosen},reason=reason)
        elif kind=="review":
            group_ids=body.get("groupIds")
            if not isinstance(group_ids,list) or not group_ids or len(group_ids)>100 or any(type(v) is not int for v in group_ids):
                raise ValueError("Choose up to 100 review groups.")
            modified=body.get("modifiedPatch")
            plan=self.domain.preview_review(actor_id=principal.id,group_ids=group_ids,
                decision=body.get("decision"),modified_patches={group_id:modified for group_id in group_ids} if modified is not None else None,reason=reason)
        elif kind=="restore":
            if len(chosen)!=1:raise ValueError("Choose one product.")
            row=chosen[0]
            plan=self.domain.preview_restore(actor_id=principal.id,flavor_id=row["flavorId"],
                restores_version_id=body["restoreVersionId"],expected_version_id=row["publishedVersionId"],reason=reason)
        elif kind=="reverse":
            plan=self.domain.preview_reverse(actor_id=principal.id,operation_id=body["operationId"],reason=reason)
        else:raise ValueError("Choose a supported classification action.")
        available={row["flavorId"] for row in rows}
        if any(change.get("changed",True) and change["flavorId"] not in available for change in plan.get("changes",[])):
            raise ValueError("Unavailable catalog products may be rejected or inspected in history, not published.")
        for group in plan.get("reviewGroups",[]):
            if group["flavorId"] not in available:
                rows.append(self.unavailable_product(principal,group["flavorId"]))
                available.add(group["flavorId"])
        self.authorize_fields(principal,db,plan)
        return self.remember(plan,rows)

    def authorize_fields(self,principal,db,plan):
        for field,key in (("categories","touchscreen.manage_categories"),("similarities","touchscreen.manage_recommendations")):
            if any(change["before"].get(field)!=change["after"].get(field) for change in plan.get("changes",[])):
                if not principal_has_permission(db,principal=principal,permission_key=key,fallback_allowed=True):
                    raise PermissionError("Classification field permission is required.")

    def commit(self,principal,preview_id,db):
        if not isinstance(preview_id,str) or len(preview_id)>80:raise ValueError("Choose a valid operation preview.")
        with self.plan_lock:
            item=self.plans.get(preview_id)
        if not item or item[0]<=time.time():raise ValueError("This preview expired. Review the operation again.")
        self.authorize_fields(principal,db,item[1])
        operations=self.storage.table("fi_administration_operations")
        with self.storage.engine.connect() as connection:
            committed=connection.execute(select(operations.c.id).where(operations.c.preview_id==preview_id)).first()
        if not committed and any(change.get("changed",True) for change in item[1].get("changes",[])):
            rows,_=self.catalog(principal);available={row["flavorId"] for row in rows}
            if any(change.get("changed",True) and change["flavorId"] not in available for change in item[1]["changes"]):
                raise ValueError("Catalog availability changed; no classification was published. Review retained history or reject pending suggestions.")
        return self.domain.commit(actor_id=principal.id,plan=item[1])

    def names(self):
        with self.storage.engine.connect() as connection:
            principals={r["id"]:r["username"] for r in connection.execute(select(self.storage.table("principals"))).mappings()}
            stores={r["id"]:r["name"] for r in connection.execute(select(self.storage.table("stores"))).mappings()}
        return principals,stores

    def queue(self,principal,status="PENDING"):
        combined_approved=status=="approved"
        raw=self.domain.queue(actor_id=principal.id,status="ALL" if combined_approved else status)
        rows,_=self.catalog(principal);by_flavor={row["flavorId"]:row for row in rows}
        people,stores=self.names()
        result=[]
        for group in raw["groups"]:
            if combined_approved and group["status"] not in {"APPROVED","MODIFIED_APPROVED"}:continue
            row=by_flavor.get(group["flavorId"]) or self.unavailable_product(principal,group["flavorId"])
            submissions=[]
            for sub in group["submissions"]:
                submissions.append({**sub,"employee":people.get(sub.get("employee_id",sub.get("employeeId")),"Existing staff"),
                    "store":stores.get(sub.get("store_id",sub.get("storeId")),"Recorded store"),
                    "submittedAt":sub.get("submitted_at",sub.get("submittedAt")),
                    "proposedPatch":sub.get("proposed_patch",sub.get("proposedPatch",{}))})
            result.append({**group,"productId":row["id"],"product":row,"current":row["classification"],
                "catalogUnavailable":bool(row.get("catalogUnavailable")),
                "historyUrl":API+"/products/"+row["id"]+"/history",
                "proposed":group.get("proposed"),"diff":group.get("differences",[]),"submissions":submissions})
        return {"groups":result}

    def history(self,principal,product_id):
        row=self.product(principal,product_id);raw=self.domain.history(actor_id=principal.id,flavor_id=row["flavorId"])
        people,_=self.names()
        versions=[{**v,"createdAt":v.get("created_at",v.get("createdAt")),
            "actor":people.get(v.get("created_by_principal_id",v.get("actorId")),"Existing administrator"),
            "restoresVersionId":v.get("restores_version_id",v.get("restoresVersionId"))} for v in raw.get("versions",[])]
        table=self.storage.table("fi_administration_operations")
        operation_ids={item["operation_id"] for item in raw.get("operations",[])}
        with self.storage.engine.connect() as connection:
            operations=[dict(item) for item in connection.execute(select(table).where(table.c.id.in_(operation_ids))).mappings()] if operation_ids else []
        operations=[{**item,"actor":people.get(item["actor_id"]),"created_at":utc_stamp(item["created_at"]),"createdAt":utc_stamp(item["created_at"])} for item in operations if item["kind"] in {"bulk","reverse"}]
        events=[{**event,"actor":people.get(event["actor_id"])} for event in raw.get("audit",[])]
        return {**raw,"product":row,"versions":versions,"events":events,"operations":operations}

    def analytics(self,principal):
        from zoneinfo import ZoneInfo
        rows,_=self.catalog(principal);by_flavor={row["flavorId"]:row for row in rows}
        result=self.domain.analytics(actor_id=principal.id,flavor_ids=list(by_flavor))
        people,stores=self.names();coverage=dict(Counter(row["confidence"] for row in rows))
        result["classificationCoverage"]=coverage
        result["counts"].update(verifiedProducts=coverage.get("verified",0),inferredProducts=coverage.get("inferred",0),
            provisionalProducts=coverage.get("provisional",0),unclassifiedProducts=coverage.get("unclassified",0),totalProducts=len(rows))
        result["employees"]=[{"employee":people.get(person["employeeId"]),"store":", ".join(stores.get(store,"Recorded store") for store in person["stores"]),
            "submitted":person["count"],"approved":person["approved"],"submissionIds":person["submissionIds"]} for person in result["employeeParticipation"]]
        submissions=self.storage.table("fi_employee_suggestions")
        with self.storage.engine.connect() as connection:
            recorded=[dict(item) for item in connection.execute(select(submissions)).mappings()]
        repeats=Counter(item["flavor_id"] for item in recorded)
        result["repeatedProducts"]=[{"productId":by_flavor[flavor]["id"],"product":by_flavor[flavor],
            "corrections":count,"submissionIds":[item["id"] for item in recorded if item["flavor_id"]==flavor]}
            for flavor,count in repeats.most_common() if count>1 and flavor in by_flavor]
        result["recentPublished"]=[{"productId":by_flavor[item["flavor_id"]]["id"],"product":by_flavor[item["flavor_id"]],
            "actor":people.get(item["reviewed_by_principal_id"]),"at":item["published_at"],"action":item["action"],"versionId":item["version_id"]}
            for item in result["recentChanges"] if item["flavor_id"] in by_flavor]
        days=Counter()
        for item in recorded:
            stamp=item["submitted_at"]
            stamp=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(stamp)
            if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
            days[stamp.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()]+=1
        result["daily"]=[{"date":date,"count":count} for date,count in sorted(days.items())]
        result["operations"]=[{**item,"actor":people.get(item["actorId"])} for item in result["operations"] if item["kind"] in {"bulk","reverse"}]
        return result

    def own_suggestions(self,principal):
        rows,_=self.catalog(principal);by_flavor={row["flavorId"]:row for row in rows}
        data=self.domain.my_suggestions(employee_id=principal.id)["submissions"]
        decisions=self.storage.table("fi_review_decisions")
        group_ids={item["review_group_id"] for item in data}
        with self.storage.engine.connect() as connection:
            reasons=dict(connection.execute(select(decisions.c.review_group_id,decisions.c.reason).where(decisions.c.review_group_id.in_(group_ids))).all()) if group_ids else {}
        return {"items":[{"id":item["id"],"product":by_flavor.get(item["flavor_id"],{"name":"Previously catalogued product"}),
            "productId":by_flavor.get(item["flavor_id"],{}).get("id"),"submittedAt":item["submitted_at"],"status":item["status"],
            "reviewNote":reasons.get(item["review_group_id"]),"proposedPatch":item["proposed_patch"]} for item in data]}

    def suggest(self,principal,request,product_id,body):
        row=self.product(principal,product_id)
        if body.get("baseVersionId")!=row["publishedVersionId"] or body.get("baseFingerprint")!=row["classificationFingerprint"]:
            raise ValueError("The classification changed. Review the latest guide before submitting.")
        snapshot=self.snapshot_loader();store_ids={s["localStoreId"] for s in snapshot["stores"] if s.get("verified")}
        store_id=principal.store_id or getattr(request.state,"current_store_id",None)
        if store_id not in store_ids:raise PermissionError("An assigned current store is required.")
        return self.storage.submit_suggestion(flavor_id=row["flavorId"],employee_id=principal.id,store_id=store_id,
            base_version_id=row["publishedVersionId"],proposed_patch=body["changes"],comment=body.get("comment"))

admin_router=APIRouter(prefix="/v2/flavor-intelligence",tags=["flavor-intelligence"],route_class=PrivateRoute,dependencies=[Depends(require_staff_view)])
employee_router=APIRouter(tags=["flavor-suggestions"],route_class=PrivateRoute,dependencies=[Depends(require_staff_view)])

@admin_router.get("")
def admin_page(request:Request,principal:Principal=Depends(administrator)):
    from app.services.flavor_explorer.portal import render_portal
    service(request)
    return render_portal(request,principal)

@employee_router.get("/v2/flavor-suggestions")
def staff_page(request:Request,principal:Principal=Depends(get_current_principal)):
    from app.services.flavor_explorer.portal import render_portal
    service(request)
    return render_portal(request,principal,staff=True)

@admin_router.get("/api/bootstrap")
def bootstrap(request:Request,principal:Principal=Depends(get_current_principal),db:Session=Depends(get_db)):
    rows,taxonomy=guarded(lambda:service(request).catalog(principal))
    can_admin=principal.role in {Role.ADMIN,Role.MANAGER} and all(
        principal_has_permission(db,principal=principal,permission_key=key,fallback_allowed=True)
        for key in ("touchscreen.view","touchscreen.manage_flavors","touchscreen.publish"))
    return {"principal":{"id":principal.id,"username":principal.username,"role":principal.role.value,"storeId":principal.store_id,"store":service(request).names()[1].get(principal.store_id)},
        "capabilities":{"admin":can_admin,"manageExplorerVisibility":can_admin,"manageExplorerPhotos":can_admin},"taxonomy":{**taxonomy,"sensoryAxes":[{"slug":key,"label":label} for key,label in AXES]},
        "developmentPreview":bool(getattr(request.app.state,"flavor_development_preview",False))}

@admin_router.get("/api/products")
def products(request:Request,q:str="",confidence:str="",type:str="",principal:Principal=Depends(get_current_principal)):
    rows,_=guarded(lambda:service(request).catalog(principal))
    tokens=q.casefold().split()
    result=[row for row in rows if (not confidence or row["confidence"]==confidence) and (not type or row["type"]==type)
        and all(token in (row["brand"]+" "+row["name"]+" "+" ".join(row["classification"]["notes"])+" "+
                         " ".join(row["inferredClassification"]["notes"])).casefold() for token in tokens)]
    return {"items":result,"count":len(result)}

@admin_router.get("/api/products/{product_id}")
def product(request:Request,product_id:str,principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).product(principal,product_id))

@admin_router.post("/api/products/{product_id}/suggest",dependencies=[Depends(verify_csrf)])
async def suggest(request:Request,product_id:str,principal:Principal=Depends(get_current_principal)):
    body=await document(request,{"baseVersionId","baseFingerprint","changes","comment"})
    result=guarded(lambda:service(request).suggest(principal,request,product_id,body))
    return {**result,"submissionId":result["submission_id"],"status":"PENDING"}

@admin_router.get("/api/my-suggestions")
def own_suggestions(request:Request,principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).own_suggestions(principal))

@admin_router.get("/api/suggestions")
def suggestions(request:Request,status:str="PENDING",principal:Principal=Depends(administrator)):
    return guarded(lambda:service(request).queue(principal,status or "ALL"))

@admin_router.get("/api/products/{product_id}/history")
def history(request:Request,product_id:str,principal:Principal=Depends(administrator)):
    return guarded(lambda:service(request).history(principal,product_id))

@admin_router.get("/api/analytics")
def analytics(request:Request,principal:Principal=Depends(administrator)):
    return guarded(lambda:service(request).analytics(principal))

@admin_router.post("/api/preview",dependencies=[Depends(verify_csrf)])
async def preview(request:Request,principal:Principal=Depends(administrator),db:Session=Depends(get_db)):
    body=await document(request,{"kind","productIds","classification","operation","groupIds","decision","modifiedPatch","restoreVersionId","operationId","reason"})
    return guarded(lambda:service(request).preview(principal,body,db))

@admin_router.post("/api/commit",dependencies=[Depends(verify_csrf)])
async def commit(request:Request,principal:Principal=Depends(administrator),db:Session=Depends(get_db)):
    body=await document(request,{"previewId"})
    result=guarded(lambda:service(request).commit(principal,body.get("previewId"),db))
    return {**result,"operationId":result["id"]}


@admin_router.get("/api/discovery/products")
def discovery_products(request:Request,q:str="",type:str="",visibility:str="all",principal:Principal=Depends(administrator)):
    items=guarded(lambda:service(request).discovery_products(principal,q,type,visibility))
    return {"items":items,"count":len(items)}

@admin_router.post("/api/discovery/preview",dependencies=[Depends(verify_csrf)])
async def discovery_preview(request:Request,principal:Principal=Depends(administrator)):
    body=await document(request,{"productIds","excluded","reason"})
    return guarded(lambda:service(request).preview_discovery(principal,body))

@admin_router.post("/api/discovery/commit",dependencies=[Depends(verify_csrf)])
async def discovery_commit(request:Request,principal:Principal=Depends(administrator)):
    body=await document(request,{"previewId"})
    return guarded(lambda:service(request).commit_discovery(principal,body.get("previewId")))


@admin_router.get("/api/products/{product_id}/photo")
def product_photo(request:Request,product_id:str,principal:Principal=Depends(administrator)):
    return guarded(lambda:service(request).product_photo(principal,product_id))

@admin_router.post("/api/products/{product_id}/photo",dependencies=[Depends(verify_csrf)])
async def change_product_photo(request:Request,product_id:str,principal:Principal=Depends(administrator)):
    body=await document(request,{"expectedToken","mode","dataBase64","sourcePageUrl","sourceImageUrl",
                                 "sourceLabel","rightsNote","restoreEventId","sourceKind","action"},max_bytes=3_000_000)
    return guarded(lambda:service(request).change_product_photo(principal,product_id,body))
