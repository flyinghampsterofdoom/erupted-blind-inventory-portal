"""Authenticated staff Explorer documents, projection and retained original photos."""
from pathlib import Path
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import FileResponse,Response,RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from app.services.digital_signage_storage import StorageUnavailable
from botocore.exceptions import BotoCoreError,ClientError
from app.services.flavor_explorer.staff_access import require_staff_view
from app.services.flavor_explorer.presentation import CSP,portal_document_path
from app.services.flavor_explorer.photo_media import asset_headers
from app.services.flavor_explorer.portal_api import PrivateRoute

ROOT=Path(__file__).resolve().parents[1]/"static/v2/flavor-explorer"
router=APIRouter(route_class=PrivateRoute)

class ProductStaticFiles(StaticFiles):
    async def get_response(self,path,scope):
        if path.casefold().startswith("flavor-explorer/") and portal_document_path(path):
            raise HTTPException(404)
        return await super().get_response(path,scope)

def staff(principal=Depends(require_staff_view)):
    return principal

def available(request):
    runtime=getattr(request.app.state,"product_explorer",None)
    if runtime is None:raise HTTPException(503,"Product Explorer is temporarily unavailable.")
    return runtime

@router.get("/explorer",include_in_schema=False)
def redirect(principal=Depends(staff)):
    return RedirectResponse("/explorer/",status_code=307)

@router.get("/explorer/api/catalog")
def catalog(request:Request,principal=Depends(staff)):
    try:body,compressed,etag=available(request).projection.get()
    except (ValueError,OSError,KeyError,TypeError,SQLAlchemyError):
        raise HTTPException(503,"Catalog temporarily unavailable. Please check with staff.")
    headers={"ETag":etag,"Vary":"Accept-Encoding","Cache-Control":"private, no-cache"}
    if request.headers.get("If-None-Match")==etag:return Response(status_code=304,headers=headers)
    if "gzip" in request.headers.get("Accept-Encoding",""):
        body=compressed;headers["Content-Encoding"]="gzip"
    return Response(body,media_type="application/json",headers=headers)

@router.api_route("/flavor-explorer/media/originals/{asset_id}",methods=["GET","HEAD"])
def original(request:Request,asset_id:int,principal=Depends(staff)):
    try:asset=available(request).originals.asset(asset_id)
    except LookupError:raise HTTPException(404,"Product photo is unavailable.")
    except (ValueError,OSError,SQLAlchemyError,StorageUnavailable,BotoCoreError,ClientError):
        raise HTTPException(503,"Product photo is unavailable.")
    return Response(asset["data"] if request.method=="GET" else b"",media_type=asset["mime"],headers=asset_headers())

@router.get("/explorer/{asset_path:path}",include_in_schema=False)
def explorer(request:Request,asset_path:str="",principal=Depends(staff)):
    relative=asset_path or "index.html"
    if portal_document_path(relative):raise HTTPException(404)
    target=(ROOT/relative).resolve()
    if not target.is_relative_to(ROOT.resolve()) or not target.is_file() or target.suffix.lower() not in {".html",".mjs",".js",".css",".svg",".png",".webp",".ico",".json"}:
        raise HTTPException(404)
    if target.suffix.lower()==".html" and target!=ROOT/"index.html":raise HTTPException(404)
    return FileResponse(target,headers={"Cache-Control":"no-cache","Content-Security-Policy":CSP,
        "X-Content-Type-Options":"nosniff","Referrer-Policy":"no-referrer"})
