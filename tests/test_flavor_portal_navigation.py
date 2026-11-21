"""Explorer presentation must preserve the existing portal entry policy."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from starlette.requests import Request
from app.auth import Principal,Role
from app.config import settings
from app.services.flavor_explorer.portal import (
    install_portal,product_navigation,ELIQUID_PATH,COMPATIBILITY_PATH,
)
from app.v2.entry import portal_home_destination,landing_destination,has_operational_workspace
from app.v2.workspaces import authorized_return,visible_destinations


def request_for(app,flags,*,linked=False,path="/v2/store-operations",active=True):
    request=Request({"type":"http","app":app,"method":"GET","scheme":"http","server":("testserver",80),
        "path":path,"root_path":"","query_string":b"","headers":[]})
    request.state.principal=Principal(999,"Synthetic navigation staff",Role.STORE,1,active)
    request.state.employee_id=1 if linked else None
    request.state.permission_flags=flags
    request.state.v2_primary=True
    return request


@pytest.mark.parametrize("store,linked,home,landing",[
    (True,False,"/v2/store-operations","/v2/store-operations"),
    (False,False,"/v2/access-help","/v2/access-help"),
    (True,True,"/v2/store-operations","/v2/scheduling/my-schedule"),
])
def test_explorer_presentation_preserves_store_empty_access_and_linked_employee_entry(monkeypatch,store,linked,home,landing):
    import app.v2.workspaces as workspaces
    monkeypatch.setattr(settings,"v2_enabled_features","v2_primary,staff_scheduling_v2")
    monkeypatch.setattr(settings,"v2_principal_features","")
    app=FastAPI()
    flags={"store.access":store,"scheduling.view_own":linked,"nav.product_explorer.eliquid":True,
        "compatibility.view":True}
    request=request_for(app,flags,linked=linked)
    destinations=workspaces.DESTINATIONS
    before=visible_destinations(request)
    install_portal(app)
    assert workspaces.DESTINATIONS is destinations
    assert visible_destinations(request)==before
    assert not has_operational_workspace(request)
    assert portal_home_destination(request)==home
    assert landing_destination(request,source="login")==landing
    assert landing_destination(request,source="v2",return_to="/v2/overview")==landing
    assert authorized_return(request,ELIQUID_PATH) is None
    assert authorized_return(request,COMPATIBILITY_PATH) is None
    section=next(section for section in product_navigation(request) if section.key=="product_explorer")
    assert [child.href for child in section.children]==([ELIQUID_PATH,COMPATIBILITY_PATH] if store else [COMPATIBILITY_PATH])


@pytest.mark.parametrize("primary",[False,True])
def test_template_context_adds_one_product_section_in_both_shells_without_changing_home(monkeypatch,primary):
    monkeypatch.setattr(settings,"v2_enabled_features","v2_primary" if primary else "")
    monkeypatch.setattr(settings,"v2_principal_features","")
    app=FastAPI();install_portal(app);install_portal(app)
    request=request_for(app,{"store.access":True,"nav.product_explorer.eliquid":True,"compatibility.view":True})
    request.state.v2_primary=primary
    response=app.state.templates.TemplateResponse(request=request,name="v2/base.html",context={
        "principal":request.state.principal,"page":SimpleNamespace(label="Synthetic page",slug="synthetic",description=""),
        "navigation":[],"stores":[],"selected_store_ids":[],"scope_locked":True,"store_scope_label":"Synthetic store",
    })
    assert len(app.state.templates.context_processors)==1
    sections=[section for section in response.context["navigation"] if section.label=="Product Explorer"]
    assert len(sections)==1
    assert [child.href for child in sections[0].children]==[ELIQUID_PATH,COMPATIBILITY_PATH]
    assert response.body.count(b'data-nav-key="product_explorer"')==1
    assert portal_home_destination(request)==("/v2/store-operations" if primary else "/store/home")


def test_explicit_compatibility_deny_preserves_independent_eliquid_navigation(monkeypatch):
    monkeypatch.setattr(settings,"v2_enabled_features","v2_primary")
    monkeypatch.setattr(settings,"v2_principal_features","")
    app=FastAPI();install_portal(app)
    request=request_for(app,{"store.access":True,"nav.product_explorer.eliquid":True,"compatibility.view":False},
        path="/v2/flavor-suggestions")
    section=next(section for section in product_navigation(request) if section.key=="product_explorer")
    assert [(child.href,child.active) for child in section.children]==[(ELIQUID_PATH,True)]
    assert section.active and section.expanded


def test_inactive_principal_never_receives_product_navigation(monkeypatch):
    monkeypatch.setattr(settings,"v2_enabled_features","v2_primary")
    monkeypatch.setattr(settings,"v2_principal_features","")
    app=FastAPI();install_portal(app)
    request=request_for(app,{"store.access":True,"nav.product_explorer.eliquid":True,"compatibility.view":True},active=False)
    assert all(section.label!="Product Explorer" for section in product_navigation(request))
