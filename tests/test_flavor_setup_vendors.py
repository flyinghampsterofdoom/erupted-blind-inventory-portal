"""Setup-only exact supplier grouping and payload confidentiality."""
from copy import deepcopy

import pytest
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from sqlalchemy import event,text
from sqlalchemy.orm import Session

from flavor_pg_fixture import explorer_pg_base,explorer_pg_engine,explorer_pg_storage
from test_flavor_production_catalog import observer
from test_flavor_production_runtime import originals_tables
from app.auth import Principal,Role
from app.services.flavor_explorer.portal_api import PortalService
from app.services.flavor_explorer.setup_vendors import SetupVendorLoader


def vendor_tables(engine):
    # No financial/contact columns: the adapter must select only Setup fields.
    with engine.begin() as c:
        c.exec_driver_sql("""CREATE TABLE vendors(id BIGINT PRIMARY KEY,name TEXT NOT NULL,active BOOLEAN NOT NULL);
            CREATE TABLE vendor_sku_configs(id BIGINT PRIMARY KEY,vendor_id BIGINT REFERENCES vendors(id),
                sku TEXT,square_variation_id TEXT,active BOOLEAN NOT NULL);
            INSERT INTO vendors VALUES(10,'Private Setup Supplier',true),(11,'Private Setup Supplier',false),(12,'Historical Supplier',true);
            INSERT INTO vendor_sku_configs VALUES
                (1,10,'SHARED','A3',true),(2,10,'SECOND','A3',false),
                (3,11,'SHARED','A3',true),(4,12,'OLD','A6',false),
                (5,10,'UNRELATED','OTHER',true),(6,10,'MATCHING-SKU',NULL,true);""")


def test_loader_reads_one_exact_batch_and_preserves_multiple_and_retired_mappings(explorer_pg_engine):
    engine=explorer_pg_engine;vendor_tables(engine);queries=[]
    def observed(_c,_cursor,statement,*_):queries.append(statement)
    event.listen(engine,"before_cursor_execute",observed)
    try:
        result=SetupVendorLoader(engine)(["A3","A6","UNMAPPED","A3"])
    finally:event.remove(engine,"before_cursor_execute",observed)
    assert len(queries)==1 and queries[0].lstrip().upper().startswith("SELECT")
    assert set(result)=={"A3","A6"}
    assert result["A3"]==[
        {"id":10,"name":"Private Setup Supplier","active":True,"historical":False},
        {"id":11,"name":"Private Setup Supplier","active":False,"historical":False}]
    assert result["A6"]==[{"id":12,"name":"Historical Supplier","active":True,"historical":True}]
    assert SetupVendorLoader(engine)([])=={}
    # Mapping edits are reflected without a process-wide cache or catalog refresh.
    with engine.begin() as c:c.exec_driver_sql("UPDATE vendor_sku_configs SET active=false WHERE id=1")
    assert SetupVendorLoader(engine)(["A3"])["A3"][0]["historical"] is True


def setup_service():
    def row(identity,variations,name,kind="freebase",visibility="included"):
        return {"id":identity,"brand":"Synthetic","name":name,"type":kind,"strengths":[3],
            "squareVariationIds":variations,"imageUrl":None,"imageSource":"illustration-fallback",
            "photoMode":"catalog","sizeLabel":"60ml","explorerVisibility":visibility,
            "excludedVariationCount":0,"variationCount":len(variations),"visibilityVersion":"fixture"}
    rows=[row("mixed",["A3","A6","UNKNOWN"],"Raspberry"),
          row("other",["B3"],"Peach","salt","excluded"),row("unmapped",["NONE"],"Mango")]
    mappings={"A3":[{"id":10,"name":"Same Name","active":True,"historical":False},
                    {"id":11,"name":"Same Name","active":False,"historical":False}],
              "A6":[{"id":10,"name":"Same Name","active":True,"historical":True}],
              "B3":[{"id":12,"name":"Old Supplier","active":True,"historical":True}]}
    calls=[]
    service=PortalService.__new__(PortalService)
    service.catalog=lambda principal:(deepcopy(rows),{})
    def loader(identities):
        calls.append(identities);return deepcopy(mappings)
    service.setup_vendor_loader=loader
    return service,rows,calls


def test_setup_filters_union_vendors_and_any_unmapped_variant_with_stable_options():
    service,rows,calls=setup_service()
    all_products=service.discovery_catalog(None)
    assert calls==[["A3","A6","B3","NONE","UNKNOWN"]]
    mixed=all_products["items"][0]
    assert [v["id"] for v in mixed["vendors"]]==[10,11]
    assert mixed["vendors"][0]["historical"] is False
    assert mixed["vendorUnmappedVariationCount"]==1
    options=all_products["vendors"]
    assert {v["id"] for v in options}=={10,11,12}
    for query in ({"vendor":"10"},{"vendor":"11"},{"q":"Rasp","product_type":"freebase"},
                  {"visibility":"excluded"},{"vendor":"999"},{"q":"absent"}):
        assert service.discovery_catalog(None,**query)["vendors"]==options
    assert [r["id"] for r in service.discovery_products(None,vendor="11")]==["mixed"]
    assert [r["id"] for r in service.discovery_products(None,vendor="unmapped")]==["mixed","unmapped"]
    assert service.discovery_products(None,vendor="999")==[]
    assert service.catalog(None)[0]==rows
    assert all("vendors" not in row for row in rows)


@pytest.mark.parametrize("value",["unknown","-1","0","1 OR true","9223372036854775808",None])
def test_invalid_vendor_filter_is_rejected_before_loading_supplier_data(value):
    service,_rows,calls=setup_service()
    with pytest.raises(ValueError,match="Setup vendor"):
        service.discovery_catalog(None,vendor=value)
    assert calls==[]


def test_vendor_details_exist_only_in_setup_response_and_setup_requires_administrator(observer):
    from app.services.flavor_explorer.production_runtime import install_runtime
    from app.services.flavor_explorer.portal_api import admin_router,get_db
    from app.routers.flavor_explorer import router
    storage,catalog,provider=observer
    assert catalog.refresh_once(True)
    originals_tables(storage);vendor_tables(storage.engine)
    app=FastAPI();runtime=install_runtime(app,storage.engine,catalog=catalog,start_worker=False)
    app.include_router(admin_router);app.include_router(router)
    def database():
        with Session(storage.engine) as db:yield db
    app.dependency_overrides[get_db]=database
    @app.middleware("http")
    async def explicit_test_principal(request:Request,call_next):
        who=request.headers.get("X-Explicit-Test-Principal","admin")
        request.state.principal=Principal(1 if who=="admin" else 2,"Synthetic fixture",
            Role.ADMIN if who=="admin" else Role.STORE,None if who=="admin" else 4,True)
        request.state.permission_flags={"touchscreen.view":True}
        return await call_next(request)
    calls=len(provider.calls);client=TestClient(app)
    setup=client.get("/v2/flavor-intelligence/api/discovery/products?vendor=12")
    assert setup.status_code==200 and setup.headers["Cache-Control"]=="no-store"
    assert setup.json()["count"]==1
    assert next(v for v in setup.json()["items"][0]["vendors"] if v["id"]==12)["historical"] is True
    assert setup.json()["vendors"]
    assert client.get("/v2/flavor-intelligence/api/discovery/products",
        headers={"X-Explicit-Test-Principal":"staff"}).status_code==403
    def no_vendor_fields(value):
        if isinstance(value,dict):
            assert not any(key.startswith("vendor") for key in value)
            for child in value.values():no_vendor_fields(child)
        elif isinstance(value,list):
            for child in value:no_vendor_fields(child)
    product_id=setup.json()["items"][0]["id"]
    for path in ("/v2/flavor-intelligence/api/products",
            "/v2/flavor-intelligence/api/products/"+product_id,
            "/v2/flavor-intelligence/api/products/"+product_id+"/photo",
            "/v2/flavor-intelligence/api/bootstrap","/explorer/api/catalog"):
        response=client.get(path)
        assert response.status_code==200,(path,response.text)
        no_vendor_fields(response.json())
        assert "Private Setup Supplier" not in response.text
    no_vendor_fields(app.state.product_compatibility.bootstrap(Principal(1,"Synthetic admin",Role.ADMIN,None,True)))
    no_vendor_fields(catalog.snapshot())
    assert len(provider.calls)==calls
