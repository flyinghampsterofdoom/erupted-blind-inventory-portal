"""Square-owned discovery facts, normalized without a second product authority.

All transports are injected. This module performs no filesystem or database IO.
Only capture_readonly makes remote reads; every request crosses the established
Square policy. Snapshot inventory quantities belong in private server storage.
The customer projection intentionally contains no quantities, costs or secrets.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qsl, urlsplit

from app.services.square_request_policy import enforce_square_request_policy

SCHEMA_VERSION = 1
SENSORY_KEYS = ("sweetness", "tartness", "creaminess", "richness", "brightness", "cooling", "crispness")
STORE_SPECS = (
    ("hwy99", "Highway 99", 4, {"hwy99", "highway99"}),
    ("longview", "Longview", 6, {"longview"}),
    ("andresen", "Andresen", 2, {"andresen"}),
    ("sr503", "SR503", 9, {"sr503", "stateroute503"}),
)
TYPE_PATTERNS = {
    "freebase": re.compile(r"\bfree[\s-]?base\b", re.I),
    "salt": re.compile(r"\b(?:salts?|nicotine\s+salts?)\b", re.I),
    "disposable": re.compile(r"\bdisposables?\b", re.I),
}
MG_PATTERN = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)?)\s*mg(?:\s*/\s*ml)?(?![a-z])", re.I)
PERCENT_PATTERN = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)?)\s*%(?!\w)")
SIZE_PATTERN = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)?)\s*ml\b", re.I)
KNOWN_BRANDS = (
    "Charlie's Chalk Dust", "Coastal Clouds", "Beard Vape Co", "Fresh Farms",
    "Suicide Bunny", "Keep It 100", "Air Factory", "Custard Monster", "Dessert Monster",
    "Fruit Monster", "Jam Monster", "Pacha Mama", "Dinner Lady", "Lost Vape",
    "Juice Head", "Pod Juice", "Naked 100", "NKD 100", "The Finest", "Red's Apple",
    "Reds Apple", "Geek Bar", "Lost Mary", "Elf Bar", "Vapetasia", "Fruitia",
    "Fusion", "Ruthless", "Cuttwood", "Sadboy", "Humble", "7 Daze", "Dripmore",
    "Yogi", "Glas", "Twist", "RAZ",
)
# Shared Phase 1 vocabulary. These are provisional name-derived suggestions,
# never published classifications or a path for employee input to publish itself.
NOTE_SPECS = (
    ("raspberry", r"\b(?:raspberr(?:y|ies)|razz)\b", ("fruit",), (3,4,0,1,5,0,4)),
    ("strawberry", r"\b(?:strawberr(?:y|ies)|straw)\b", ("fruit",), (4,2,0,1,4,0,2)),
    ("blueberry", r"\bblueberr(?:y|ies)\b", ("fruit",), (4,2,0,2,3,0,2)),
    ("blackberry", r"\bblackberr(?:y|ies)\b", ("fruit",), (3,3,0,2,4,0,3)),
    ("pineapple", r"\bpineapples?\b", ("fruit",), (4,4,0,1,5,0,3)),
    ("mango", r"\bmango(?:es|s)?\b", ("fruit",), (5,2,1,2,4,0,2)),
    ("green-apple", r"\bgreen[\s-]+apples?\b", ("fruit",), (3,5,0,0,5,0,5)),
    ("peach", r"\bpeach(?:es)?\b", ("fruit",), (4,2,0,1,4,0,3)),
    ("kiwi", r"\bkiwis?\b", ("fruit",), (3,4,0,0,5,0,4)),
    ("watermelon", r"\bwatermelons?\b", ("fruit",), (4,1,0,0,4,0,4)),
    ("lemon", r"\b(?:lemons?|lemonade)\b", ("fruit",), (2,5,0,0,5,0,4)),
    ("lime", r"\b(?:limes?|limeade)\b", ("fruit",), (2,5,0,0,5,0,5)),
    ("cherry", r"\bcherr(?:y|ies)\b", ("fruit",), (4,3,0,2,4,0,2)),
    ("grape", r"\bgrapes?\b", ("fruit",), (5,2,0,2,3,0,2)),
    ("vanilla", r"\bvanilla\b", ("dessert",), (4,0,4,4,0,0,0)),
    ("custard", r"\bcustard\b", ("dessert",), (4,0,5,5,0,0,0)),
    ("cream", r"\bcream\b", ("dessert",), (3,0,5,4,0,0,0)),
    ("caramel", r"\bcaramel\b", ("dessert","candy"), (5,0,2,5,0,0,0)),
    ("mint", r"\bmint\b", ("ice",), (1,0,0,0,4,5,5)),
    ("tobacco", r"\btobacco\b", ("tobacco",), (1,0,0,5,0,0,1)),
    ("black-tea", r"\b(?:black[\s-]+tea|tea)\b", ("beverage",), (1,1,0,2,3,0,3)),
    ("cola", r"\bcola\b", ("beverage",), (5,3,0,2,3,0,4)),
)


def _now(value=None):
    value = value() if callable(value) else value
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("An explicit timezone is required for discovery observations")
    return value.astimezone(timezone.utc)


def _date(value):
    try:
        return _now(value) if value else None
    except (ValueError, TypeError):
        return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _number(value):
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else float(value)


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style"}:
            self.skip += 1
        elif tag.lower() in {"br", "p", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style"} and self.skip:
            self.skip -= 1
        elif tag.lower() in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.skip:
            self.parts.append(value)


def plaintext(value, limit=1200):
    parser = _PlainText()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()[:limit]


def safe_square_image(value):
    """Permit only public Square image origins, never arbitrary or signed URLs."""
    try:
        parsed = urlsplit(str(value or ""))
        host = (parsed.hostname or "").lower()
        allowed = (
            host == "images.squareup.com"
            or host.endswith(".squarecdn.com")
            or bool(re.fullmatch(r"(?:items-images-production|square-production)\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", host))
        )
        if parsed.scheme != "https" or not allowed or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 443} or parsed.fragment:
            return None
        if any(re.search(r"token|signature|credential|authorization|x-amz|key", key, re.I)
               for key, _ in parse_qsl(parsed.query)):
            return None
        return parsed.geturl()
    except (ValueError, TypeError):
        return None


def _attributes(raw):
    """Retain only discovery-relevant custom values, excluding all other metadata."""
    result = {}
    for key, value in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(value, dict):
            continue
        label = str(value.get("name") or key)
        if not re.search(r"nicotine|strength|format|product.?type|brand", label, re.I):
            continue
        result[str(key)[:120]] = {
            field: str(value[field])[:240]
            for field in ("name", "string_value", "number_value")
            if value.get(field) is not None
        }
    return result


def resolve_type(category_names, item_name="", variation_name="", *, attributes=None):
    # Device compatibility is not a liquid format. Square's real catalog has
    # 'Salt Device' hardware categories whose color variations must never become salts.
    labels=[str(value).strip() for value in category_names]
    explicit_format=any(re.fullmatch(r"(?:free[ -]?base|(?:nicotine[ -]+)?salts?|disposables?)",label,re.I) for label in labels)
    hardware=any(re.search(r"\b(?:devices?|pods?|coils?|tanks?|mod(?:s|[ -]+kits?)?|chargers?|batteries|accessories)\b",label,re.I) for label in labels)
    explicit_disposable=bool(TYPE_PATTERNS["disposable"].search(item_name+" "+variation_name))
    if hardware and not explicit_format and not explicit_disposable:
        return {"type":None,"evidence":[],"issue":"unsupported_product_type"}
    evidence = []
    for field, values in (
        ("category", category_names), ("item.name", [item_name]), ("variation.name", [variation_name])
    ):
        for value in values:
            if field=="category" and re.search(r"\b(?:devices?|pods?|coils?|tanks?|mod(?:s|[ -]+kits?)?|chargers?|batteries|accessories)\b",str(value),re.I):
                continue
            for kind, pattern in TYPE_PATTERNS.items():
                if pattern.search(str(value)):
                    evidence.append({"field": field, "value": str(value), "type": kind})
    for key, value in (attributes or {}).items():
        label = str(value.get("name") or key)
        if re.search(r"format|product.?type", label, re.I):
            for kind, pattern in TYPE_PATTERNS.items():
                if pattern.search(str(value.get("string_value") or "")):
                    evidence.append({"field": "custom_attribute", "value": label, "type": kind})
    kinds = {row["type"] for row in evidence}
    return {
        "type": next(iter(kinds)) if len(kinds) == 1 else None,
        "evidence": evidence,
        "issue": "conflicting_product_type" if len(kinds) > 1 else "unresolved_product_type" if not kinds else None,
    }


def resolve_strength(item_name, variation_name, *, item_attributes=None, variation_attributes=None):
    evidence = []
    for field, text in (("item.name", item_name), ("variation.name", variation_name)):
        for pattern, multiplier in ((MG_PATTERN, 1), (PERCENT_PATTERN, 10)):
            for match in pattern.finditer(str(text or "")):
                evidence.append({"field": field, "value": match.group(0), "strengthMg": _number(Decimal(match[1]) * multiplier)})
    for field, attributes in (("item.custom_attribute", item_attributes), ("variation.custom_attribute", variation_attributes)):
        for key, value in (attributes or {}).items():
            label = str(value.get("name") or key)
            if not re.search(r"nicotine|strength", label, re.I):
                continue
            text = str(value.get("string_value") or value.get("number_value") or "")
            tokens = []
            for pattern, multiplier in ((MG_PATTERN, 1), (PERCENT_PATTERN, 10)):
                tokens.extend(_number(Decimal(match[1])*multiplier) for match in pattern.finditer(text))
            if not tokens and re.fullmatch(r"\d+(?:\.\d+)?", text):
                if re.search(r"percent|%", label, re.I):
                    tokens = [_number(Decimal(text)*10)]
                elif re.search(r"(?:^|[_\s])mg(?:[_/\s]|$)", label, re.I):
                    tokens = [_number(text)]
            for number in tokens:
                evidence.append({"field": field, "value": label, "strengthMg": number})
    strengths = {row["strengthMg"] for row in evidence}
    invalid = any(number < 0 or number > 100 for number in strengths)
    return {
        "strengthMg": next(iter(strengths)) if len(strengths) == 1 and not invalid else None,
        "evidence": evidence,
        "issue": "invalid_strength" if invalid else "conflicting_strength" if len(strengths) > 1 else "unresolved_strength" if not strengths else None,
    }


def _clean_label(value):
    value = MG_PATTERN.sub(" ", str(value or ""))
    value = PERCENT_PATTERN.sub(" ", value)
    value = SIZE_PATTERN.sub(" ", value)
    value = re.sub(r"\b(?:nicotine\s+salts?|nic\s+salts?|salts?|free[\s-]?base)\b", " ", value, flags=re.I)
    value = re.sub(r"[|·]+", " ", value)
    value = re.sub(r"\(\s*\)", " ", value)
    return re.sub(r"\s+", " ", value).strip(" -|,/")


def _brand(item_name, category_names, attributes):
    for key, value in (attributes or {}).items():
        if re.search(r"\bbrand\b", str(value.get("name") or key).replace("_", " "), re.I):
            brand = plaintext(value.get("string_value"), 80)
            if brand:
                return brand, "custom_attribute"
    candidates = set(KNOWN_BRANDS)
    for name in category_names:
        if name and not any(pattern.search(name) for pattern in TYPE_PATTERNS.values()):
            candidates.add(name)
    for brand in sorted(candidates, key=lambda value: (-len(value), value)):
        if re.match(r"^" + re.escape(brand) + r"(?=$|[\s:·(-])", item_name, re.I):
            return brand, "catalog_name_prefix"
    return "Brand not specified", "unresolved"


def _flavor_profile(name):
    matched = [spec for spec in NOTE_SPECS if re.search(spec[1], name, re.I)]
    notes = [spec[0] for spec in matched]
    categories = {category for spec in matched for category in spec[2]}
    sensory = {
        key: int(sum(spec[3][index] for spec in matched)/len(matched) + 0.5) if matched else 0
        for index, key in enumerate(SENSORY_KEYS)
    }
    if re.search(r"\b(?:ice|iced|freeze|frozen|menthol|cool|cooling)\b", name, re.I):
        categories.add("ice")
        sensory["cooling"] = max(sensory["cooling"], 4)
        sensory["crispness"] = max(sensory["crispness"], 3)
    for category, pattern in (
        ("dessert", r"\b(?:cake|cheesecake|pie|pudding|cookie|cookies)\b"),
        ("beverage", r"\b(?:lemonade|limeade|tea|soda|cola|coffee)\b"),
        ("candy", r"\b(?:candy|gummy|gummies|chew|sour\s+belts)\b"),
        ("fruit", r"\b(?:apple|grapefruit|guava|orange|citrus|coconut|melon)\b"),
    ):
        if re.search(pattern, name, re.I):
            categories.add(category)
    if "watermelon" in notes:
        art = "watermelon"
    elif set(notes) & {"raspberry", "blueberry", "blackberry", "strawberry", "grape", "cherry"}:
        art = "berry"
    elif set(notes) & {"green-apple", "lemon", "lime"}:
        art = "citrus"
    elif set(notes) & {"pineapple", "mango", "kiwi", "peach"}:
        art = "tropical"
    elif "dessert" in categories:
        art = "dessert"
    elif "tobacco" in notes:
        art = "tobacco"
    elif "ice" in categories:
        art = "mint"
    else:
        art = "tropical"
    accents = {"berry":"#e8668f", "citrus":"#b2c77c", "tropical":"#e6b269",
               "dessert":"#d5b68a", "tobacco":"#bd946f", "mint":"#9ccfc6", "watermelon":"#df8f93"}
    return notes, sorted(categories), sensory, art, accents[art]


def resolve_locations(locations):
    stores, gaps = [], []
    for key, label, local_id, aliases in STORE_SPECS:
        matches = []
        for location in locations:
            if not isinstance(location, dict) or str(location.get("status") or "").upper() != "ACTIVE":
                continue
            name = re.sub(r"[^a-z0-9]", "", str(location.get("name") or "").lower())
            name = re.sub(r"^erupted(?:vapor)?", "", name)
            if name in aliases and str(location.get("id") or "").strip():
                matches.append(location)
        verified = len(matches) == 1
        store = {"id": key, "name": label, "shortName": label, "localStoreId": local_id,
                 "squareLocationId": str(matches[0]["id"]) if verified else None, "verified": verified}
        if not verified:
            gaps.append({"code":"ambiguous_store_mapping" if matches else "missing_store_mapping", "storeId":key})
        stores.append(store)
    counts = Counter(store["squareLocationId"] for store in stores if store["verified"])
    for store in stores:
        if store["verified"] and counts[store["squareLocationId"]] > 1:
            store.update(verified=False, squareLocationId=None)
            gaps.append({"code":"duplicate_store_location", "storeId":store["id"]})
    return stores, gaps


def _minimal_item(item, observed_at):
    data = item.get("item_data") or {}
    if item.get("is_deleted") or data.get("is_archived") or not item.get("id"):
        return None
    result = {
        "id":str(item["id"]), "name":plaintext(data.get("name"), 300),
        "description":plaintext(data.get("description_plaintext") or data.get("description") or data.get("description_html")),
        "updatedAt":item.get("updated_at"), "observedAt":observed_at,
        "categoryIds": sorted({str(category["id"]) for category in (data.get("categories") or []) if isinstance(category, dict) and category.get("id")}
                              | {str((data.get("reporting_category") or {})["id"])} if (data.get("reporting_category") or {}).get("id") else
                              {str(category["id"]) for category in (data.get("categories") or []) if isinstance(category, dict) and category.get("id")}),
        "imageIds":[str(value) for value in (data.get("image_ids") or [])],
        "customAttributes":_attributes(item.get("custom_attribute_values")),
        "variations":[],
    }
    for raw in data.get("variations", []) or []:
        if not isinstance(raw, dict) or raw.get("is_deleted") or not raw.get("id"):
            continue
        variation = raw.get("item_variation_data") or {}
        if variation.get("sellable") is False:
            continue
        result["variations"].append({
            "id":str(raw["id"]), "squareItemId":str(item["id"]),
            "name":plaintext(variation.get("name"), 240), "sku":str(variation.get("sku") or "")[:120],
            "updatedAt":raw.get("updated_at") or item.get("updated_at"),
            "imageIds":[str(value) for value in (variation.get("image_ids") or [])],
            "customAttributes":_attributes(raw.get("custom_attribute_values")),
            "trackInventory":variation.get("track_inventory"),
        })
    return result


def capture_readonly(get, post, now=None):
    """Capture minimal facts through injected approved Square clients, with no IO.

    get(path) -> dict, post(path, payload) -> dict. Errors never imply zero.
    A failed capture raises so its caller can retain the last successful snapshot.
    """
    metrics, pair_observed_at = Counter(), {}
    def read_post(path, payload):
        if path not in {"/v2/catalog/search-catalog-items", "/v2/catalog/search", "/v2/inventory/counts/batch-retrieve"}:
            raise ValueError("Explorer transport is read-only")
        enforce_square_request_policy("POST", path, payload)
        result = post(path, payload)
        metrics["POST " + path] += 1
        if not isinstance(result, dict) or result.get("errors"):
            raise ValueError("Square discovery read was incomplete")
        if path == "/v2/inventory/counts/batch-retrieve":
            observed = _iso(_now(now))
            for row in result.get("counts", []) or []:
                if isinstance(row, dict) and row.get("catalog_object_id") and row.get("location_id"):
                    pair_observed_at[(str(row["location_id"]), str(row["catalog_object_id"]))] = observed
        return result

    enforce_square_request_policy("GET", "/v2/locations")
    locations = get("/v2/locations")
    metrics["GET /v2/locations"] += 1
    if not isinstance(locations, dict) or locations.get("errors") or not isinstance(locations.get("locations"), list):
        raise ValueError("Square locations were incomplete")
    stores, gaps = resolve_locations(locations["locations"])
    if not all(store["verified"] for store in stores):
        raise ValueError("Four unambiguous active Square store mappings are required")

    def pages(path, payload, key):
        cursor, seen = None, set()
        for _ in range(256):
            request = dict(payload)
            if cursor:
                request["cursor"] = cursor
            response = read_post(path, request)
            if not isinstance(response.get(key), list):
                raise ValueError("Square catalog page was incomplete")
            yield response[key], _iso(_now(now))
            cursor = str(response.get("cursor") or "").strip()
            if not cursor:
                return
            if cursor in seen:
                raise ValueError("Repeated Square catalog cursor")
            seen.add(cursor)
        raise ValueError("Square catalog exceeded the bounded page count")

    categories, images = {}, {}
    for objects, _ in pages("/v2/catalog/search", {
        "object_types":["CATEGORY","IMAGE"], "include_deleted_objects":False, "limit":100,
    }, "objects"):
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("is_deleted") or not obj.get("id"):
                continue
            if obj.get("type") == "CATEGORY":
                categories[str(obj["id"])] = plaintext((obj.get("category_data") or {}).get("name"), 120)
            elif obj.get("type") == "IMAGE":
                images[str(obj["id"])] = safe_square_image((obj.get("image_data") or {}).get("url"))

    items = {}
    for rows, observed in pages("/v2/catalog/search-catalog-items", {"limit":100}, "items"):
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Square returned an invalid catalog item")
            item = _minimal_item(row, observed)
            if item is not None:
                items[item["id"]] = item
    if not items:
        raise ValueError("Square returned an unexpectedly empty active catalog")
    snapshot = {
        "schemaVersion":SCHEMA_VERSION, "source":"square",
        "catalogObservedAt":_iso(_now(now)), "stores":stores,
        "categories":categories, "images":images, "items":list(items.values()),
        "inventory":[], "gaps":gaps,
    }
    # The existing gateway owns pagination, explicit-pair semantics and strict
    # numeric validation. No second inventory transport is introduced here.
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    candidates = normalize_catalog(snapshot)["products"]
    variation_ids = sorted({variation["id"] for product in candidates for variation in product["variations"]})
    gateway = SquareOrderingReadGateway(post=read_post)
    counts = gateway.fetch_current_inventory_counts(
        location_ids=[store["squareLocationId"] for store in stores],
        variation_ids=variation_ids, preserve_partial=False,
    )
    inventory_observed_at = _iso(_now(now))
    snapshot["inventoryObservedAt"] = inventory_observed_at
    snapshot["inventory"] = [
        {"locationId":location_id, "variationId":variation_id, "quantity":str(record.quantity),
         "calculatedAt":_iso(record.calculated_at),
         "observedAt":pair_observed_at.get((location_id, variation_id), inventory_observed_at)}
        for (location_id, variation_id), record in sorted(counts.counts.items())
    ]
    snapshot["inventoryCoverage"] = {
        "requestedPairs":len(variation_ids)*len(stores), "observedPairs":len(snapshot["inventory"]),
        "missingPairs":len(variation_ids)*len(stores)-len(snapshot["inventory"]),
    }
    snapshot["readMetrics"] = dict(sorted(metrics.items()))
    # Defense in depth: Decimal/datetime/provider response objects never escape.
    json.dumps(snapshot, allow_nan=False)
    return snapshot


def normalize_catalog(snapshot):
    """Pure normalization; classifications here remain explicitly provisional."""
    categories = snapshot.get("categories") or {}
    images = snapshot.get("images") or {}
    groups, gaps, variation_owners = {}, [], {}
    def gap(code, item_id, variation_id=None):
        record = {"code":code, "squareItemId":item_id}
        if variation_id:
            record["squareVariationId"] = variation_id
        gaps.append(record)
    for item in sorted(snapshot.get("items", []), key=lambda row:str(row.get("id") or "")):
        item_id, item_name = str(item.get("id") or ""), str(item.get("name") or "").strip()
        if not item_id or not item_name:
            gap("missing_item_identity", item_id)
            continue
        category_names = [categories[value] for value in item.get("categoryIds", []) if categories.get(value)]
        item_attributes = item.get("customAttributes") or {}
        brand, brand_source = _brand(item_name, category_names, item_attributes)
        item_flavor = item_name
        if brand_source != "unresolved":
            item_flavor = re.sub(r"^"+re.escape(brand)+r"[\s:·(-]*", "", item_flavor, flags=re.I)
        item_flavor = _clean_label(item_flavor) or item_name
        for variation in item.get("variations", []):
            variation_id = str(variation.get("id") or "")
            if not variation_id:
                gap("missing_variation_identity", item_id)
                continue
            if variation_id in variation_owners and variation_owners[variation_id] != item_id:
                raise ValueError("One Square variation cannot belong to two items")
            variation_owners[variation_id] = item_id
            variation_name = str(variation.get("name") or "")
            type_info = resolve_type(category_names, item_name, variation_name,
                                     attributes={
                                         **{"item."+key:value for key,value in item_attributes.items()},
                                         **{"variation."+key:value for key,value in (variation.get("customAttributes") or {}).items()},
                                     })
            if type_info["issue"]:
                gap(type_info["issue"], item_id, variation_id)
                continue
            strength = resolve_strength(item_name, variation_name,
                                        item_attributes=item_attributes,
                                        variation_attributes=variation.get("customAttributes"))
            if strength["issue"] in {"conflicting_strength", "invalid_strength"}:
                gap(strength["issue"], item_id, variation_id)
                continue
            if strength["issue"]:
                gap(strength["issue"], item_id, variation_id)
            kind = type_info["type"]
            sizes = {_number(match[1]) for text in (item_name, variation_name) for match in SIZE_PATTERN.finditer(text)}
            if len(sizes) > 1:
                gap("conflicting_size", item_id, variation_id)
                continue
            size = next(iter(sizes)) if sizes else None
            size_label = (str(size) + " mL") if size is not None else ""
            variation_flavor = _clean_label(variation_name)
            if variation_flavor.lower() in {"regular", "default", "standard", ""}:
                variation_flavor = ""
            if variation_flavor and re.fullmatch(r"[\d\W]+", variation_flavor):
                variation_flavor = ""
            # Flavor-bearing variations are distinct cards, particularly devices.
            flavor = variation_flavor or item_flavor
            if variation_flavor and kind != "disposable":
                flavor = item_flavor + " · " + variation_flavor
            explicit_item_strength = bool(MG_PATTERN.search(item_name) or PERCENT_PATTERN.search(item_name))
            if explicit_item_strength and not variation_flavor and brand_source != "unresolved":
                key = ("strength-split", brand.casefold(), item_flavor.casefold(), kind, size_label)
            else:
                key = ("item", item_id, variation_flavor.casefold(), kind, size_label)
            if key not in groups:
                notes, flavor_categories, sensory, art, accent = _flavor_profile(flavor)
                image_ids = list(variation.get("imageIds") or []) + list(item.get("imageIds") or [])
                image = next((url for image_id in image_ids if (url := safe_square_image(images.get(image_id)))), None)
                product = {
                    "id":"square-"+sha256(json.dumps(key).encode()).hexdigest()[:20],
                    "squareItemId":item_id, "squareItemIds":[item_id], "brand":brand,
                    "brandSource":brand_source, "name":flavor, "catalogName":item_name,
                    "seriesName":item_flavor if variation_flavor else "",
                    "description":plaintext(item.get("description")) or
                        ("Flavor notes identified in the Square name: " + ", ".join(notes).replace("-", " ") + "."
                         if notes else "The flavor profile has not been classified yet. Ask a staff member for more detail."),
                    "type":kind, "formatLabel":{"freebase":"Freebase e-liquid","salt":"Nicotine salt","disposable":"Disposable"}[kind],
                    "sizeLabel":size_label, "notes":notes, "categories":flavor_categories,
                    "sensory":sensory, "sensorySource":"catalog-inferred",
                    "strengths":[], "variations":[], "accent":accent, "art":art,
                    "imageUrl":image, "imageSource":"square" if image else "illustration-fallback",
                    "inventorySource":"square", "classificationSource":"catalog-inferred",
                    "classificationStatus":"provisional", "classificationVersion":None,
                    "typeProvenance":type_info["evidence"],
                    "catalogReadAt":item.get("observedAt") or snapshot.get("catalogObservedAt"),
                }
                groups[key] = product
                if not image:
                    gap("missing_catalog_image", item_id)
                if brand_source == "unresolved":
                    gap("unresolved_brand", item_id)
                if not notes:
                    gap("unclassified_flavor_notes", item_id)
            product = groups[key]
            if item_id not in product["squareItemIds"]:
                product["squareItemIds"].append(item_id)
            if any(row["id"] == variation_id for row in product["variations"]):
                continue
            product["variations"].append({
                "id":variation_id, "squareItemId":item_id, "sku":str(variation.get("sku") or ""),
                "name":variation_name, "strengthMg":strength["strengthMg"],
                "strengthProvenance":strength["evidence"], "availability":{},
            })
    products = sorted(groups.values(), key=lambda product:(product["brand"].casefold(),product["name"].casefold(),product["id"]))
    for product in products:
        product["squareItemIds"].sort()
        product["squareItemId"] = product["squareItemIds"][0]
        product["strengths"] = sorted({variation["strengthMg"] for variation in product["variations"] if variation["strengthMg"] is not None})
        product["variations"].sort(key=lambda variation:(variation["strengthMg"] is None,variation["strengthMg"] or 0,variation["id"]))
    return {"products":products, "gaps":gaps}


def project_snapshot(snapshot, now=None, ttl_seconds=900):
    """Build the public read model; current stock event age is not read freshness."""
    if snapshot.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("Unsupported Square discovery snapshot version")
    if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 900:
        raise ValueError("Discovery inventory TTL must be between 1 and 900 seconds")
    current = _now(now)
    normalized = normalize_catalog(snapshot)
    products = normalized["products"]
    stores = snapshot.get("stores") or []
    inventory, conflicts = {}, set()
    for row in snapshot.get("inventory", []):
        key = (str(row.get("locationId") or ""),str(row.get("variationId") or ""))
        if key in inventory and inventory[key] != row:
            conflicts.add(key)
        else:
            inventory[key] = row
    states = Counter()
    for product in products:
        for variation in product["variations"]:
            for store in stores:
                key = (str(store.get("squareLocationId") or ""),variation["id"])
                row = inventory.get(key)
                record = {"state":"unknown", "checkedAt":None, "calculatedAt":None,
                          "validUntil":None, "reason":"not_returned"}
                if not store.get("verified") or not store.get("squareLocationId"):
                    record["reason"] = "store_mapping_unverified"
                elif key in conflicts:
                    record.update(state="check_staff", reason="conflicting_counts")
                elif row is not None:
                    observed = _date(row.get("observedAt"))
                    calculated = _date(row.get("calculatedAt"))
                    try:
                        value = Decimal(str(row.get("quantity")))
                    except (InvalidOperation, ValueError, TypeError):
                        value = Decimal("NaN")
                    if observed is None or observed > current + timedelta(seconds=60):
                        record["reason"] = "invalid_observation_time"
                    elif not value.is_finite():
                        record["reason"] = "invalid_count"
                    else:
                        until = observed + timedelta(seconds=ttl_seconds)
                        state = "available" if value > 0 else "out_of_stock" if value == 0 else "check_staff"
                        reason = "explicit_count" if value >= 0 else "negative_count"
                        if current >= until:
                            state, reason = "stale", "expired_observation"
                        elif calculated is not None and calculated > observed + timedelta(minutes=5):
                            state, reason = "check_staff", "future_stock_event"
                        record.update(state=state, reason=reason, checkedAt=_iso(observed),
                                      calculatedAt=_iso(calculated), validUntil=_iso(until))
                record["label"] = {
                    "available":"Available", "out_of_stock":"Out of stock",
                    "check_staff":"Check with staff", "stale":"Check with staff",
                    "unknown":"Availability unknown",
                }[record["state"]]
                variation["availability"][store["id"]] = record
                states[record["state"]] += 1
    gaps = list(snapshot.get("gaps") or []) + normalized["gaps"]
    gap_counts = dict(sorted(Counter(gap["code"] for gap in gaps).items()))
    public_stores = [{key:store.get(key) for key in ("id","name","shortName","verified","squareLocationId")} for store in stores]
    public = {
        "products":products, "stores":public_stores, "gaps":gaps,
        "meta":{
            "source":"square-snapshot", "inventorySource":"square", "representative":False,
            "catalogReadAt":snapshot.get("catalogObservedAt"),
            "inventoryReadAt":snapshot.get("inventoryObservedAt"),
            "catalogObservedAt":snapshot.get("catalogObservedAt"),
            "inventoryObservedAt":snapshot.get("inventoryObservedAt"),
            "projectedAt":_iso(current), "inventoryTtlSeconds":ttl_seconds,
            "classificationSource":"catalog-inferred", "classificationStatus":"provisional",
            "productCount":len(products),
            "variationCount":sum(len(product["variations"]) for product in products),
            "availabilityCounts":dict(sorted(states.items())), "gapCounts":gap_counts,
            "note":"Actual Square catalog and inventory observations. Flavor classifications are provisional name-derived suggestions.",
        },
    }
    json.dumps(public, allow_nan=False)
    return public
