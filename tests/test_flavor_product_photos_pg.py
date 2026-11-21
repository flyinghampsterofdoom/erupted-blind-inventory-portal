"""Portable Product Photos qualification against the owned PostgreSQL fixture."""
from copy import deepcopy
from io import BytesIO
import hashlib

import pytest
from PIL import Image
from sqlalchemy import select,func

from flavor_pg_fixture import explorer_pg_base,explorer_pg_engine,explorer_pg_storage
from app.services.flavor_explorer.product_photos import ProductPhotos,NAMES


def product(ids=("PHOTO-V1","PHOTO-V2"),**extra):
    return {"id":"photo-product","brand":"Synthetic","name":"Photo fixture","type":"freebase",
            "squareVariationIds":list(ids),"catalogImageUrl":"https://images.squareup.com/fixture.jpg",**extra}


def raster(color="red",format="PNG"):
    output=BytesIO()
    Image.new("RGB",(30,42),color).save(output,format)
    return output.getvalue()


def canonical(color="red"):
    data=raster(color,"WEBP")
    return {"id":hashlib.sha256(data).hexdigest(),"data":data,"mime":"image/webp",
            "width":30,"height":42,"byte_size":len(data)}


def apply(photos,p,**values):
    return photos.apply(actor_id=1,product=p,expected_token=photos.describe(p)["stateToken"],**values)


def test_reviewed_canonical_import_preserves_sha_bytes_and_rejects_false_custody(explorer_pg_storage):
    photos=ProductPhotos(explorer_pg_storage);p=product();asset=canonical()
    saved=photos.apply_canonical(actor_id=1,product=p,expected_token=photos.describe(p)["stateToken"],
        asset=asset,metadata={"sourceLabel":"Reviewed manufacturer"},source_kind="manufacturer")
    assert saved["assetId"]==asset["id"]
    assert photos.asset(asset["id"])["data"]==asset["data"]
    again=photos.apply_canonical(actor_id=1,product=p,expected_token=saved["stateToken"],
        asset=asset,metadata={"sourceLabel":"Reviewed manufacturer"},source_kind="manufacturer")
    assert again["assetId"]==saved["assetId"]
    with explorer_pg_storage.engine.connect() as c:
        assets=photos.metadata.tables[NAMES[1]];ops=photos.metadata.tables[NAMES[2]]
        assert c.scalar(select(func.count()).select_from(assets))==1
        before=c.scalar(select(func.count()).select_from(ops))
    for wrong in ({**asset,"id":"0"*64},{**asset,"width":31},{**asset,"mime":"image/png"},
                  {**asset,"byte_size":len(asset["data"])+1}):
        with pytest.raises(ValueError):
            photos.apply_canonical(actor_id=1,product=p,expected_token=again["stateToken"],asset=wrong)
    with pytest.raises(PermissionError):
        photos.apply_canonical(actor_id=2,product=p,expected_token=again["stateToken"],asset=asset)
    with explorer_pg_storage.engine.connect() as c:
        assert c.scalar(select(func.count()).select_from(ops))==before


def test_legacy_source_claims_stay_unclassified_until_append_only_trusted_assignment(explorer_pg_storage):
    photos=ProductPhotos(explorer_pg_storage);p=product()
    legacy=apply(photos,p,mode="custom",data=raster(),metadata={
        "sourceLabel":"Manufacturer wording alone is not a trusted source kind",
        "sourcePageUrl":"https://maker.example/product"})
    assert legacy["effectiveSourceKind"]=="legacy"
    assert legacy["candidates"]["manufacturer"] is None
    event=legacy["history"][0]["eventId"];op_id=legacy["operationId"]
    ops=photos.metadata.tables[NAMES[2]]
    with explorer_pg_storage.engine.connect() as c:
        original=dict(c.execute(select(ops).where(ops.c.id==op_id)).mappings().one())
    reviewed=photos.classify_retained(actor_id=1,product=p,expected_token=legacy["stateToken"],
        event_id=event,source_kind="manufacturer")
    assert reviewed["assetId"]==legacy["assetId"]
    assert reviewed["effectiveSourceKind"]=="manufacturer"
    assert reviewed["history"][0]["restoredFromEventId"]==event
    assert reviewed["history"][0]["sourceKind"]=="manufacturer"
    assert reviewed["candidates"]["legacy"]["assetId"]==legacy["assetId"]
    with explorer_pg_storage.engine.connect() as c:
        assert dict(c.execute(select(ops).where(ops.c.id==op_id)).mappings().one())==original
        assert c.scalar(select(func.count()).select_from(photos.metadata.tables[NAMES[1]]))==1
    with pytest.raises(ValueError):
        photos.classify_retained(actor_id=1,product=product(("OTHER-VARIANT",)),
            expected_token=photos.describe(product(("OTHER-VARIANT",)))["stateToken"],event_id=event,source_kind="manufacturer")


def test_priority_preserves_original_and_effective_attribution_when_own_fallback_changes(explorer_pg_storage):
    photos=ProductPhotos(explorer_pg_storage)
    p=product(originalImageUrl="/flavor-explorer/media/originals/17")
    initial=photos.describe(p)
    assert initial["mode"]=="catalog"
    assert initial["url"]==p["originalImageUrl"]
    legacy=apply(photos,p,mode="custom",data=raster("red"),metadata={"sourceLabel":"Legacy"})
    assert legacy["url"]==p["originalImageUrl"]
    own=apply(photos,p,mode="custom",data=raster("blue"),source_kind="own",metadata={"sourceLabel":"Our first"})
    maker=apply(photos,p,mode="custom",data=raster("green"),source_kind="manufacturer",
        metadata={"sourceLabel":"Official","sourcePageUrl":"https://maker.example/product"})
    own2=apply(photos,p,mode="custom",data=raster("yellow"),source_kind="own",metadata={"sourceLabel":"Our replacement"})
    assert own2["stateToken"]!=maker["stateToken"]
    assert own2["url"]==maker["url"]
    assert own2["sourceLabel"]=="Official"
    assert own2["sourcePageUrl"]=="https://maker.example/product"
    assert own2["candidates"]["own"]["assetId"]!=own["assetId"]
    with pytest.raises(ValueError,match="changed"):
        photos.apply(actor_id=1,product=p,expected_token=maker["stateToken"],action="remove_candidate",source_kind="manufacturer")
    fallback=apply(photos,p,action="remove_candidate",source_kind="manufacturer")
    assert fallback["url"]==own2["candidates"]["own"]["url"]
    original=apply(photos,p,action="remove_candidate",source_kind="own")
    assert original["url"]==p["originalImageUrl"]
    assert original["sourceLabel"]==""
    assert original["originalFallback"]["url"]==p["originalImageUrl"]
    assert original["candidates"]["legacy"] is not None
    catalog=apply(photos,p,mode="catalog")
    assert catalog["url"]==p["originalImageUrl"]
    hidden=apply(photos,p,mode="hidden")
    assert hidden["url"] is None
    assert hidden["candidates"]["legacy"] is not None
    unsafe=deepcopy(p);unsafe["originalImageUrl"]="/flavor-explorer/media/originals/17?token=x"
    fresh=product(("FRESH",),originalImageUrl=unsafe["originalImageUrl"])
    assert photos.describe(fresh)["url"]==p["catalogImageUrl"]


def test_slot_removal_and_restored_removal_preserve_each_variation_policy(explorer_pg_storage):
    photos=ProductPhotos(explorer_pg_storage);p=product()
    manufacturer=apply(photos,p,mode="custom",data=raster("green"),source_kind="manufacturer")
    maker_event=manufacturer["history"][0]["eventId"]
    own=apply(photos,p,mode="custom",data=raster("blue"),source_kind="own")
    first=product(("PHOTO-V1",))
    apply(photos,first,mode="hidden")
    removed=apply(photos,p,action="remove_candidate",source_kind="manufacturer")
    remove_event=removed["history"][0]["eventId"]
    assert removed["mode"]=="mixed"
    assert removed["history"][0]["action"]=="remove"
    assert removed["history"][0]["policyMode"]=="mixed"
    assert photos.describe(first)["mode"]=="hidden"
    assert photos.describe(first)["url"] is None
    assert photos.describe(product(("PHOTO-V2",)))["url"]==own["candidates"]["own"]["url"]
    restored_maker=apply(photos,p,restore_event_id=maker_event)
    assert restored_maker["effectiveSourceKind"]=="manufacturer"
    assert restored_maker["candidates"]["own"]["assetId"]==own["candidates"]["own"]["assetId"]
    apply(photos,first,mode="hidden")
    restored_remove=apply(photos,p,restore_event_id=remove_event)
    assert restored_remove["mode"]=="mixed"
    assert restored_remove["history"][0]["action"]=="remove"
    assert restored_remove["history"][0]["sourceKind"]=="manufacturer"
    assert restored_remove["history"][0]["policyMode"]=="mixed"
    assert photos.describe(first)["url"] is None
    assert photos.describe(product(("PHOTO-V2",)))["url"]==own["candidates"]["own"]["url"]
    assert restored_remove["candidates"]["manufacturer"] is None
    catalog=apply(photos,p,mode="catalog")
    apply(photos,p,action="remove_candidate",source_kind="own")
    assert photos.describe(p)["mode"]=="catalog"
    assert photos.describe(p)["url"]==catalog["url"]


def test_legacy_sqlite_schema_reads_and_upgrades_without_rewriting_history():
    """Portable feature fixture; never constructs or bypasses the guarded local adapter."""
    from contextlib import contextmanager
    from datetime import datetime,timezone
    import json
    from sqlalchemy import create_engine,MetaData,text,inspect
    from sqlalchemy.exc import DBAPIError
    from app.services.flavor_explorer.product_photos import initialize_product_photos

    class Fixture:
        def __init__(self):
            self.engine=create_engine("sqlite+pysqlite:///:memory:")
            self.metadata=MetaData()
        def table(self,name):return self.metadata.tables[name]
        @contextmanager
        def _write(self):
            with self.engine.begin() as c:yield c
        def _principal(self,c,identity,admin=False):
            row=c.execute(select(self.table("principals")).where(self.table("principals").c.id==identity)).mappings().first()
            if not row or not row["active"] or admin and row["role"]!="ADMIN":raise PermissionError()
            return row

    storage=Fixture();p=product(("LEGACY-V",));asset=canonical()
    with storage.engine.begin() as c:
        for statement in (
            "CREATE TABLE stores(id INTEGER PRIMARY KEY)",
            "CREATE TABLE principals(id INTEGER PRIMARY KEY,active INTEGER NOT NULL,role TEXT NOT NULL)",
            "CREATE TABLE fi_product_photo_schema(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)",
            "CREATE TABLE fi_product_photo_assets(id VARCHAR(64) PRIMARY KEY,data BLOB NOT NULL,mime VARCHAR(40) NOT NULL,width INTEGER NOT NULL,height INTEGER NOT NULL,byte_size INTEGER NOT NULL)",
            "CREATE TABLE fi_product_photo_operations(id VARCHAR(36) PRIMARY KEY,actor_id INTEGER NOT NULL REFERENCES principals(id),created_at DATETIME NOT NULL,product JSON NOT NULL,mode VARCHAR(10) NOT NULL,asset_id VARCHAR(64) REFERENCES fi_product_photo_assets(id),attribution JSON NOT NULL,restored_from_event_id INTEGER)",
            "CREATE TABLE fi_product_photo_events(id INTEGER PRIMARY KEY AUTOINCREMENT,operation_id VARCHAR(36) NOT NULL REFERENCES fi_product_photo_operations(id),square_variation_id TEXT NOT NULL,before_event_id INTEGER)",
            "INSERT INTO principals VALUES(1,1,'ADMIN')",
            "INSERT INTO fi_product_photo_schema VALUES(1,1)",
        ):c.exec_driver_sql(statement)
        c.execute(text("INSERT INTO fi_product_photo_assets(id,data,mime,width,height,byte_size) VALUES(:id,:data,:mime,:width,:height,:byte_size)"),asset)
        c.execute(text("INSERT INTO fi_product_photo_operations VALUES(:id,1,:at,:product,'custom',:asset,:metadata,NULL)"),
            {"id":"legacy-operation","at":datetime(2026,11,20,tzinfo=timezone.utc).isoformat(),
             "product":json.dumps(p),"asset":asset["id"],"metadata":json.dumps({"sourceLabel":"Retained","sourcePageUrl":"","sourceImageUrl":"","rightsNote":""})})
        c.exec_driver_sql("INSERT INTO fi_product_photo_events VALUES(1,'legacy-operation','LEGACY-V',NULL)")
        before={name:([column["name"] for column in inspect(c).get_columns(name)],
                     c.exec_driver_sql("SELECT * FROM "+name).all()) for name in NAMES[:4]}
    storage.metadata.reflect(storage.engine,only=["stores","principals"])
    photos=ProductPhotos(storage)
    legacy=photos.describe(p)
    assert legacy["effectiveSourceKind"]=="legacy"
    assert legacy["assetId"]==asset["id"]
    with storage.engine.connect() as c:
        assert NAMES[4] not in inspect(c).get_table_names()
        assert "source_kind" not in {column["name"] for column in inspect(c).get_columns(NAMES[2])}
    initialize_product_photos(storage)
    with storage.engine.connect() as c:
        for name,(columns,rows) in before.items():
            assert c.exec_driver_sql("SELECT "+",".join(columns)+" FROM "+name).all()==rows
        assert c.exec_driver_sql("SELECT version FROM "+NAMES[4]).scalar_one()==1
    own=apply(photos,p,mode="custom",data=raster("blue"),source_kind="own")
    assert own["effectiveSourceKind"]=="own"
    apply(photos,p,mode="hidden")
    removed=apply(photos,p,action="remove_candidate",source_kind="own")
    assert removed["mode"]=="hidden"
    assert removed["candidates"]["legacy"]["assetId"]==asset["id"]
    assert photos.asset(asset["id"])["data"]==asset["data"]
    with pytest.raises(DBAPIError),storage._write() as c:
        c.exec_driver_sql("UPDATE fi_product_photo_priority_schema SET version=2")
    storage.engine.dispose()
