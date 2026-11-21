"""Staff production transport and exact legacy-photo fallback regressions."""
from io import BytesIO
from types import SimpleNamespace
import hashlib,json
from datetime import datetime,timezone
import pytest
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from sqlalchemy import text
from flavor_pg_fixture import explorer_pg_base,explorer_pg_engine,explorer_pg_storage
from test_flavor_production_catalog import synthetic_snapshot,synthetic_identity,observer
from app.auth import Principal,Role
from app.services.flavor_explorer.production_runtime import install_runtime
from app.services.flavor_explorer.production_originals import ExistingOriginals
from app.services.flavor_explorer.product_photos import ProductPhotos
from app.services.flavor_explorer.square_catalog import normalize_catalog

def originals_tables(storage):
    with storage.engine.begin() as c:
        c.exec_driver_sql("""ALTER TABLE touchscreen_flavors ADD is_active BOOLEAN NOT NULL DEFAULT true, ADD deleted_at TIMESTAMPTZ;
            CREATE TABLE touchscreen_flavor_sku_links(id BIGINT PRIMARY KEY,touchscreen_flavor_id BIGINT,
                square_variation_id TEXT,format TEXT,is_active BOOLEAN NOT NULL DEFAULT true);
            CREATE TABLE touchscreen_flavor_media(id BIGINT PRIMARY KEY,touchscreen_flavor_id BIGINT,media_asset_id BIGINT,role TEXT);
            CREATE TABLE digital_signage_media_assets(id BIGINT PRIMARY KEY,storage_key TEXT,content_type TEXT,
                size_bytes BIGINT,content_hash TEXT,width INT,height INT,media_type TEXT,archived_at TIMESTAMPTZ);""")

def png(color):
    from PIL import Image
    output=BytesIO();Image.new("RGB",(12,16),color).save(output,format="PNG");return output.getvalue()

def add_original(storage,identity,variation_ids,data):
    with storage.engine.begin() as c:
        c.execute(text("INSERT INTO digital_signage_media_assets VALUES(:id,:key,'image/png',:size,:sha,12,16,'IMAGE',NULL)"),
            {"id":identity,"key":"fixture/"+str(identity),"size":len(data),"sha":hashlib.sha256(data).hexdigest()})
        c.execute(text("INSERT INTO touchscreen_flavor_media VALUES(:id,77,:id,'PRIMARY')"),{"id":identity})
        for i,vid in enumerate(variation_ids):
            c.execute(text("INSERT INTO touchscreen_flavor_sku_links VALUES(:id,77,:vid,'FREEBASE',true)"),
                {"id":identity*100+i,"vid":vid})

class Observed:
    def __init__(self,snapshot):self.value=snapshot
    def snapshot(self):return self.value
    def identity(self):return synthetic_identity()
    def stock(self,ids):return {}
    def start(self):raise AssertionError("Unit fixture must not start a production worker")
    def stop(self):pass

def test_original_exact_identity_integrity_and_in_place_replacement(explorer_pg_storage):
    storage=explorer_pg_storage;originals_tables(storage);data=png("blue")
    add_original(storage,1,["V1","V2"],data)
    objects=SimpleNamespace(get=lambda key:data)
    originals=ExistingOriginals(storage.engine,objects)
    products=[{"id":"one","type":"freebase","squareVariationIds":["V1","V2"]},
        {"id":"missing","type":"freebase","squareVariationIds":["V1","V3"]},
        {"id":"wrong-format","type":"salt","squareVariationIds":["V1","V2"]}]
    originals.attach(products)
    assert products[0]["originalImageUrl"]=="/flavor-explorer/media/originals/1"
    assert all("originalImageUrl" not in row for row in products[1:])
    before=originals.version()
    assert originals.asset(1)["data"]==data
    with storage.engine.begin() as c:
        c.execute(text("INSERT INTO digital_signage_media_assets SELECT 2,'fixture/2',content_type,size_bytes,content_hash,width,height,media_type,NULL FROM digital_signage_media_assets WHERE id=1"))
        c.execute(text("UPDATE touchscreen_flavor_media SET media_asset_id=2 WHERE id=1"))
    assert originals.version()!=before
    originals.attach(products);assert products[0]["originalImageUrl"].endswith("/2")
    with pytest.raises(LookupError):originals.asset(1)
    bad=ExistingOriginals(storage.engine,SimpleNamespace(get=lambda key:b"corrupt"))
    with pytest.raises(ValueError):bad.asset(2)
    with storage.engine.begin() as c:c.execute(text("UPDATE touchscreen_flavor_sku_links SET is_active=false WHERE square_variation_id='V2'"))
    originals.attach(products);assert "originalImageUrl" not in products[0]

def test_cross_worker_projection_observes_photo_events_and_keeps_own_fallback(explorer_pg_storage):
    storage=explorer_pg_storage;originals_tables(storage)
    snapshot=synthetic_snapshot()
    with storage._write() as c:
        observations=storage.table("fi_catalog_observations")
        c.execute(observations.update().where(observations.c.id==1).values(snapshot=snapshot,generation=1))
    app1=FastAPI();app2=FastAPI()
    runtime1=install_runtime(app1,storage.engine,catalog=Observed(snapshot),start_worker=False)
    runtime2=install_runtime(app2,storage.engine,catalog=Observed(snapshot),start_worker=False)
    product=normalize_catalog(snapshot)["products"][0];photos=ProductPhotos(storage)
    before=runtime2.projection.get()[2]
    primary=photos.apply(actor_id=1,product=product,expected_token=photos.describe(product)["stateToken"],
        mode="custom",data=png("red"),source_kind="manufacturer")
    after=runtime2.projection.get()
    assert after[2]!=before
    assert json.loads(after[0])["products"][0]["imageUrl"]==primary["url"]
    own=photos.apply(actor_id=1,product=product,expected_token=primary["stateToken"],
        mode="custom",data=png("blue"),source_kind="own")
    assert own["url"]==primary["url"] and own["candidates"]["own"]
    assert runtime1.projection.get()[2]==runtime2.projection.get()[2]
    removed=photos.apply(actor_id=1,product=product,expected_token=own["stateToken"],
        action="remove_candidate",source_kind="manufacturer")
    assert removed["effectiveSourceKind"]=="own"
    assert json.loads(runtime2.projection.get()[0])["products"][0]["imageUrl"]==removed["url"]

def test_staff_documents_and_photos_require_active_view_permission_and_reserved_documents_are_denied(explorer_pg_storage):
    from app.routers.flavor_explorer import router,ProductStaticFiles,ROOT
    from app.services.flavor_explorer.photo_media import router as photos_router
    from app.services.flavor_explorer.portal_api import admin_router,employee_router
    storage=explorer_pg_storage;originals_tables(storage);app=FastAPI()
    install_runtime(app,storage.engine,catalog=Observed(synthetic_snapshot()),start_worker=False)
    app.include_router(router);app.include_router(photos_router);app.include_router(admin_router);app.include_router(employee_router)
    app.mount("/v2-assets",ProductStaticFiles(directory=str(ROOT.parent)))
    @app.middleware("http")
    async def explicit_test_principal(request:Request,call_next):
        # Test fixture only: no production sessions, cookie factories or login routes.
        who=request.headers.get("X-Explicit-Test-Principal")
        if who:
            request.state.principal=Principal(1,"Synthetic admin",Role.ADMIN,None,who!="inactive")
            if who!="missing-flags":
                request.state.permission_flags={"store.access":who=="active","touchscreen.view":who=="view-only"}
        return await call_next(request)
    client=TestClient(app)
    assert client.get("/explorer/").status_code==401
    assert client.get("/explorer/api/catalog").status_code==401
    assert client.get("/flavor-explorer/media/photos/"+"a"*64+".webp").status_code==401
    assert client.get("/flavor-explorer/media/originals/1").status_code==401
    assert client.get("/explorer/",headers={"X-Explicit-Test-Principal":"inactive"}).status_code==403
    protected_paths=("/explorer/","/explorer/api/catalog",
        "/flavor-explorer/media/photos/"+"a"*64+".webp","/flavor-explorer/media/originals/1",
        "/v2/flavor-intelligence/api/products","/v2/flavor-suggestions")
    for who in ("denied","missing-flags"):
        for path in protected_paths:
            assert client.get(path,headers={"X-Explicit-Test-Principal":who}).status_code==403,(who,path)
    assert client.get("/explorer/api/catalog",headers={"X-Explicit-Test-Principal":"view-only"}).status_code==200
    headers={"X-Explicit-Test-Principal":"active"}
    assert client.get("/explorer/",headers=headers).status_code==200
    assert client.get("/explorer/api/catalog",headers=headers).status_code==200
    for name in ("admin.html","staff.html","compatibility.html","compatibility-request.html","owner-preview.html"):
        assert client.get("/explorer/"+name,headers=headers).status_code==404
        assert client.get("/v2-assets/flavor-explorer/"+name,headers=headers).status_code==404
        assert client.get("/v2-assets/Flavor-Explorer/"+name,headers=headers).status_code==404
    assert client.post("/v2/flavor-intelligence/api/products/unknown/photo",json={},headers=headers).status_code==403

def test_internal_release_bundle_is_absent_from_public_source_and_untrusted_inputs_fail(tmp_path):
    from pathlib import Path
    from app.services.flavor_explorer.production_import import bundle,MANIFEST_SHA
    assert len(MANIFEST_SHA)==64
    assert not Path("app/services/flavor_explorer/release_assets").exists()
    with pytest.raises(ValueError,match="trusted private release bundle"):
        bundle(tmp_path/"missing")
    (tmp_path/"manifest.json").write_text('{"version":1,"photos":[]}')
    with pytest.raises(ValueError,match="fixed reviewed import manifest changed"):
        bundle(tmp_path)


def test_http_photo_adapter_requires_source_and_removes_only_selected_candidate(observer):
    import base64
    from app.services.flavor_explorer.portal_api import PortalService
    storage,catalog,provider=observer
    assert catalog.refresh_once(True)
    service=PortalService(storage,catalog.snapshot)
    principal=Principal(1,"Synthetic admin",Role.ADMIN,None,True)
    product=service.catalog(principal)[0][0]
    before=service.product_photo(principal,product["id"])["photo"]
    upload={"mode":"custom","dataBase64":base64.b64encode(png("red")).decode(),"expectedToken":before["stateToken"]}
    with pytest.raises(ValueError,match="Choose Manufacturer"):
        service.change_product_photo(principal,product["id"],upload)
    maker=service.change_product_photo(principal,product["id"],dict(upload,sourceKind="manufacturer"))["photo"]
    own=service.change_product_photo(principal,product["id"],{"mode":"custom","dataBase64":base64.b64encode(png("blue")).decode(),
        "sourceKind":"own","expectedToken":maker["stateToken"]})["photo"]
    assert own["url"]==maker["url"] and own["effectiveSourceKind"]=="manufacturer"
    changed=service.change_product_photo(principal,product["id"],{"action":"remove_candidate","sourceKind":"manufacturer",
        "expectedToken":own["stateToken"]})["photo"]
    assert changed["effectiveSourceKind"]=="own" and changed["candidates"]["manufacturer"] is None
    assert changed["url"]==own["candidates"]["own"]["url"]

def test_original_storage_outage_is_a_private_service_unavailable_response():
    from app.routers.flavor_explorer import router
    from app.services.digital_signage_storage import StorageUnavailable
    app=FastAPI()
    def unavailable(asset_id):raise StorageUnavailable("Synthetic unavailable configured storage")
    app.state.product_explorer=SimpleNamespace(originals=SimpleNamespace(asset=unavailable))
    app.include_router(router)
    @app.middleware("http")
    async def authenticated_fixture(request,call_next):
        request.state.principal=Principal(1,"Synthetic admin",Role.ADMIN,None,True)
        request.state.permission_flags={"store.access":True,"touchscreen.view":False}
        return await call_next(request)
    result=TestClient(app).get("/flavor-explorer/media/originals/1")
    assert result.status_code==503
    assert "Synthetic unavailable" not in result.text
