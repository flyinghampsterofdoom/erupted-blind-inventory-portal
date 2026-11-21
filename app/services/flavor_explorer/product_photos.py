"""Staff-review product photos; never modify Square, Compatibility or classification.

Only the explicitly initialized feature authority is written. Raster bytes and
attributed assignment history are retained in that same database, with no files,
URL fetching, licensing inference or customer-publication decision.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
import re
import uuid
import warnings
from urllib.parse import parse_qsl, urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import (MetaData, Table, Column, Integer, String, Text, DateTime,
    LargeBinary, JSON, ForeignKey, ForeignKeyConstraint, UniqueConstraint,
    CheckConstraint, Index, select, insert, func, inspect)

VERSION=1
NAMES=("fi_product_photo_schema","fi_product_photo_assets",
       "fi_product_photo_operations","fi_product_photo_events","fi_product_photo_priority_schema")
BASE_NAMES=NAMES[:4]
PRIORITY_VERSION=1
SOURCE_KINDS=("manufacturer","own","legacy")
MAX_UPLOAD=2*1024*1024
MAX_PIXELS=12_000_000
MAX_EDGE=1200
MEDIA_PREFIX="/flavor-explorer/media/photos/"
MODES={"catalog","custom","hidden"}
FIELDS={"sourcePageUrl","sourceImageUrl","sourceLabel","rightsNote"}

def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)

def _text(value,limit,*,empty=True):
    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 and c not in "\n\t" for c in value):
        raise ValueError("Invalid product photo text.")
    value=value.strip()
    if not empty and not value:raise ValueError("A current product identity is required.")
    return value

def _metadata_values(value):
    if value is None:value={}
    if not isinstance(value,dict) or set(value)-FIELDS:raise ValueError("Unsupported product photo metadata.")
    result={key:_text(value.get(key,""),4000 if key=="rightsNote" else 300 if key=="sourceLabel" else 2048)
            for key in sorted(FIELDS)}
    for key in ("sourcePageUrl","sourceImageUrl"):
        if not result[key]:continue
        try:
            url=urlsplit(result[key])
            if (url.scheme!="https" or not url.hostname or url.username or url.password
                    or url.port not in (None,443)
                    or any(re.search(r"token|signature|credential|authorization|x-amz|key",part,re.I)
                           for part,_ in parse_qsl(url.query))):
                raise ValueError()
        except ValueError:raise ValueError("Photo source references must be ordinary public HTTPS URLs.")
    return result

def _product(value):
    if not isinstance(value,dict):raise ValueError("A current E-liquid product is required.")
    if value.get("type") not in {"freebase","salt","disposable"}:
        raise ValueError("Photo management is limited to E-liquid Explorer products.")
    ids=value.get("squareVariationIds")
    if ids is None:
        variants=value.get("variations")
        if not isinstance(variants,list):raise ValueError("Exact current Square variations are required.")
        ids=[row.get("id") if isinstance(row,dict) else None for row in variants]
    if not isinstance(ids,list) or not 1<=len(ids)<=10000:raise ValueError("Exact current Square variations are required.")
    clean=[_text(identity,255,empty=False) for identity in ids]
    if len(set(clean))!=len(clean):raise ValueError("Square variation identities must be distinct.")
    return {"id":_text(value.get("id"),255,empty=False),
            "brand":_text(value.get("brand",""),300),
            "name":_text(value.get("name",""),1000,empty=False),
            "type":value["type"],"squareVariationIds":sorted(clean)}

def _catalog_url(product):
    from app.services.flavor_explorer.square_catalog import safe_square_image
    return safe_square_image(product.get("catalogImageUrl",product.get("imageUrl")))

def _url(asset_id):return MEDIA_PREFIX+asset_id+".webp" if asset_id else None

def _canonical(data):
    if not isinstance(data,bytes) or not 0<len(data)<=MAX_UPLOAD:
        raise ValueError("Upload a raster image no larger than 2 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error",Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as opened:
                if opened.format not in {"JPEG","PNG","WEBP"} or getattr(opened,"n_frames",1)!=1:
                    raise ValueError("Use a single JPEG, PNG or WebP product photo.")
                width,height=opened.size
                if width<1 or height<1 or width*height>MAX_PIXELS:
                    raise ValueError("Product photos must contain at most 12 megapixels.")
                image=ImageOps.exif_transpose(opened)
                image.thumbnail((MAX_EDGE,MAX_EDGE),Image.Resampling.LANCZOS)
                alpha=image.mode in {"RGBA","LA"} or "transparency" in image.info
                image=image.convert("RGBA" if alpha else "RGB")
                output=BytesIO()
                image.save(output,"WEBP",quality=85,method=4,exif=b"",icc_profile=b"",xmp=b"")
                encoded=output.getvalue()
                if len(encoded)>MAX_UPLOAD:raise ValueError("The normalized photo is too large.")
                return {"id":hashlib.sha256(encoded).hexdigest(),"data":encoded,
                        "mime":"image/webp","width":image.width,"height":image.height,
                        "byte_size":len(encoded)}
    except (UnidentifiedImageError,OSError,SyntaxError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError("That file could not be decoded as a safe product photo.") from None


def _retained_canonical(value):
    """Validate trusted retained bytes without a second lossy encoding pass."""
    if not isinstance(value,dict):raise ValueError("A reviewed canonical photo asset is required.")
    data=value.get("data")
    if isinstance(data,(bytearray,memoryview)):data=bytes(data)
    if not isinstance(data,bytes) or not 0<len(data)<=MAX_UPLOAD:raise ValueError("Invalid retained photo bytes.")
    sha=hashlib.sha256(data).hexdigest()
    if value.get("id")!=sha or value.get("mime")!="image/webp" or value.get("byte_size")!=len(data):
        raise ValueError("Retained photo identity does not match its exact bytes.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error",Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if (image.format!="WEBP" or getattr(image,"n_frames",1)!=1
                        or image.width<1 or image.height<1 or max(image.size)>MAX_EDGE
                        or type(value.get("width")) is not int or type(value.get("height")) is not int
                        or image.size!=(value["width"],value["height"])):
                    raise ValueError("Invalid retained canonical photo dimensions.")
                image.load()
    except (UnidentifiedImageError,OSError,SyntaxError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise ValueError("Retained photo bytes could not be decoded safely.") from None
    return {"id":sha,"data":data,"mime":"image/webp","width":value["width"],
            "height":value["height"],"byte_size":len(data)}

def _metadata(storage):
    m=MetaData()
    for name in ("stores","principals"):storage.table(name).to_metadata(m)
    Table(NAMES[0],m,Column("id",Integer,primary_key=True),
          Column("version",Integer,nullable=False),CheckConstraint("id=1"))
    Table(NAMES[1],m,Column("id",String(64),primary_key=True),
          Column("data",LargeBinary,nullable=False),Column("mime",String(40),nullable=False),
          Column("width",Integer,nullable=False),Column("height",Integer,nullable=False),
          Column("byte_size",Integer,nullable=False),
          CheckConstraint("length(id)=64 AND mime='image/webp'"),
          CheckConstraint("width BETWEEN 1 AND 1200 AND height BETWEEN 1 AND 1200"),
          CheckConstraint("byte_size=length(data) AND byte_size BETWEEN 1 AND 2097152"))
    Table(NAMES[2],m,Column("id",String(36),primary_key=True),
          Column("actor_id",ForeignKey("principals.id",ondelete="RESTRICT"),nullable=False),
          Column("created_at",DateTime(timezone=True),nullable=False),
          Column("product",JSON,nullable=False),Column("mode",String(10),nullable=False),
          Column("asset_id",ForeignKey(NAMES[1]+".id",ondelete="RESTRICT")),
          Column("attribution",JSON,nullable=False),Column("restored_from_event_id",Integer),
          Column("source_kind",String(16)),
          Column("candidate_action",String(16),nullable=False,server_default="legacy"),
          CheckConstraint("mode IN ('catalog','custom','hidden')"),
          CheckConstraint("(mode='custom' AND asset_id IS NOT NULL) OR (mode<>'custom' AND asset_id IS NULL)"),
          CheckConstraint("source_kind IS NULL OR source_kind IN ('manufacturer','own','legacy')"),
          CheckConstraint("candidate_action IN ('legacy','assign','remove','policy')"),
          CheckConstraint("(candidate_action='legacy' AND source_kind IS NULL) OR "
                          "(candidate_action='assign' AND source_kind IS NOT NULL AND mode='custom' AND asset_id IS NOT NULL) OR "
                          "(candidate_action='remove' AND source_kind IS NOT NULL AND mode='catalog' AND asset_id IS NULL) OR "
                          "(candidate_action='policy' AND source_kind IS NULL AND mode IN ('catalog','hidden') AND asset_id IS NULL)"))
    events=Table(NAMES[3],m,Column("id",Integer,primary_key=True,autoincrement=True),
          Column("operation_id",ForeignKey(NAMES[2]+".id",ondelete="RESTRICT"),nullable=False),
          Column("square_variation_id",Text,nullable=False),Column("before_event_id",Integer),
          Column("policy_mode",String(10)),
          CheckConstraint("policy_mode IS NULL OR policy_mode IN ('catalog','custom','hidden')"),
          UniqueConstraint("id","square_variation_id"),
          UniqueConstraint("operation_id","square_variation_id"),
          ForeignKeyConstraint(["before_event_id","square_variation_id"],
              [NAMES[3]+".id",NAMES[3]+".square_variation_id"],ondelete="RESTRICT"),
          CheckConstraint("before_event_id IS NULL OR before_event_id<id"))
    Index("fi_product_photo_variation_latest",events.c.square_variation_id,events.c.id)
    Table(NAMES[4],m,Column("id",Integer,primary_key=True),Column("version",Integer,nullable=False),CheckConstraint("id=1"))
    return m


def _priority(connection,metadata):
    names=set(inspect(connection).get_table_names())
    columns={row["name"] for row in inspect(connection).get_columns(NAMES[2])} if NAMES[2] in names else set()
    event_columns={row["name"] for row in inspect(connection).get_columns(NAMES[3])} if NAMES[3] in names else set()
    if NAMES[4] not in names:
        if {"source_kind","candidate_action"}.intersection(columns) or "policy_mode" in event_columns:
            raise ValueError("Product photo priority schema is incomplete.")
        return False
    if not {"source_kind","candidate_action"}<=columns or "policy_mode" not in event_columns:
        raise ValueError("Product photo priority schema is incomplete.")
    rows=connection.execute(select(metadata.tables[NAMES[4]])).mappings().all()
    if len(rows)!=1 or rows[0]["id"]!=1 or rows[0]["version"]!=PRIORITY_VERSION:
        raise ValueError("Unsupported product photo priority schema.")
    return True

def _feature(connection,metadata):
    names=set(inspect(connection).get_table_names());present=names.intersection(BASE_NAMES)
    if not present:
        if NAMES[4] in names:raise ValueError("Product photo schema is incomplete.")
        return False
    if present!=set(BASE_NAMES):raise ValueError("Product photo schema is incomplete.")
    rows=connection.execute(select(metadata.tables[NAMES[0]])).mappings().all()
    if len(rows)!=1 or rows[0]["id"]!=1 or rows[0]["version"]!=VERSION:
        raise ValueError("Unsupported product photo schema.")
    _priority(connection,metadata)
    return True

SQLITE_PRIORITY_OPERATION_GUARD="""CREATE TRIGGER IF NOT EXISTS fi_product_photo_priority_operation
BEFORE INSERT ON fi_product_photo_operations BEGIN
SELECT CASE WHEN NOT (
 (NEW.candidate_action='legacy' AND NEW.source_kind IS NULL) OR
 (NEW.candidate_action='assign' AND NEW.source_kind IS NOT NULL AND NEW.source_kind IN ('manufacturer','own','legacy') AND NEW.mode='custom' AND NEW.asset_id IS NOT NULL) OR
 (NEW.candidate_action='remove' AND NEW.source_kind IS NOT NULL AND NEW.source_kind IN ('manufacturer','own','legacy') AND NEW.mode='catalog' AND NEW.asset_id IS NULL) OR
 (NEW.candidate_action='policy' AND NEW.source_kind IS NULL AND NEW.mode IN ('catalog','hidden') AND NEW.asset_id IS NULL)
) THEN RAISE(ABORT,'Invalid product photo candidate action') END; END"""

SQLITE_PRIORITY_EVENT_GUARD="""CREATE TRIGGER IF NOT EXISTS fi_product_photo_priority_event
BEFORE INSERT ON fi_product_photo_events BEGIN
SELECT CASE WHEN NOT EXISTS (
 SELECT 1 FROM fi_product_photo_operations AS o WHERE o.id=NEW.operation_id AND (
  (o.candidate_action='legacy' AND NEW.policy_mode IS NULL) OR
  (o.candidate_action='assign' AND NEW.policy_mode='custom') OR
  (o.candidate_action='policy' AND NEW.policy_mode=o.mode) OR
  (o.candidate_action='remove' AND NEW.policy_mode=COALESCE(
   (SELECT COALESCE(e.policy_mode,p.mode) FROM fi_product_photo_events AS e
    JOIN fi_product_photo_operations AS p ON p.id=e.operation_id WHERE e.id=NEW.before_event_id),'catalog'))
 )
) THEN RAISE(ABORT,'Invalid product photo policy transition') END; END"""

def initialize_product_photos(storage):
    """Explicit SQLite setup/upgrade only. Historical rows and bytes never change."""
    if storage.engine.dialect.name!="sqlite":raise ValueError("Production photos require a reviewed migration.")
    m=_metadata(storage)
    with storage._write() as c:
        exists=_feature(c,m)
        for name in BASE_NAMES:m.tables[name].create(c,checkfirst=True)
        if not exists:c.execute(insert(m.tables[NAMES[0]]).values(id=1,version=VERSION))
        columns={row["name"] for row in inspect(c).get_columns(NAMES[2])}
        if "source_kind" not in columns:c.exec_driver_sql("ALTER TABLE fi_product_photo_operations ADD COLUMN source_kind VARCHAR(16) CHECK(source_kind IS NULL OR source_kind IN ('manufacturer','own','legacy'))")
        if "candidate_action" not in columns:c.exec_driver_sql("ALTER TABLE fi_product_photo_operations ADD COLUMN candidate_action VARCHAR(16) NOT NULL DEFAULT 'legacy' CHECK(candidate_action IN ('legacy','assign','remove','policy'))")
        event_columns={row["name"] for row in inspect(c).get_columns(NAMES[3])}
        if "policy_mode" not in event_columns:c.exec_driver_sql("ALTER TABLE fi_product_photo_events ADD COLUMN policy_mode VARCHAR(10) CHECK(policy_mode IS NULL OR policy_mode IN ('custom','catalog','hidden'))")
        m.tables[NAMES[4]].create(c,checkfirst=True)
        if not c.scalar(select(func.count()).select_from(m.tables[NAMES[4]])):
            c.execute(insert(m.tables[NAMES[4]]).values(id=1,version=PRIORITY_VERSION))
        for name in NAMES:
            for action in ("UPDATE","DELETE"):
                c.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS "+name+"_no_"+action.lower()
                    +" BEFORE "+action+" ON "+name
                    +" BEGIN SELECT RAISE(ABORT,'Product photo history is append-only'); END")
        c.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS fi_product_photo_admin_required
            BEFORE INSERT ON fi_product_photo_operations BEGIN
            SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.actor_id
                AND active=1 AND role IN ('ADMIN','MANAGER'))
                THEN RAISE(ABORT,'Active administrator required') END; END""")
        c.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS fi_product_photo_head_required
            BEFORE INSERT ON fi_product_photo_events BEGIN
            SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM fi_product_photo_operations AS o,
                json_each(o.product,'$.squareVariationIds') AS v
                WHERE o.id=NEW.operation_id AND v.value=NEW.square_variation_id)
                THEN RAISE(ABORT,'Variation outside recorded photo action') END;
            SELECT CASE WHEN COALESCE(NEW.before_event_id,0)<>COALESCE(
                (SELECT MAX(id) FROM fi_product_photo_events WHERE square_variation_id=NEW.square_variation_id),0)
                THEN RAISE(ABORT,'Product photo head changed') END; END""")
        c.exec_driver_sql(SQLITE_PRIORITY_OPERATION_GUARD)
        c.exec_driver_sql(SQLITE_PRIORITY_EVENT_GUARD)
    return storage

def _empty_head():
    return {"id":0,"mode":"catalog","slots":{kind:None for kind in SOURCE_KINDS},
            "slotHeads":{kind:0 for kind in SOURCE_KINDS}}

def _action(row):
    action=row.get("candidate_action") or "legacy"
    if action=="legacy":return ("assign","legacy") if row["mode"]=="custom" else ("policy",None)
    return action,row.get("source_kind")

def _heads(c,m,ids,priority=None):
    if priority is None:priority=_priority(c,m)
    result={identity:_empty_head() for identity in ids};seen={identity:set() for identity in ids}
    events=m.tables[NAMES[3]];ops=m.tables[NAMES[2]]
    columns=[events.c.id,events.c.square_variation_id,ops.c.mode,ops.c.asset_id,ops.c.attribution]
    if priority:columns.extend([events.c.policy_mode,ops.c.source_kind,ops.c.candidate_action])
    for offset in range(0,len(ids),800):
        rows=c.execute(select(*columns).join(ops,ops.c.id==events.c.operation_id)
            .where(events.c.square_variation_id.in_(ids[offset:offset+800])).order_by(events.c.id.desc())).mappings()
        for raw in rows:
            row=dict(raw);identity=row["square_variation_id"];head=result[identity]
            if not head["id"]:head.update(id=row["id"],mode=row.get("policy_mode") or row["mode"])
            action,kind=_action(row)
            if action=="policy":continue
            if kind not in SOURCE_KINDS:raise ValueError("Invalid retained photo source kind.")
            if kind in seen[identity]:continue
            seen[identity].add(kind);head["slotHeads"][kind]=row["id"]
            if action=="assign":
                head["slots"][kind]={"eventId":row["id"],"assetId":row["asset_id"],"url":_url(row["asset_id"]),
                    **deepcopy(row["attribution"])}
            elif action!="remove":raise ValueError("Invalid retained photo candidate action.")
    return result

def _token(product,heads):
    content={"productId":product["id"],"heads":[[identity,heads[identity]["id"],
        [[kind,heads[identity]["slotHeads"][kind]] for kind in SOURCE_KINDS]] for identity in product["squareVariationIds"]]}
    return hashlib.sha256(_json(content).encode()).hexdigest()

def _original_url(product):
    value=product.get("originalImageUrl")
    return value if isinstance(value,str) and re.fullmatch(r"/flavor-explorer/media/originals/[1-9][0-9]{0,18}",value) else None

def _fallback(product):
    original=_original_url(product)
    return (original,"own",None,_metadata_values(None)) if original else (_catalog_url(product),"catalog",None,_metadata_values(None))

def _effective(product,head):
    if head["mode"]=="hidden":return None,None,None,_metadata_values(None)
    if head["mode"]=="catalog":return _fallback(product)
    for kind in ("manufacturer","own"):
        slot=head["slots"][kind]
        if slot:return slot["url"],kind,slot["assetId"],{key:slot.get(key,"") for key in FIELDS}
    if _original_url(product):return _fallback(product)
    legacy=head["slots"]["legacy"]
    if legacy:return legacy["url"],"legacy",legacy["assetId"],{key:legacy.get(key,"") for key in FIELDS}
    return _fallback(product)

def _resolved(product,clean,heads):
    modes={heads[identity]["mode"] for identity in clean["squareVariationIds"]}
    values=[_effective(product,heads[identity]) for identity in clean["squareVariationIds"]]
    if len(modes)!=1 or len({(value[0],value[1],value[2]) for value in values})!=1:
        return "mixed",None,None,None,_metadata_values(None)
    url,kind,asset,metadata=values[0]
    if len({_json(value[3]) for value in values})!=1:metadata=_metadata_values(None)
    return next(iter(modes)),url,kind,asset,metadata

def _candidate(clean,heads,kind):
    slots=[heads[identity]["slots"][kind] for identity in clean["squareVariationIds"]]
    active=[slot for slot in slots if slot]
    if not active:return None
    if len(active)!=len(slots) or len({slot["assetId"] for slot in active})!=1:
        return {"sourceKind":kind,"mixed":True,"activeVariationCount":len(active),"variationCount":len(slots),
                "assetId":None,"url":None,"eventId":None,**_metadata_values(None)}
    metadata={key:active[0].get(key,"") for key in FIELDS}
    if len({_json({key:slot.get(key,"") for key in FIELDS}) for slot in active})!=1:metadata=_metadata_values(None)
    return {"sourceKind":kind,"mixed":False,"activeVariationCount":len(active),"variationCount":len(slots),
            "eventId":max(slot["eventId"] for slot in active),"assetId":active[0]["assetId"],"url":active[0]["url"],**metadata}

def _signature(head):
    return [head["mode"],[[kind,None if head["slots"][kind] is None else
        [head["slots"][kind]["assetId"],{key:head["slots"][kind].get(key,"") for key in FIELDS}]] for kind in SOURCE_KINDS]]

def _stamp(value):
    if value.tzinfo is None:value=value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()

class ProductPhotos:
    def __init__(self,storage):
        self.storage=storage;self.metadata=_metadata(storage)

    def _history(self,c,clean,priority):
        events=self.metadata.tables[NAMES[3]];ops=self.metadata.tables[NAMES[2]]
        relevant=select(events.c.operation_id,func.max(events.c.id).label("event_id")).where(
            events.c.square_variation_id.in_(clean["squareVariationIds"])).group_by(events.c.operation_id).subquery()
        columns=[ops.c.id,ops.c.actor_id,ops.c.created_at,ops.c.product,ops.c.mode,ops.c.asset_id,
                 ops.c.attribution,ops.c.restored_from_event_id,relevant.c.event_id]
        if priority:columns.extend([ops.c.source_kind,ops.c.candidate_action])
        rows=[dict(row) for row in c.execute(select(*columns).join(relevant,relevant.c.operation_id==ops.c.id)
            .order_by(relevant.c.event_id.desc()).limit(50)).mappings()]
        policies={}
        if priority and rows:
            for op_id,policy in c.execute(select(events.c.operation_id,events.c.policy_mode).where(
                    events.c.operation_id.in_([row["id"] for row in rows]),
                    events.c.square_variation_id.in_(clean["squareVariationIds"]))):
                policies.setdefault(op_id,set()).add(policy)
        result=[]
        for row in rows:
            action,kind=_action(row);modes={value or row["mode"] for value in policies.get(row["id"],{None})}
            mode=next(iter(modes)) if len(modes)==1 else "mixed"
            result.append({"eventId":row["event_id"],"operationId":row["id"],"actorId":row["actor_id"],
                "at":_stamp(row["created_at"]),"mode":mode,"policyMode":mode,"action":action,"sourceKind":kind,
                "assetId":row["asset_id"] if action=="assign" else None,
                "url":_url(row["asset_id"]) if action=="assign" else None,
                "restoredFromEventId":row["restored_from_event_id"],
                "revertible":sorted(row["product"]["squareVariationIds"])==clean["squareVariationIds"],
                **deepcopy(row["attribution"])})
        return result

    def _describe(self,c,original,clean,ready):
        priority=_priority(c,self.metadata) if ready else False
        heads=_heads(c,self.metadata,clean["squareVariationIds"],priority) if ready else {identity:_empty_head() for identity in clean["squareVariationIds"]}
        mode,url,kind,asset,attribution=_resolved(original,clean,heads)
        return {"productId":clean["id"],"stateToken":_token(clean,heads),"mode":mode,"assetId":asset,"url":url,
            "effectiveSourceKind":kind,"candidates":{source:_candidate(clean,heads,source) for source in SOURCE_KINDS},
            "originalFallback":{"url":_original_url(original),"sourceKind":"own"} if _original_url(original) else None,
            "catalogUrl":_catalog_url(original),"history":self._history(c,clean,priority) if ready else [],**attribution}

    def describe(self,product):
        clean=_product(product)
        with self.storage.engine.connect() as c:return self._describe(c,product,clean,_feature(c,self.metadata))

    def asset(self,sha):
        if not isinstance(sha,str) or not re.fullmatch(r"[0-9a-f]{64}",sha):raise ValueError("Product photo is unavailable.")
        with self.storage.engine.connect() as c:
            if not _feature(c,self.metadata):raise ValueError("Product photo is unavailable.")
            row=c.execute(select(self.metadata.tables[NAMES[1]]).where(self.metadata.tables[NAMES[1]].c.id==sha)).mappings().first()
            if not row:raise ValueError("Product photo is unavailable.")
            data=bytes(row["data"])
            if len(data)!=row["byte_size"] or hashlib.sha256(data).hexdigest()!=sha:raise ValueError("Product photo bytes are unavailable.")
            return {**dict(row),"data":data}

    def classify_retained(self,*,actor_id,product,expected_token,event_id,source_kind,current_product_loader=None):
        """Trusted reviewed import only; never exposed as a browser source-claim shortcut."""
        if source_kind not in {"manufacturer","own"}:raise ValueError("Choose an explicit reviewed photo source.")
        return self.apply(actor_id=actor_id,product=product,expected_token=expected_token,
            trusted_source_event_id=event_id,source_kind=source_kind,current_product_loader=current_product_loader)

    def apply_canonical(self,*,actor_id,product,expected_token,asset,metadata=None,source_kind="manufacturer",
                        current_product_loader=None):
        """Trusted non-HTTP custody import. Preserve reviewed SHA and original bytes."""
        if source_kind not in {"manufacturer","own"}:raise ValueError("Choose an explicit reviewed photo source.")
        with self.storage.engine.connect() as c:self.storage._principal(c,actor_id,admin=True)
        canonical=_retained_canonical(asset)
        return self._apply(actor_id=actor_id,product=product,expected_token=expected_token,mode="custom",
            metadata=metadata,source_kind=source_kind,current_product_loader=current_product_loader,
            canonical_asset=canonical)

    def apply(self,*,actor_id,product,expected_token,mode="catalog",data=None,metadata=None,restore_event_id=None,
              current_product_loader=None,source_kind=None,action=None,trusted_source_event_id=None):
        return self._apply(actor_id=actor_id,product=product,expected_token=expected_token,mode=mode,data=data,
            metadata=metadata,restore_event_id=restore_event_id,current_product_loader=current_product_loader,
            source_kind=source_kind,action=action,trusted_source_event_id=trusted_source_event_id)

    def _apply(self,*,actor_id,product,expected_token,mode="catalog",data=None,metadata=None,restore_event_id=None,
              current_product_loader=None,source_kind=None,action=None,trusted_source_event_id=None,canonical_asset=None):
        clean=_product(product)
        if not isinstance(expected_token,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_token):
            raise ValueError("Refresh the current product photo before changing it.")
        retained=restore_event_id if restore_event_id is not None else trusted_source_event_id
        if retained is not None and (type(retained) is not int or retained<=0):raise ValueError("Choose a retained product photo event.")
        if restore_event_id is not None and trusted_source_event_id is not None:raise ValueError("Choose one retained photo action.")
        if action not in {None,"remove_candidate"}:raise ValueError("Choose a valid product photo action.")
        if source_kind is not None and source_kind not in {"manufacturer","own"}:raise ValueError("Choose Manufacturer photo or Our photo.")
        with self.storage.engine.connect() as c:self.storage._principal(c,actor_id,admin=True)
        if restore_event_id is not None and (source_kind is not None or action is not None):raise ValueError("Retained photo source and action cannot be replaced.")
        if retained is not None:
            if data is not None or metadata is not None or action is not None:raise ValueError("A photo revert cannot replace its retained bytes or source.")
            asset=None;attribution=None
        elif action=="remove_candidate":
            if source_kind is None or data is not None or metadata is not None:raise ValueError("Choose the photo candidate to remove without replacing its bytes or source.")
            asset=None;attribution=_metadata_values(None)
        else:
            if mode not in MODES:raise ValueError("Choose a custom photo, hide it, or restore the catalog photo.")
            if mode!="custom" and source_kind is not None:raise ValueError("Only uploaded photos have a source kind.")
            attribution=_metadata_values(metadata)
            if mode=="custom":asset=canonical_asset if canonical_asset is not None else _canonical(data)
            else:
                if data is not None:raise ValueError("Only custom photos accept uploaded image bytes.")
                asset=None
        ops=self.metadata.tables[NAMES[2]];events=self.metadata.tables[NAMES[3]];assets=self.metadata.tables[NAMES[1]]
        with self.storage._write() as c:
            self.storage._principal(c,actor_id,admin=True)
            if not _feature(c,self.metadata) or not _priority(c,self.metadata):raise ValueError("Product photo priority has not been initialized.")
            if current_product_loader is not None:
                current=current_product_loader()
                if current is None:raise ValueError("The current catalog product or variation set changed. Refresh before applying.")
                fresh=_product(current)
                if any(fresh[key]!=clean[key] for key in ("id","type","squareVariationIds")):
                    raise ValueError("The current catalog product or variation set changed. Refresh before applying.")
                product=current;clean=fresh
            heads=_heads(c,self.metadata,clean["squareVariationIds"],True)
            if _token(clean,heads)!=expected_token:raise ValueError("The product photo or current variation set changed. Refresh before applying.")
            actual_kind=source_kind or "legacy"
            if retained is not None:
                row=c.execute(select(ops).join(events,events.c.operation_id==ops.c.id).where(events.c.id==retained)).mappings().first()
                if not row or sorted(row["product"]["squareVariationIds"])!=clean["squareVariationIds"]:
                    raise ValueError("That retained photo belongs to a different exact variation set.")
                candidate_action,actual_kind=_action(dict(row))
                if trusted_source_event_id is not None:
                    if candidate_action!="assign" or not row["asset_id"]:raise ValueError("Choose a retained raster photo.")
                    candidate_action="assign";actual_kind=source_kind
                attribution=deepcopy(row["attribution"]);asset_id=row["asset_id"] if candidate_action=="assign" else None
                mode="custom" if candidate_action=="assign" else row["mode"] if candidate_action=="policy" else "catalog"
            elif action=="remove_candidate":candidate_action="remove";asset_id=None;mode="catalog"
            elif mode=="custom":candidate_action="assign";asset_id=asset["id"]
            else:candidate_action="policy";actual_kind=None;asset_id=None
            future=deepcopy(heads)
            for identity,head in future.items():
                if candidate_action=="assign":
                    head["mode"]="custom";head["slots"][actual_kind]={"assetId":asset_id,**deepcopy(attribution)}
                elif candidate_action=="remove":head["slots"][actual_kind]=None
                else:head["mode"]=mode
            changed=sum(_signature(heads[identity])!=_signature(future[identity]) for identity in heads)
            if asset and not c.scalar(select(assets.c.id).where(assets.c.id==asset_id)):c.execute(insert(assets).values(**asset))
            operation_id=str(uuid.uuid4())
            c.execute(insert(ops).values(id=operation_id,actor_id=actor_id,created_at=datetime.now(timezone.utc),
                product=clean,mode=mode,asset_id=asset_id,attribution=attribution,restored_from_event_id=retained,
                source_kind=actual_kind,candidate_action=candidate_action))
            for identity in clean["squareVariationIds"]:
                c.execute(insert(events).values(operation_id=operation_id,square_variation_id=identity,
                    before_event_id=heads[identity]["id"] or None,policy_mode=future[identity]["mode"]))
            return {**self._describe(c,product,clean,True),"operationId":operation_id,"changedCount":changed}

    def overlay(self,products):
        if not isinstance(products,list):raise ValueError("A product catalog is required.")
        cleaned=[_product(product) for product in products]
        ids=sorted({identity for product in cleaned for identity in product["squareVariationIds"]})
        with self.storage.engine.connect() as c:
            ready=_feature(c,self.metadata);priority=_priority(c,self.metadata) if ready else False
            heads=_heads(c,self.metadata,ids,priority) if ready else {identity:_empty_head() for identity in ids}
            projected=[]
            for original,clean in zip(products,cleaned):
                mode,url,kind,_asset,_attribution=_resolved(original,clean,heads)
                source="staff-photo" if url and (kind in {"manufacturer","own","legacy"}) else "square" if url else "illustration-fallback"
                projected.append((url,source,mode))
        for product,(url,source,mode) in zip(products,projected):product.update(imageUrl=url,imageSource=source,photoMode=mode)
        return products
