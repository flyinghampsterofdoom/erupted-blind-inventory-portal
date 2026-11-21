"""Compose both employee Product Explorers in the existing V2 portal shell."""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
from fastapi import Depends,HTTPException,Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader,FileSystemLoader
from app.auth import get_current_principal,is_admin_role
from app.v2.navigation import NavigationChildDef,NavigationSectionDef,build_navigation
from app.v2.workspaces import Destination

ROOT=Path(__file__).resolve().parents[2]
ASSETS=ROOT/"static/v2/flavor-explorer"
ADMIN_PATH="/v2/flavor-intelligence"
STAFF_PATH="/v2/flavor-suggestions"
ELIQUID_PATH="/v2/product-explorer/eliquid"
COMPATIBILITY_PATH="/v2/compatibility-explorer"

def register_product_permissions():
    from app.models import PrincipalRole
    from app.services import access_control_service as access
    definitions=(
        ("nav.product_explorer.eliquid","E-liquid Explorer Navigation","Shows E-liquid Explorer when an existing store or touchscreen capability allows access."),
        ("compatibility.view","View Compatibility Explorer","Search verified device, coil and pod compatibility."),
        ("compatibility.suggest","Suggest Compatibility Changes","Submit unpublished additions and corrections."),
        ("compatibility.check_stock","Check Compatibility Product Stock","Read current verified product stock across the four stores."),
        ("compatibility.review","Review Compatibility Suggestions","Approve, modify and publish compatibility suggestions company-wide."),
    )
    known={row.key for row in access.PERMISSIONS}
    access.PERMISSIONS+=tuple(access.PermissionDef(*row) for row in definitions if row[0] not in known)
    everyone={PrincipalRole.ADMIN,PrincipalRole.MANAGER,PrincipalRole.LEAD,PrincipalRole.STORE}
    for key,*_ in definitions:
        access.FALLBACK_ROLE_SET_BY_PERMISSION[key]=everyone-{PrincipalRole.STORE} if key=="compatibility.review" else set(everyone)

def eliquid_destination(principal,flags):
    return ADMIN_PATH if is_admin_role(principal.role) and all(flags.get(key,False) for key in
        ("touchscreen.view","touchscreen.manage_flavors","touchscreen.publish")) else STAFF_PATH

def product_navigation(request):
    navigation=build_navigation(request)
    if getattr(request.app.state,"flavor_preview_auth",False):
        navigation=[section for section in navigation if section.key in {"product_explorer","primary-product-explorer"}]
    # Both management and employee E-liquid pages mark the same single child.
    if request.url.path.startswith((ADMIN_PATH,STAFF_PATH)):
        from dataclasses import replace
        navigation=[replace(section,active=True,expanded=True,
            children=tuple(replace(child,active=child.href==ELIQUID_PATH) for child in section.children))
            if section.label=="Product Explorer" else section for section in navigation]
    return navigation

def install_portal(app):
    if getattr(app.state,"flavor_portal_installed",False):return
    register_product_permissions()
    templates=getattr(app.state,"templates",None)
    if templates is None:
        templates=Jinja2Templates(directory=str(ROOT/"templates"));app.state.templates=templates
    templates.env.loader=ChoiceLoader([FileSystemLoader(str(ASSETS)),templates.env.loader])
    globals_=templates.env.globals
    globals_.setdefault("asset_version","flavor-preview")
    globals_.setdefault("csrf_token",lambda request:getattr(request.state,"csrf_token",""))
    globals_.setdefault("purchasing_v3_enabled",lambda:False)
    globals_.setdefault("primary_navigation",build_navigation)
    globals_.setdefault("portal_home",lambda request:
        eliquid_destination(request.state.principal,getattr(request.state,"permission_flags",{}))
        if getattr(request.state,"principal",None) else "/login")
    globals_.setdefault("format_portal_datetime",lambda value:value.isoformat() if value else "")
    import app.v2.navigation as navigation
    navigation.NAVIGATION_REGISTRY=tuple(section for section in navigation.NAVIGATION_REGISTRY
        if section.key not in {"flavor_intelligence","product_explorer"})+(NavigationSectionDef(
        key="product_explorer",label="Product Explorer",order=63,all_children_permission=None,
        active_prefixes=(ADMIN_PATH,STAFF_PATH,ELIQUID_PATH,COMPATIBILITY_PATH),
        children=(
            NavigationChildDef(key="product_explorer.eliquid",label="E-liquid Explorer",order=10,
                permission="nav.product_explorer.eliquid",route_path=ELIQUID_PATH,active_prefix=ELIQUID_PATH,
                any_permissions=("store.access","touchscreen.view")),
            NavigationChildDef(key="product_explorer.compatibility",label="Compatibility Explorer",order=20,
                permission="compatibility.view",route_path=COMPATIBILITY_PATH,active_prefix=COMPATIBILITY_PATH),
        )),)
    import app.v2.workspaces as workspaces
    removed={ADMIN_PATH,STAFF_PATH,ELIQUID_PATH,COMPATIBILITY_PATH}
    workspaces.DESTINATIONS=tuple(destination for destination in workspaces.DESTINATIONS
        if destination.path not in removed and destination.group!="Flavor Intelligence")+(
        Destination("Product Explorer","E-liquid Explorer",ELIQUID_PATH,"",
            roles=("ADMIN","MANAGER","LEAD","STORE"),any_permissions=("store.access","touchscreen.view")),
        Destination("Product Explorer","Compatibility Explorer",COMPATIBILITY_PATH,"compatibility.view",
            roles=("ADMIN","MANAGER","LEAD","STORE")),
    )
    @app.get(ELIQUID_PATH,include_in_schema=False)
    def eliquid(request:Request,principal=Depends(get_current_principal)):
        # The presentation landing does not grant administrator capabilities.
        flags=getattr(request.state,"permission_flags",{}) or {}
        if not any(flags.get(key,False) for key in ("store.access","touchscreen.view")):raise HTTPException(403)
        return RedirectResponse(eliquid_destination(principal,flags),status_code=303)
    app.state.flavor_portal_installed=True

def render_portal(request,principal,staff=False):
    if not principal or not principal.active:raise HTTPException(401)
    if not staff and not is_admin_role(principal.role):raise HTTPException(403)
    install_portal(request.app)
    development=bool(getattr(request.app.state,"flavor_preview_auth",False))
    page=SimpleNamespace(slug="flavor-suggestions" if staff else "flavor-intelligence",
        label="Flavor suggestions" if staff else "Flavor Intelligence",
        description="Suggest a correction for administrator review." if staff else
                    "Classify products, review staff suggestions and restore history.",
        badge="Development fixtures" if development else "Flavor Intelligence")
    return request.app.state.templates.TemplateResponse(request=request,name="staff.html" if staff else "admin.html",
        context=dict(request=request,principal=principal,page=page,navigation=product_navigation(request),
            stores=[],selected_store_ids=[],all_stores_selected=True,
            store_scope_label="Assigned store" if staff else "Organization-wide",
            scope_locked=True,hide_preview_banner=True,development_fixture=development,
            fixture_notice="Development preview · Synthetic staff identities. No real employee actions." if development else ""))
