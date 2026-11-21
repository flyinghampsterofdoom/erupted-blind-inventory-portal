"""Native PostgreSQL checks for the combined Explorer storage and guards."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest
from sqlalchemy import inspect,insert,select,func,text,update
from sqlalchemy.exc import IntegrityError

from flavor_pg_fixture import explorer_pg_base,explorer_pg_engine,explorer_pg_storage
from app.auth import Role
from app.services.flavor_explorer.production_schema_v0058 import upgrade,verify_schema
from app.services.flavor_explorer.production_storage import PostgresStorage
from app.services.flavor_explorer.administration import FlavorAdministration
from app.services.flavor_explorer.discovery import DiscoveryVisibility,excluded_variation_ids
from app.services.flavor_explorer.product_photos import ProductPhotos
from app.services.flavor_explorer.compatibility.store import CompatibilityService

EMPTY={"description":"","notes":[],"categories":[],"sensory":{},"similarities":[]}
PROFILE={**EMPTY,"description":"Synthetic reviewed raspberry","notes":["raspberry"],
         "categories":[1],"sensory":{"tartness":.6}}
PRODUCT={"id":"fixture-liquid","brand":"Synthetic","name":"Raspberry","type":"freebase",
         "squareVariationIds":["A3","A6"],"imageUrl":"https://images.squareup.com/fixture.jpg"}
EVIDENCE=[{"url":"https://manufacturer.example/fixture","title":"Synthetic manufacturer",
           "publisher":"Fixture","claim":"Explicit fixture compatibility"}]

def count(storage,name):
    with storage.engine.connect() as c:return c.scalar(select(func.count()).select_from(storage.table(name)))
def protected(engine):
    with engine.connect() as c:
        return {name:c.exec_driver_sql('SELECT * FROM "'+name+'" ORDER BY id').all() for name in
            ("principals","stores","touchscreen_flavors","touchscreen_flavor_categories")}
def identities(storage):
    return [storage.mirror_square_identity("ITEM-"+x,[x+"3",x+"6"]) for x in ("A","B")]
def profile(service,flavor):
    return service.catalog_state(["A3","B3"],actor_id=1)["profiles"][str(flavor)]
def publish(service,flavor,snapshot,expected=None):
    return service.commit(actor_id=1,plan=service.preview_publish(actor_id=1,flavor_id=flavor,
        snapshot=snapshot,expected_version_id=expected,reason="Synthetic reviewed native PG publication"))
def raster(color):
    buffer=BytesIO();Image.new("RGB",(36,60),color).save(buffer,"PNG");return buffer.getvalue()

def test_migration_is_atomic_preserves_real_target_shape_and_never_seeds_staff(explorer_pg_engine):
    engine=explorer_pg_engine;before=protected(engine)
    with pytest.raises(RuntimeError,match="injected"):
        with engine.begin() as c:
            upgrade(c)
            raise RuntimeError("injected migration interruption")
    with engine.connect() as c:
        assert not any(name.startswith(("fi_","cp_")) for name in inspect(c).get_table_names())
    assert protected(engine)==before
    with engine.begin() as c:upgrade(c)
    assert verify_schema(engine)["schemaVersion"]==1
    storage=PostgresStorage(engine)
    new=storage.mirror_square_identity("FRESH-SQUARE",["FRESH-VARIANT"])
    assert new!=77 and storage.table("touchscreen_flavors").name=="fi_flavor_identities"
    assert protected(engine)==before and count(storage,"fi_classification_versions")==0
    assert count(storage,"fi_notes")==0 and count(storage,"fi_sensory_axes")==7
    with pytest.raises(PermissionError):storage.mirror_principal(principal_id=10)
    with pytest.raises(PermissionError):storage.mirror_store(store_id=10)
    storage.close()
    with engine.connect() as c:assert c.scalar(text("SELECT 1"))==1

def test_bulk_reverse_restores_unclassified_and_diverged_head_rejects_every_target(explorer_pg_storage):
    storage=explorer_pg_storage;a,b=identities(storage);service=FlavorAdministration(storage)
    bulk=service.preview_bulk(actor_id=1,flavor_ids=[a,b],patch={"notes":{"add":["raspberry"]}},
        expected_versions={str(a):None,str(b):None},reason="Synthetic native PG bulk")
    initial=service.commit(actor_id=1,plan=bulk)
    assert set(storage.public_classification_for_square(["A3","B3"]))=={"A3","B3"}
    service.commit(actor_id=1,plan=service.preview_reverse(actor_id=1,operation_id=initial["id"],reason="Restore initial states"))
    assert storage.public_classification_for_square(["A3","B3"])=={}
    assert profile(service,a)["versionId"] is not None
    again=service.commit(actor_id=1,plan=service.preview_bulk(actor_id=1,flavor_ids=[a,b],
        patch={"notes":{"add":["raspberry"]}},expected_versions={str(v):profile(service,v)["versionId"] for v in (a,b)},
        reason="Second native PG bulk"))
    reverse=service.preview_reverse(actor_id=1,operation_id=again["id"],reason="Captured reverse")
    b_before=profile(service,b)["versionId"]
    publish(service,a,{**PROFILE,"description":"A later reviewed correction"},profile(service,a)["versionId"])
    operations=count(storage,"fi_administration_operations")
    with pytest.raises(ValueError):service.commit(actor_id=1,plan=reverse)
    assert profile(service,b)["versionId"]==b_before and count(storage,"fi_administration_operations")==operations

def test_competing_workers_serialize_and_reject_one_stale_publication(explorer_pg_storage):
    storage=explorer_pg_storage;a,_b=identities(storage);service=FlavorAdministration(storage)
    plans=[service.preview_publish(actor_id=1,flavor_id=a,snapshot={**PROFILE,"description":label},
        expected_version_id=None,reason="Competing worker fixture") for label in ("First fixture","Second fixture")]
    def commit(plan):
        try:return FlavorAdministration(PostgresStorage(storage.engine)).commit(actor_id=1,plan=plan)
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(commit,plans))
    assert sum(result is not None for result in results)==1
    assert count(storage,"fi_publication_revisions")==count(storage,"fi_administration_operations")==1
    assert count(storage,"fi_audit_events")==1

def test_employee_origin_remains_unpublished_until_admin_review_with_native_audit(explorer_pg_storage):
    storage=explorer_pg_storage;a,_b=identities(storage);service=FlavorAdministration(storage)
    submitted=storage.submit_suggestion(flavor_id=a,employee_id=2,store_id=4,base_version_id=None,
        proposed_patch={"notes":["raspberry"],"sensory":{"tartness":.6}},comment="Synthetic employee observation")
    assert storage.public_classification_for_square(["A3"])=={} and count(storage,"fi_publication_revisions")==0
    assert count(storage,"fi_classification_versions")==1
    with pytest.raises(PermissionError):
        service.preview_review(actor_id=3,group_ids=[submitted["review_group_id"]],decision="APPROVED",reason="Lead cannot approve flavors")
    service.commit(actor_id=1,plan=service.preview_review(actor_id=1,group_ids=[submitted["review_group_id"]],
        decision="APPROVED",reason="Explicit native PG admin review"))
    assert storage.public_classification_for_square(["A3"])["A3"]["notes"]==["raspberry"]
    assert count(storage,"fi_audit_events")==3
    for name in ("fi_employee_suggestions","fi_review_decisions","fi_classification_versions","fi_audit_events"):
        with pytest.raises(IntegrityError):
            with storage._write() as c:c.execute(storage.table(name).delete())

def test_visibility_and_photo_priority_share_no_classification_or_cp_mutations(explorer_pg_storage):
    storage=explorer_pg_storage;identities(storage)
    before=protected(storage.engine);visibility=DiscoveryVisibility(storage);photos=ProductPhotos(storage)
    plan=visibility.preview(actor_id=1,products=[PRODUCT],excluded=True,reason="Synthetic exact variation scope")
    visibility.commit(actor_id=1,plan=plan,currentproducts=[PRODUCT])
    assert excluded_variation_ids(storage)=={"A3","A6"}
    restore=visibility.preview(actor_id=1,products=[PRODUCT],excluded=False,reason="Restore synthetic scope")
    visibility.commit(actor_id=1,plan=restore,currentproducts=[PRODUCT])
    official=photos.apply(actor_id=1,product=PRODUCT,expected_token=photos.describe(PRODUCT)["stateToken"],
        mode="custom",source_kind="manufacturer",data=raster("red"),metadata={"sourceLabel":"Manufacturer fixture"})
    own=photos.apply(actor_id=1,product=PRODUCT,expected_token=official["stateToken"],
        mode="custom",source_kind="own",data=raster("blue"),metadata={"sourceLabel":"Own fixture"})
    assert own["assetId"]==official["assetId"] and own["effectiveSourceKind"]=="manufacturer"
    fallback=photos.apply(actor_id=1,product=PRODUCT,expected_token=own["stateToken"],
        source_kind="manufacturer",action="remove_candidate")
    assert fallback["assetId"]!=official["assetId"] and fallback["effectiveSourceKind"]=="own"
    assert photos.asset(official["assetId"])["mime"]=="image/webp"
    with pytest.raises(ValueError):
        photos.apply(actor_id=1,product=PRODUCT,expected_token=official["stateToken"],mode="hidden")
    assert protected(storage.engine)==before
    assert count(storage,"fi_classification_versions")==count(storage,"cp_products")==0

def test_native_photo_event_policy_and_history_guards_reject_direct_corruption(explorer_pg_storage):
    storage=explorer_pg_storage;photos=ProductPhotos(storage)
    result=photos.apply(actor_id=1,product=PRODUCT,expected_token=photos.describe(PRODUCT)["stateToken"],
        mode="custom",source_kind="manufacturer",data=raster("red"))
    ops=photos.metadata.tables["fi_product_photo_operations"];events=photos.metadata.tables["fi_product_photo_events"]
    before=count(storage,"fi_product_photo_operations")
    with pytest.raises(IntegrityError):
        with storage._write() as c:
            operation=str(uuid.uuid4())
            c.execute(insert(ops).values(id=operation,actor_id=1,created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                product=PRODUCT,mode="catalog",asset_id=None,attribution={},source_kind="manufacturer",candidate_action="remove"))
            head=c.scalar(select(func.max(events.c.id)).where(events.c.square_variation_id=="A3"))
            c.execute(insert(events).values(operation_id=operation,square_variation_id="A3",before_event_id=head,policy_mode="hidden"))
    assert count(storage,"fi_product_photo_operations")==before
    with pytest.raises(IntegrityError):
        with storage._write() as c:c.execute(ops.update().values(attribution={}))
    assert photos.describe(PRODUCT)["assetId"]==result["assetId"]

def test_cp_lead_review_and_immutable_originals_use_existing_principal_permissions(explorer_pg_storage,tmp_path):
    storage=explorer_pg_storage
    people={pid:SimpleNamespace(id=pid,role=Role(role),store_id=store,active=True)
        for pid,role,store in [(1,"ADMIN",None),(2,"STORE",4),(3,"LEAD",9)]}
    source={"key":"fixture",**EVIDENCE[0]}
    seed={"manufacturers":[{"key":"fixture","name":"Synthetic manufacturer","aliases":[]}],
        "evidence":[source],"products":[{"key":"pod-06","manufacturerKey":"fixture","name":"Synthetic 0.6 pod",
            "model":"Fixture","family":"Fixture","kind":"pod","integratedCoil":True,"resistanceOhm":"0.6",
            "identifiers":[{"kind":"sku","value":"0000123","packageConfiguration":"4-pack"}]}],
        "relationships":[],"catalogAssociations":[]}
    path=tmp_path/"explicit-synthetic-seed.json";path.write_text(json.dumps(seed))
    service=CompatibilityService(storage);service.import_seed(path)
    original=service.product(people[2],"pod-06")
    proposed={"patch":{"name":"Company-reviewed synthetic pod"},"evidence":EVIDENCE}
    suggestion=service.suggest(people[2],{"kind":"correction","productId":"pod-06",
        "baseVersion":original["version"],"proposed":proposed,"comment":"Explicit fixture"})
    with pytest.raises(PermissionError):service.review(people[2],suggestion["id"],"APPROVED")
    with storage._write() as c:
        c.execute(text("INSERT INTO principal_permission_overrides(principal_id,permission_key,allowed) VALUES(3,'compatibility.review',false)"))
    with pytest.raises(PermissionError):service.review(people[3],suggestion["id"],"APPROVED")
    with storage._write() as c:c.execute(text("UPDATE principal_permission_overrides SET allowed=true WHERE principal_id=3"))
    service.review(people[3],suggestion["id"],"APPROVED",reason="Company-wide fixture review")
    assert service.product(people[2],"pod-06")["name"]==proposed["patch"]["name"]
    assert service.search(people[2],"0000123","identifier")["exactProductId"]=="pod-06"
    for table in ("cp_suggestions","cp_audit"):
        with pytest.raises(IntegrityError):
            with storage._write() as c:c.execute(storage.table(table).delete())
    with pytest.raises(IntegrityError):
        with storage._write() as c:c.execute(update(storage.table("cp_suggestions")).values(proposed={}))
