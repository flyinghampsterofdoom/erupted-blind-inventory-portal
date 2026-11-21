"""Use the portal's current staff permissions for every E-liquid view."""
from fastapi import HTTPException,Request
from app.auth import get_current_principal

def require_staff_view(request:Request):
    principal=get_current_principal(request)
    flags=getattr(request.state,"permission_flags",None)
    if not isinstance(flags,dict) or not any(flags.get(key) is True for key in ("store.access","touchscreen.view")):
        raise HTTPException(403,"E-liquid Explorer access is required.")
    return principal
