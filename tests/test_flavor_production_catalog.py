"""Mock-provider native PG refresh/lease/identity/stock regressions."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import threading

import pytest
from sqlalchemy import select,update,func,text
from flavor_pg_fixture import explorer_pg_base,explorer_pg_engine,explorer_pg_storage
from app.services.flavor_explorer.production_catalog import ProductionCatalog
from app.services.flavor_explorer.square_catalog import STORE_SPECS,normalize_catalog,project_snapshot

def stamp():return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
def synthetic_snapshot():
    observed=stamp()
    stores=[{"id":key,"name":name,"shortName":name,"localStoreId":local,
        "squareLocationId":"LOC"+str(local),"verified":True} for key,name,local,_ in STORE_SPECS]
    return {"schemaVersion":1,"source":"square","catalogObservedAt":observed,"inventoryObservedAt":observed,
        "inventoryTtlSeconds":300,"stores":stores,"categories":{"FREE":"Freebase"},"images":{},"gaps":[],
        "items":[{"id":"ITEM","name":"Juice Head Raspberry","categoryIds":["FREE"],"description":"Synthetic source fixture",
            "observedAt":observed,"variations":[{"id":"A3","squareItemId":"ITEM","name":"3mg","sku":"0000123"},
                                              {"id":"A6","squareItemId":"ITEM","name":"6mg","sku":"0000126"}]}],
        "inventory":[{"locationId":row["squareLocationId"],"variationId":vid,"quantity":"3",
            "calculatedAt":"2020-01-01T00:00:00Z","observedAt":observed} for row in stores for vid in ("A3","A6")],
        "sync":{"mode":"automatic","failures":0}}

def synthetic_identity():
    snapshot=synthetic_snapshot()
    return {"observedAt":snapshot["catalogObservedAt"],"available":True,"stores":snapshot["stores"],
        "variations":[{"id":"A3","itemId":"ITEM","sku":"0000123","upc":"012345678905","itemName":"Juice Head Raspberry","variationName":"3mg"},
                      {"id":"A6","itemId":"ITEM","sku":"0000126","upc":"","itemName":"Juice Head Raspberry","variationName":"6mg"}]}

class Provider:
    def __init__(self):
        self.calls=[];self.fail=False;self.item_id="ITEM";self.negative=False;self.interrupt=None
        self.long_sku="000"+("1234567890"*14)
    def get(self,path):
        self.calls.append(("GET",path))
        if self.fail:raise RuntimeError("Synthetic private source failure; do not persist this text.")
        assert path=="/v2/locations"
        return {"locations":[{"id":"LOC"+str(local),"name":name,"status":"ACTIVE"}
            for _key,name,local,_aliases in STORE_SPECS]}
    def post(self,path,payload):
        self.calls.append(("POST",path))
        if self.fail:raise RuntimeError("Synthetic private source failure; do not persist this text.")
        if path=="/v2/catalog/search":
            return {"objects":[{"id":"FREE","type":"CATEGORY","category_data":{"name":"Freebase"}}]}
        if path=="/v2/catalog/search-catalog-items":
            return {"items":[{"id":self.item_id,"item_data":{"name":"Juice Head Raspberry","categories":[{"id":"FREE"}],
                "variations":[{"id":vid,"item_variation_data":{"name":str(mg)+"mg","sku":self.long_sku if vid=="A3" else "0000126",
                    "upc":"012345678905" if vid=="A3" else "","sellable":True,"track_inventory":True}} for vid,mg in (("A3",3),("A6",6))]}}]}
        assert path=="/v2/inventory/counts/batch-retrieve"
        if self.interrupt:
            action=self.interrupt;self.interrupt=None;action()
        return {"counts":[{"catalog_object_id":"A3","location_id":"LOC4","state":"IN_STOCK",
            "quantity":"-1" if self.negative else "3","calculated_at":"2020-01-01T00:00:00Z"},
            {"catalog_object_id":"A3","location_id":"LOC6","state":"IN_STOCK","quantity":"0","calculated_at":"2020-01-01T00:00:00Z"}]}

@pytest.fixture
def observer(explorer_pg_storage):
    storage=explorer_pg_storage
    with storage._write() as c:
        c.execute(text("UPDATE stores SET name='Highway 99',square_location_id='LOC4' WHERE id=4"))
        c.execute(text("UPDATE stores SET name='SR503',square_location_id='LOC9' WHERE id=9"))
        c.execute(text("INSERT INTO stores(id,name,square_location_id,active) VALUES(6,'Longview','LOC6',true),(2,'Andresen','LOC2',true)"))
    provider=Provider()
    return storage,ProductionCatalog(storage,provider.get,provider.post),provider

def observation(storage):
    with storage.engine.connect() as c:return dict(c.execute(select(storage.table("fi_catalog_observations"))).mappings().one())

def test_durable_refresh_mirrors_whole_card_and_search_reads_never_poll_inventory(observer):
    storage,catalog,provider=observer
    assert catalog.refresh_once(force=True) is True
    result=catalog.last_result
    assert result["status"]=="refreshed" and result["catalogRefreshed"] and result["identityRefreshed"]
    snapshot=catalog.snapshot();identity=catalog.identity()
    assert identity["available"] and identity["variations"][0]["sku"]==provider.long_sku
    products=normalize_catalog(snapshot)["products"]
    with storage.engine.connect() as c:
        links=c.execute(select(storage.table("fi_square_flavor_links"))).mappings().all()
        assert len(links)==2 and len({row["flavor_id"] for row in links})==1
        assert c.scalar(select(func.count()).select_from(storage.table("touchscreen_flavors")))==1
        assert c.scalar(text("SELECT count(*) FROM touchscreen_flavors"))==1
    calls=len(provider.calls)
    second=ProductionCatalog(storage,provider.get,provider.post)
    assert second.snapshot()==snapshot and second.identity()["available"]
    assert second.refresh_once() is False and second.last_result["status"]=="idle"
    assert len(provider.calls)==calls and len(products)==1

def test_failed_reads_preserve_last_good_times_counts_identity_and_sanitized_error(observer):
    storage,catalog,provider=observer;assert catalog.refresh_once(True)
    before=observation(storage);provider.fail=True
    assert catalog.refresh_once(True) is False
    after=observation(storage)
    assert after["snapshot"]["inventory"]==before["snapshot"]["inventory"]
    assert after["snapshot"]["inventoryObservedAt"]==before["snapshot"]["inventoryObservedAt"]
    assert after["identity"]==before["identity"] and after["last_success_at"]==before["last_success_at"]
    assert after["last_error"]=="RuntimeError"
    assert "private source" not in str(after)
    calls=len(provider.calls)
    assert catalog.refresh_once() is False and len(provider.calls)==calls
    stock=catalog.stock(["A3"])
    assert len(stock["stores"])==4 and all(row["state"]=="unknown" and row["quantity"] is None for row in stock["stores"])
    assert observation(storage)==after

def test_cross_worker_lease_blocks_reads_then_expired_owner_is_recovered(observer):
    storage,catalog,provider=observer
    lease=storage.table("fi_refresh_lease")
    with storage._write() as c:
        c.execute(update(lease).where(lease.c.id==1).values(owner="other-worker",
            expires_at=datetime.now(timezone.utc)+timedelta(minutes=1)))
    assert catalog.refresh_once(True) is False and catalog.last_result["status"]=="busy"
    assert not provider.calls
    with storage._write() as c:c.execute(update(lease).where(lease.c.id==1).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert catalog.refresh_once(True)
    with storage.engine.connect() as c:assert c.scalar(select(lease.c.owner)) is None

def test_stock_is_explicit_unknown_safe_package_separate_and_never_persists(observer):
    storage,catalog,provider=observer;assert catalog.refresh_once(True)
    before=observation(storage);provider.calls.clear()
    stock=catalog.stock(["A3","A6"])
    assert all(path=="/v2/inventory/counts/batch-retrieve" for _method,path in provider.calls)
    assert stock["observedAt"] and all(store["quantity"] is None for store in stock["stores"])
    highway=next(store for store in stock["stores"] if store["id"]=="hwy99")
    assert highway["state"]=="in_stock"
    assert {row["id"]:row["state"] for row in highway["variations"]}=={"A3":"in_stock","A6":"unknown"}
    provider.negative=True
    assert next(row for row in catalog.stock(["A3"])["stores"] if row["id"]=="hwy99")["state"]=="unknown"
    assert observation(storage)==before
    old=deepcopy(before["identity"]);old["observedAt"]=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    with storage._write() as c:c.execute(update(storage.table("fi_catalog_observations")).values(identity=old))
    provider.calls.clear()
    assert all(row["state"]=="unknown" for row in catalog.stock(["A3"])["stores"]) and not provider.calls

def test_reassigned_square_parent_aborts_observation_and_identity_mirror_atomically(observer):
    storage,catalog,provider=observer;assert catalog.refresh_once(True)
    before=observation(storage);provider.item_id="WRONG-PARENT"
    assert catalog.refresh_once(True) is False
    after=observation(storage)
    assert after["snapshot"]==before["snapshot"] and after["identity"]==before["identity"]
    with storage.engine.connect() as c:
        assert set(c.scalars(select(storage.table("fi_square_flavor_links").c.square_item_id)))=={"ITEM"}
        assert c.scalar(select(func.count()).select_from(storage.table("touchscreen_flavors")))==1
    assert after["last_error"]=="ValueError"

def test_generation_change_mid_read_prevents_old_worker_from_overwriting_new_facts(observer):
    storage,catalog,provider=observer;assert catalog.refresh_once(True)
    before=observation(storage)
    def publish_newer_generation():
        with storage._write() as c:
            table=storage.table("fi_catalog_observations")
            c.execute(update(table).where(table.c.id==1).values(generation=table.c.generation+1,last_error="newer-worker-marker"))
    provider.interrupt=publish_newer_generation
    assert catalog.refresh_once(True) is False
    after=observation(storage)
    assert after["generation"]==before["generation"]+1 and after["last_error"]=="newer-worker-marker"
    assert after["snapshot"]==before["snapshot"]

def test_worker_start_is_idempotent_and_stop_wakes_promptly(observer,monkeypatch):
    _storage,catalog,_provider=observer;called=threading.Event()
    monkeypatch.setattr(catalog,"refresh_once",lambda:called.set())
    catalog.start();thread=catalog._thread;catalog.start()
    assert catalog._thread is thread and called.wait(1)
    catalog.stop();assert not thread.is_alive()


@pytest.mark.parametrize("across_pages",[False,True])
def test_duplicate_requested_pairs_are_unknown_before_gateway_dedup(observer,across_pages):
    storage,catalog,_provider=observer;assert catalog.refresh_once(True)
    before=observation(storage);calls=[]
    first={"catalog_object_id":"A3","location_id":"LOC4","state":"IN_STOCK","quantity":"3",
           "calculated_at":"2020-01-01T00:00:00Z"}
    conflicting={**first,"quantity":"0"}
    unaffected={**first,"location_id":"LOC6","quantity":"2"}
    def read(path,payload):
        assert path=="/v2/inventory/counts/batch-retrieve"
        calls.append(deepcopy(payload))
        if across_pages:
            if payload.get("cursor"):return {"counts":[conflicting]}
            return {"counts":[first,unaffected],"cursor":"NEXT-PAGE"}
        return {"counts":[first,conflicting,unaffected]}
    catalog.post=read
    result=catalog.stock(["A3"])
    highway=next(row for row in result["stores"] if row["id"]=="hwy99")
    assert highway["state"]=="unknown" and highway["quantity"] is None
    assert highway["variations"][0]["reason"]=="duplicate_count"
    assert highway["variations"][0]["calculatedAt"] is None
    longview=next(row for row in result["stores"] if row["id"]=="longview")
    assert longview["state"]=="in_stock" and longview["quantity"]=="2"
    assert len(calls)==(2 if across_pages else 1)
    assert observation(storage)==before


def test_disabled_store_invalidates_cached_runtime_projection_immediately(observer):
    from types import SimpleNamespace
    from app.services.flavor_explorer.production_runtime import ProductExplorerRuntime
    storage,catalog,_provider=observer;assert catalog.refresh_once(True)
    originals=SimpleNamespace(attach=lambda products:products,version=lambda:"synthetic-no-originals")
    runtime=ProductExplorerRuntime(storage,catalog,originals)
    before=runtime.projection.get()[2]
    assert before
    with storage._write() as c:
        stores=storage.table("stores")
        c.execute(update(stores).where(stores.c.id==4).values(active=False))
    with pytest.raises(ValueError,match="active store"):
        runtime.projection.get()


@pytest.mark.parametrize("across_pages",[False,True])
def test_background_duplicates_are_omitted_and_project_unknown_not_zero(observer,across_pages):
    _storage,catalog,provider=observer
    original_post=catalog.post
    first={"catalog_object_id":"A3","location_id":"LOC4","state":"IN_STOCK","quantity":"3",
           "calculated_at":"2020-01-01T00:00:00Z"}
    conflicting={**first,"quantity":"0"}
    explicit_zero={**first,"location_id":"LOC6","quantity":"0"}
    def read(path,payload):
        if path!="/v2/inventory/counts/batch-retrieve":return original_post(path,payload)
        if across_pages:
            if payload.get("cursor"):return {"counts":[conflicting]}
            return {"counts":[first,explicit_zero],"cursor":"BACKGROUND-NEXT"}
        return {"counts":[first,conflicting,explicit_zero]}
    catalog.post=read
    assert catalog.refresh_once(force=True)
    snapshot=catalog.snapshot()
    assert {(row["locationId"],row["variationId"]) for row in snapshot["inventory"]}=={("LOC6","A3")}
    assert snapshot["inventory"][0]["quantity"]=="0"
    assert snapshot["inventoryCoverage"]=={"requestedPairs":8,"observedPairs":1,"missingPairs":7,"duplicatePairs":1}
    assert any(gap["code"]=="duplicate_inventory_count" for gap in snapshot["gaps"])
    projected=project_snapshot(snapshot,ttl_seconds=300)
    variation=next(v for p in projected["products"] for v in p["variations"] if v["id"]=="A3")
    assert variation["availability"]["hwy99"]["state"]=="unknown"
    assert variation["availability"]["longview"]["state"]=="out_of_stock"
    observed=snapshot["inventoryObservedAt"]
    provider.fail=True
    assert catalog.refresh_once(force=True) is False
    retained=catalog.snapshot()
    assert retained["inventory"]==snapshot["inventory"] and retained["inventoryObservedAt"]==observed


def test_category_spelling_mint_ice_and_brand_alias_regressions():
    from app.services.flavor_explorer.square_catalog import _flavor_profile, _brand
    for name in ("Red's 7obacco", "Red’s Tobacco", "Classic Tobacco"):
        notes, categories, *_ = _flavor_profile(name)
        assert "tobacco" in notes and "tobacco" in categories
    for name in ("Glacial Mint", "Crisp Menthol", "Peppermint", "Spearmint", "Wintergreen"):
        _, categories, sensory, *_ = _flavor_profile(name)
        assert "mint" in categories and "ice" not in categories and sensory["cooling"] >= 4
    for name in ("Watermelon Ice", "Blueberry Ice", "Strawberry Freeze", "Mango Ice"):
        _, categories, *_ = _flavor_profile(name)
        assert "fruit" in categories and "ice" in categories and "mint" not in categories
    assert set(_flavor_profile("Mint Ice")[1]) == {"mint", "ice"}
    assert "ice" not in _flavor_profile("Vanilla Ice Cream")[1]
    assert "tobacco" not in _flavor_profile("Tobacconist fruit")[1]
    for name in ("Reds Apple Mango", "Red's Apple Mango", "Red’s Mango"):
        assert _brand(name, [], {})[0] == "Red's"
    assert _brand("NKD 100 Mango", [], {})[0] == "Naked 100"
    assert _brand("Blue Razz", ["Geekbar Pulse (15k)"], {})[0] == "Geek Bar"
    assert _brand("Mango", ["Juice Head", "Coastal Clouds"], {})[0] == "Brand not specified"


def test_legacy_combined_category_is_projected_without_changing_publication():
    from app.services.flavor_explorer.cache import _apply_classification_confidence
    from copy import deepcopy
    profile={"classificationStatus":"approved","notes":["mint"],"categories":["legacy-mint-ice"],
             "sensory":{},"sensoryScale":5}
    original=deepcopy(profile)
    def projected(name,categories=None,catalog_name=None):
        published={"v":{**profile,**({"categories":categories} if categories is not None else {})}}
        product={"id":"p","name":name,"catalogName":catalog_name,"variations":[{"id":"v"}]}
        return _apply_classification_confidence({"products":[product]},published)["products"][0]
    assert projected("Glacial Mint")["categories"]==["mint"]
    assert projected("Mint Freeze")["categories"]==["ice","mint"]
    assert projected("Mint Freeze",catalog_name="Legacy Mint")["categories"]==["ice","mint"]
    assert projected("Glacial Mint",["ice"])["categories"]==["ice"]
    assert profile==original

def test_brand_alias_does_not_remove_the_apple_flavor_from_reds_products():
    snapshot=synthetic_snapshot()
    snapshot["items"][0]["name"]="Red's Apple Berries"
    product=normalize_catalog(snapshot)["products"][0]
    assert product["brand"]=="Red's"
    assert "Apple" in product["name"] and "fruit" in product["categories"]
    from app.services.flavor_explorer.square_catalog import _flavor_profile
    assert "fruit" in _flavor_profile("Mixed Berries")[1]
