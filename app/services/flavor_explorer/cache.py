"""One bounded local Square snapshot, atomically replaced; no Square mutations."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

def default_snapshot_path() -> Path:
    return Path.home() / "flavor-explorer" / "square-snapshot.json"

def _safe_path(path: Path) -> Path:
    path=Path(path).expanduser().resolve()
    home=Path.home().resolve()
    import re
    session_id=os.environ.get("AVA_BOUNDED_SESSION","")
    root=Path("/Volumes/AvaOne-Development-Retained/sessions")/session_id
    expected_home=(root/"persistent/home").resolve()
    expected_tmp=(root/"disposable/tmp").resolve()
    valid_tmp=Path(os.environ.get("TMPDIR","/")).resolve()==expected_tmp
    if not re.fullmatch(r"AVA-ERUPTED-[A-Za-z0-9_-]{1,60}",session_id) or home!=expected_home or not (path.is_relative_to(home) or (valid_tmp and path.is_relative_to(expected_tmp))):
        raise ValueError("Flavor preview cache must stay in the registered development home.")
    if path.suffix!=".json":
        raise ValueError("JSON snapshot path required.")
    return path

def store_snapshot(snapshot: dict, path: Path | None=None) -> dict:
    from app.services.flavor_explorer.square_catalog import project_snapshot
    path=_safe_path(path or default_snapshot_path())
    if not isinstance(snapshot,dict) or not snapshot.get("items") or len(snapshot.get("stores",[]))!=4:
        raise ValueError("A complete Square catalog and four verified stores are required.")
    # Compile and validate before touching the last successful snapshot.
    public=project_snapshot(snapshot,ttl_seconds=int(snapshot.get("inventoryTtlSeconds",900)))
    if not public.get("products"):
        raise ValueError("Snapshot contains no safely resolved discovery products.")
    encoded=json.dumps(snapshot,ensure_ascii=False,separators=(",",":")).encode()
    if len(encoded)>4_000_000:
        raise ValueError("Unexpectedly large discovery snapshot.")
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix=".square-",suffix=".json",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as handle:
            handle.write(encoded);handle.flush();os.fsync(handle.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)
    return public

def read_snapshot(path: Path | None=None, *, reference=None) -> dict:
    if reference is not None:
        raise ValueError("Registered local preview references are not supported by the hosted release.")
    else:
        path=_safe_path(path or default_snapshot_path())
    if not path.is_file() or path.stat().st_size>4_000_000:
        raise ValueError("Square discovery cache is unavailable.")
    return json.loads(path.read_text())

def _project_customer_catalog(snapshot, storage, now=None, *, product_enricher=None):
    """Project current customer-visible variations without changing source observations."""
    from copy import deepcopy
    from app.services.flavor_explorer.discovery import excluded_variation_ids
    from app.services.flavor_explorer.square_catalog import project_snapshot

    excluded = excluded_variation_ids(storage) if storage is not None else set()
    visible = snapshot
    if excluded:
        visible = deepcopy(snapshot)
        items = []
        for item in visible.get("items", []):
            item["variations"] = [variation for variation in item.get("variations", [])
                                  if variation.get("id") not in excluded]
            if item["variations"]:
                items.append(item)
        visible["items"] = items
    result = project_snapshot(visible, now=now,
        ttl_seconds=int(snapshot.get("inventoryTtlSeconds", 900)))
    published, note_labels = {}, {}
    if storage is not None:
        ids = [variation["id"] for product in result["products"] for variation in product["variations"]]
        published = storage.public_classification_for_square(ids)
        note_labels = _published_note_labels(storage, published)
    # Resolve approved similarities against surviving variations only.
    _apply_classification_confidence(result, published)
    result["noteDefinitions"] = _public_note_definitions(result, note_labels)
    if storage is not None:
        from app.services.flavor_explorer.product_photos import ProductPhotos
        if product_enricher is not None:product_enricher(result["products"])
        ProductPhotos(storage).overlay(result["products"])
    result.setdefault("meta", {}).update(source="square",
        syncEnabled=snapshot.get("sync", {}).get("mode") == "automatic")
    result.pop("gaps", None)
    return result


def public_catalog(path: Path | None=None, *, assigned_store_id="hwy99", allow_location_selection=False, now=None, reference=None) -> dict:
    snapshot = read_snapshot(path, reference=reference)
    from app.services.flavor_explorer.storage import open_existing
    database = (reference.database_path() if reference is not None else
                Path.home() / "flavor-explorer" / "intelligence.sqlite3")
    # This registered read path requires its operational visibility authority.
    # Missing/corrupt storage never falls back to an unfiltered customer catalog.
    storage = open_existing(database, reference=reference)
    try:
        result = _project_customer_catalog(snapshot, storage, now=now)
    finally:
        storage.close()
    if assigned_store_id not in {store["id"] for store in result["stores"]}:
        raise ValueError("Assigned store not found in verified Square locations.")
    result.setdefault("meta", {}).update(assignedStoreId=assigned_store_id,
        locationSelectionAllowed=allow_location_selection)
    return result

SENSORY_AXES = ("sweetness", "tartness", "creaminess", "richness", "brightness", "cooling", "crispness")
PROFILE_SOURCE_LABELS = {
    "existing_verified": "Reviewed flavor profile",
    "established": "Reviewed flavor profile",
    "provisional": "Notes from product catalog",
    "awaiting": "Flavor profile coming soon",
}


def _finite_intensity(value, scale=5):
    import math
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > scale:
        return None
    return round(value * 5 / scale)


def _profile_content(profile):
    scale = profile.get("sensoryScale", 5)
    if scale not in (1, 5):
        raise ValueError("Unsupported published sensory scale")
    sensory = profile.get("sensory") or {}
    return {
        "description": profile.get("description", ""),
        "notes": sorted(set(profile.get("notes") or [])),
        "categories": sorted(set(profile.get("categories") or [])),
        "sensory": {axis: _finite_intensity(sensory.get(axis), scale) for axis in SENSORY_AXES},
        "similarities": [{"squareVariationIds":sorted(set(item.get("squareVariationIds",[]))),
                          "weight":item["weight"],"reason":item["reason"]}
                         for item in profile.get("similarities",[])
                         if isinstance(item,dict) and type(item.get("weight")) in (int,float)
                         and 0<item["weight"]<=1 and isinstance(item.get("reason"),str)
                         and isinstance(item.get("squareVariationIds"),list)
                         and all(isinstance(value,str) for value in item["squareVariationIds"])],
    }


def _attested_existing(profile):
    # Reserved for a future recovered-and-reviewed publication, never name inference.
    provenance = profile.get("provenance")
    return (isinstance(provenance, dict)
            and provenance.get("sourceSystem") == "erupted-touchscreen"
            and provenance.get("kind") == "recovered-existing"
            and provenance.get("verified") is True
            and type(provenance.get("sourceFlavorId")) is int
            and provenance["sourceFlavorId"] > 0
            and isinstance(provenance.get("evidenceId"), str)
            and bool(provenance["evidenceId"].strip()))


def _agreed_publication(product, published):
    variations = product.get("variations") or []
    if not variations:
        return None
    profiles = [published.get(variation["id"]) for variation in variations]
    if any(not isinstance(profile, dict) or profile.get("classificationStatus") != "approved" for profile in profiles):
        return None
    content = _profile_content(profiles[0])
    if any(_profile_content(profile) != content for profile in profiles[1:]):
        return None
    confidence = "existing_verified" if all(_attested_existing(profile) for profile in profiles) else "established"
    return content, confidence


def _apply_classification_confidence(result, published):
    variation_products={variation["id"]:product["id"] for product in result.get("products",[])
                        for variation in product.get("variations",[])}
    for product in result.get("products", []):
        agreed = _agreed_publication(product, published)
        if agreed:
            content, confidence = agreed
            product.update(content)
            edges={}
            for relationship in content.get("similarities",[]):
                for variation_id in relationship["squareVariationIds"]:
                    product_id=variation_products.get(variation_id)
                    if product_id and product_id!=product["id"]:
                        candidate={"productId":product_id,"weight":relationship["weight"],"reason":relationship["reason"]}
                        if product_id not in edges or candidate["weight"]>edges[product_id]["weight"]:edges[product_id]=candidate
            product["similarities"]=sorted(edges.values(),key=lambda item:(-item["weight"],item["productId"]))
            product.update(classificationSource="published", classificationStatus="approved",
                           sensorySource="published")
        else:
            product["similarities"]=[]
            confidence = "provisional" if product.get("notes") else "awaiting"
            sensory = product.get("sensory") or {}
            product["sensory"] = {
                axis: _finite_intensity(sensory.get(axis)) if product.get("notes") else None
                for axis in SENSORY_AXES
            }
            product["classificationStatus"] = confidence
        product["classificationConfidence"] = confidence
        product["profileSource"] = PROFILE_SOURCE_LABELS[confidence]
    return result


def classification_review_candidates(snapshot, *, principal, published_profiles=None):
    """Private Phase 3 review projection; caller supplies an authenticated existing admin.

    No queue table, principal, proposal or publication is created here. Category
    slugs and nullable inferred attributes remain drafts requiring explicit review.
    """
    role = getattr(principal, "role", None)
    role = getattr(role, "value", role)
    if (getattr(principal, "active", False) is not True or role not in {"ADMIN", "MANAGER"}
            or type(getattr(principal, "id", None)) is not int or principal.id <= 0):
        raise PermissionError("An authenticated active administrator is required")
    from app.services.flavor_explorer.square_catalog import normalize_catalog
    projection = _apply_classification_confidence(
        {"products": normalize_catalog(snapshot)["products"]}, published_profiles or {})
    result = []
    for product in projection["products"]:
        confidence = product["classificationConfidence"]
        if confidence not in {"provisional", "awaiting"}:
            continue
        result.append({
            "productId": product["id"], "squareItemIds": product.get("squareItemIds", []),
            "squareVariationIds": [variation["id"] for variation in product["variations"]],
            "confidence": confidence,
            "inferredSnapshot": {
                "description": product.get("description", ""),
                "notes": product.get("notes", []), "categories": product.get("categories", []),
                "sensory": {axis: None if value is None else value / 5
                            for axis, value in product["sensory"].items()},
            },
            "sourceMetadata": {
                "catalogObservedAt": product.get("catalogReadAt") or snapshot.get("catalogObservedAt"),
                "catalogName": product.get("catalogName", ""),
                "brandSource": product.get("brandSource"),
                "typeProvenance": product.get("typeProvenance", []),
                "classificationSource": "catalog-inferred",
            },
            "reviewReason": ("Review name-derived notes and inferred sensory estimates."
                             if confidence == "provisional" else
                             "Flavor notes are missing; establish a reviewed profile."),
        })
    return result

def _published_note_labels(storage, published):
    """Read dictionary labels for approved profiles only, without private columns."""
    import re
    from sqlalchemy import select
    slugs = sorted({
        slug for profile in published.values()
        if isinstance(profile, dict) and profile.get("classificationStatus") == "approved"
        for slug in (profile.get("notes") or [])
        if isinstance(slug, str) and re.fullmatch(r"[a-z0-9-]{1,80}", slug)
    })
    labels = {}
    if not slugs:
        return labels
    notes = storage.table("fi_notes")
    with storage.engine.connect() as connection:
        for offset in range(0, len(slugs), 800):
            labels.update(connection.execute(select(notes.c.slug, notes.c.label).where(
                notes.c.slug.in_(slugs[offset:offset + 800]))).all())
    return labels


def _public_note_definitions(result, labels):
    """Only labels referenced by agreed published customer profiles are public.

    This never assigns flavor families or sensory defaults to a new note. Unused
    recovered taxonomy and employee proposal contents are deliberately omitted.
    """
    import re
    slugs = set()
    for product in result.get("products", []):
        if (product.get("classificationSource") != "published"
                or product.get("classificationStatus") != "approved"):
            continue
        for slug in product.get("notes") or []:
            if isinstance(slug, str) and re.fullmatch(r"[a-z0-9-]{1,80}", slug):
                slugs.add(slug)
    definitions = []
    for slug in sorted(slugs):
        label = labels.get(slug)
        label = label.strip() if isinstance(label, str) else ""
        definitions.append({"id": slug, "label": (label or slug.replace("-", " ").title())[:120]})
    return definitions
