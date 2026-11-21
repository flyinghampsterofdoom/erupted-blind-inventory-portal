"""Existing staff sessions, capability overrides and CSRF protect every operation."""
from types import SimpleNamespace
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from app.auth import Principal, get_current_principal
from app.routers.store import store_access
from app.security.csrf import verify_csrf
from app.services.flavor_explorer.portal_api import PrivateRoute, document
from app.services.flavor_explorer.portal import install_portal, product_navigation

PATH="/v2/compatibility-explorer"
router=APIRouter(prefix=PATH,route_class=PrivateRoute)

def service(request):
    value=getattr(request.app.state,"product_compatibility",None)
    if value is None:raise HTTPException(503,"Compatibility Explorer is not configured for this environment.")
    return value

def guarded(callback):
    try:return callback()
    except PermissionError as error:raise HTTPException(403,str(error))
    except LookupError:raise HTTPException(404,"That compatibility record is unavailable.")
    except (ValueError,TypeError,KeyError) as error:
        detail=str(error)
        raise HTTPException(409,detail if len(detail)<350 else "Review the proposed information and try again.")
    except SQLAlchemyError:
        raise HTTPException(503,"The operation could not be completed; no partial compatibility change was published.")

def render(request,principal,name,extra=None):
    install_portal(request.app)
    fixture=bool(getattr(request.app.state,"flavor_preview_auth",False))
    context=dict(request=request,principal=principal,
        page=SimpleNamespace(slug="compatibility-explorer",label="Compatibility Explorer",
            description="Find verified devices, coils and pods.",badge="Development fixtures" if fixture else "Product Explorer"),
        navigation=product_navigation(request),stores=[],selected_store_ids=[],all_stores_selected=True,
        store_scope_label="Assigned store" if principal.store_id else "Company-wide",
        scope_locked=True,hide_preview_banner=True,development_fixture=fixture,
        fixture_notice="Development preview · Synthetic staff identities. No real employee actions." if fixture else "")
    context.update(extra or {})
    return request.app.state.templates.TemplateResponse(request=request,name=name,context=context)

@router.get("")
def page(request:Request,principal:Principal=Depends(get_current_principal)):
    guarded(lambda:service(request).bootstrap(principal))
    return render(request,principal,"compatibility.html")

@router.get("/api/bootstrap")
def bootstrap(request:Request,principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).bootstrap(principal))

@router.get("/api/search")
def search(request:Request,q:str="",mode:str="auto",principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).search(principal,q,mode))

@router.get("/api/catalog/search")
def catalog_search(request:Request,q:str="",principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).catalog_search(principal,q))

@router.get("/api/products/{product_id}")
def product(request:Request,product_id:str,identifierId:str|None=None,principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).product(principal,product_id,identifierId))

@router.get("/api/products/{product_id}/stock")
def stock(request:Request,product_id:str,identifierId:str|None=None,principal:Principal=Depends(get_current_principal)):
    # Synchronous FastAPI worker: the async listener remains free to fulfill the
    # authenticated loopback stock job while this bounded provider read waits.
    return guarded(lambda:service(request).stock(principal,product_id,identifierId))

@router.post("/api/suggestions",dependencies=[Depends(verify_csrf)])
async def suggest(request:Request,principal:Principal=Depends(get_current_principal)):
    body=await document(request,{"kind","productId","targetProductId","baseVersion","proposed","comment","scannedIdentifier"})
    return guarded(lambda:service(request).suggest(principal,body))

@router.get("/api/suggestions")
def queue(request:Request,status:str="PENDING",principal:Principal=Depends(get_current_principal)):
    return guarded(lambda:service(request).suggestions(principal,status))

@router.post("/api/suggestions/{suggestion_id}/review",dependencies=[Depends(verify_csrf)])
async def review(request:Request,suggestion_id:int,principal:Principal=Depends(get_current_principal)):
    body=await document(request,{"decision","proposed","reason"})
    return guarded(lambda:service(request).review(principal,suggestion_id,body.get("decision"),
        proposed=body.get("proposed"),reason=body.get("reason","")))

@router.get("/customer-request/{product_id}")
def customer_request(request:Request,product_id:str,identifierId:str|None=None,q:str="",principal:Principal=Depends(get_current_principal),scope=Depends(store_access)):
    prefill=guarded(lambda:service(request).customer_request_prefill(principal,product_id,identifierId))
    if len(q)>512:raise HTTPException(409,"The search was not truncated; enter at most 512 characters.")
    query={"productId":product_id,"q":q or (prefill["product"].get("matchedIdentifier") or {}).get("value") or prefill["product"]["name"]}
    if identifierId:query["identifierId"]=identifierId
    prefill["return_url"]=PATH+"?"+urlencode(query)
    return render(request,principal,"compatibility-request.html",{"prefill":prefill})
