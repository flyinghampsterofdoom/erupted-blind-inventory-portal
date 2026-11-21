"""Fixed reviewed release import; never copies development accounts or audit records."""
from pathlib import Path
import hashlib,json
from sqlalchemy import select,insert,text
from app.services.flavor_explorer.production_storage import PostgresStorage,verify_schema
from app.services.flavor_explorer.production_catalog import ProductionCatalog
from app.services.flavor_explorer.product_photos import ProductPhotos
from app.services.flavor_explorer.discovery import DiscoveryVisibility,excluded_variation_ids
from app.services.flavor_explorer.square_catalog import normalize_catalog
from app.services.flavor_explorer.compatibility.store import CompatibilityService

MANIFEST_SHA="4953031dca21ec0e2242b5545ab41ebfff3e2695fe2666c3c62f53795a69ea41"
CP_SEED_SHA="80c4ddea65ce42a0cb4c3d4b583ee785dbb681d7bb2d3790c91c94ff03a4d17d"

def bundle(asset_root):
    root=Path(asset_root)
    if root.is_symlink() or not root.is_dir():raise ValueError("The trusted private release bundle is unavailable.")
    manifest_path=root/"manifest.json"
    if manifest_path.is_symlink():raise ValueError("The trusted private manifest must be a retained file.")
    raw=manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=MANIFEST_SHA:raise ValueError("The fixed reviewed import manifest changed.")
    seed_path=root/"manufacturer-seed.json"
    if seed_path.is_symlink() or hashlib.sha256(seed_path.read_bytes()).hexdigest()!=CP_SEED_SHA:
        raise ValueError("The fixed private compatibility seed changed.")
    result=json.loads(raw)
    if result["version"]!=1 or len(result["photos"])!=6 or len(result["excludedVariationIds"])!=28:
        raise ValueError("The fixed reviewed import scope changed.")
    total=0
    for item in result["photos"]:
        asset=item["asset"]
        if item["file"]!=asset["id"]+".webp":raise ValueError("Reviewed image identity changed.")
        path=root/item["file"]
        if path.is_symlink():raise ValueError("Reviewed image custody changed.")
        data=path.read_bytes()
        if item["file"]!=asset["id"]+".webp" or hashlib.sha256(data).hexdigest()!=asset["id"] or len(data)!=asset["byte_size"]:
            raise ValueError("Reviewed image custody changed.")
        asset["data"]=data;total+=len(data)
    if total!=421564:raise ValueError("Reviewed image custody size changed.")
    return result

def seed_notes(storage):
    notes=storage.table("fi_notes");categories=storage.table("touchscreen_flavor_categories")
    with storage._write() as c:
        rows=c.execute(select(categories).where(categories.c.category_type=="FRUIT").order_by(categories.c.id)).mappings().all()
        if len(rows)!=20 or {r["id"] for r in rows}!=set(range(8,28)):
            raise ValueError("The real existing FRUIT taxonomy needs review; nothing was overwritten.")
        for row in rows:
            existing=c.execute(select(notes).where(notes.c.id==row["id"])).mappings().first()
            values={"id":row["id"],"slug":str(row["slug"]),"label":str(row["name"])}
            if existing:
                if any(existing[k]!=v for k,v in values.items()):raise ValueError("A retained note identity differs.")
            else:c.execute(insert(notes).values(**values))
        c.execute(text("SELECT setval(pg_get_serial_sequence('fi_notes','id'),GREATEST((SELECT COALESCE(MAX(id),1) FROM fi_notes),1),true)"))

def apply_release(engine,actor_id,*,asset_root,catalog=None):
    verify_schema(engine);storage=PostgresStorage(engine)
    with engine.connect() as c:storage._principal(c,actor_id,admin=True)
    data=bundle(asset_root);catalog=catalog or ProductionCatalog(storage)
    if not catalog.refresh_once(force=True):raise ValueError("A fresh read-only production catalog is required before import.")
    products=normalize_catalog(catalog.snapshot())["products"];by_id={r["id"]:r for r in products}
    expected=set(data["excludedVariationIds"]);all_ids={v["id"] for p in products for v in p["variations"]}
    if not expected<=all_ids:raise ValueError("An owner-selected excluded variation is no longer current.")
    chosen=[]
    for product in products:
        ids={v["id"] for v in product["variations"]}
        if ids&expected:
            if not ids<=expected:raise ValueError("An excluded product variation set changed; review the exact selection.")
            chosen.append({**product,"squareVariationIds":sorted(ids)})
    if sum(len(p["squareVariationIds"]) for p in chosen)!=28:raise ValueError("Excluded product scope differs.")
    # Validate every product/asset before the first curation mutation.
    for item in data["photos"]:
        old=item["product"];current=by_id.get(old["id"])
        if current is None or current["brand"]!=old["brand"] or current["type"]!=old["type"] or sorted(v["id"] for v in current["variations"])!=sorted(old["squareVariationIds"]):
            raise ValueError("A reviewed manufacturer photo product identity changed.")
    seed_notes(storage)
    seed=CompatibilityService(storage).import_seed(Path(asset_root)/"manufacturer-seed.json")
    photos=ProductPhotos(storage);results=[]
    for item in data["photos"]:
        product=by_id[item["product"]["id"]];before=photos.describe(product)
        candidate=before["candidates"]["manufacturer"]
        if candidate and not candidate.get("mixed") and candidate["assetId"]==item["asset"]["id"] and all(candidate.get(k,"")==v for k,v in item["metadata"].items()):
            results.append({"productId":product["id"],"assetId":candidate["assetId"],"replayed":True});continue
        if candidate:raise ValueError("A different manufacturer image is already retained; automatic replacement is refused.")
        result=photos.apply_canonical(actor_id=actor_id,product=product,expected_token=before["stateToken"],
            asset=item["asset"],metadata=item["metadata"],source_kind="manufacturer",
            current_product_loader=lambda product_id=product["id"]:next((p for p in normalize_catalog(catalog.snapshot())["products"] if p["id"]==product_id),None))
        results.append({"productId":product["id"],"assetId":result["assetId"],"replayed":False})
    discovery=DiscoveryVisibility(storage)
    current=excluded_variation_ids(storage)
    if not current<=expected:raise ValueError("A newer owner exclusion exists; automatic policy replacement is refused.")
    plan=discovery.preview(actor_id=actor_id,products=chosen,excluded=True,
        reason="Owner-approved production rollout: preserve current E-liquid Explorer exclusions.")
    visibility=discovery.commit(actor_id=actor_id,plan=plan,currentproducts=chosen)
    return {"photos":results,"manufacturerPhotoCount":6,"variationCount":21,
        "canonicalBytes":421564,"excludedVariations":len(excluded_variation_ids(storage)),
        "visibilityOperationId":visibility["id"],"compatibilitySeed":seed}
