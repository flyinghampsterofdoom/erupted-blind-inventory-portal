"""Bounded read-only raster endpoint for the loopback staff review preview."""
import re
from pathlib import Path
from fastapi import APIRouter,HTTPException,Request
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError

PREFIX="/flavor-explorer/media/photos/"
router=APIRouter()

def asset_headers(etag=None):
    headers={"Cache-Control":"private, no-cache" if etag else "no-store","X-Content-Type-Options":"nosniff","Cross-Origin-Resource-Policy":"same-origin"}
    if etag:headers.update({"ETag":etag,"Vary":"Cookie"})
    return headers

def existing_asset(asset_id,*,reference=None):
    if not re.fullmatch(r"[0-9a-f]{64}",asset_id or ""):raise ValueError("Product photo is unavailable.")
    from app.services.flavor_explorer.storage import open_existing
    from app.services.flavor_explorer.product_photos import ProductPhotos
    path=reference.database_path() if reference is not None else Path.home()/"flavor-explorer/intelligence.sqlite3"
    storage=open_existing(path,reference=reference)
    try:return ProductPhotos(storage).asset(asset_id)
    finally:storage.close()

@router.api_route(PREFIX+"{asset_id}.webp",methods=["GET","HEAD"])
def photo_asset(request:Request,asset_id:str):
    if not getattr(request.app.state,"flavor_development_preview",False):
        from app.services.flavor_explorer.staff_access import require_staff_view
        require_staff_view(request)
    service=getattr(request.app.state,"flavor_intelligence",None)
    if service is None:raise HTTPException(503,"Product photos are not configured.")
    if not re.fullmatch(r"[0-9a-f]{64}",asset_id):raise HTTPException(404,"Product photo is unavailable.")
    # Content-addressed retained bytes never change. Authenticate before every
    # revalidation, then let the browser reuse bytes without another blob read.
    etag='"'+asset_id+'"'
    headers=asset_headers(etag)
    if etag in [value.strip() for value in request.headers.get("If-None-Match","").split(",")]:
        return Response(status_code=304,headers=headers)
    try:asset=service.photos.asset(asset_id)
    except ValueError:raise HTTPException(404,"Product photo is unavailable.")
    except (OSError,SQLAlchemyError):raise HTTPException(503,"Product photo is unavailable.")
    return Response(asset["data"] if request.method=="GET" else b"",media_type=asset["mime"],headers={**headers,"Content-Length":str(len(asset["data"]))})
