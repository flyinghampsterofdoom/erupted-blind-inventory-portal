"""Audited customer discovery visibility in the existing FI database.

These records curate only the customer E-liquid projection. Exact Square
variation identities remain read-only facts; classifications, taxonomy, stock,
Compatibility records and staff catalog access are never changed here.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib,json,uuid
from sqlalchemy import (
    MetaData,Table,Column,Integer,String,Text,Boolean,DateTime,JSON,ForeignKey,
    ForeignKeyConstraint,UniqueConstraint,CheckConstraint,Index,select,insert,func,inspect)

FEATURE_VERSION=1
NAMES=("fi_discovery_schema","fi_discovery_operations","fi_discovery_changes")
MAX_PRODUCTS=500
MAX_VARIATIONS=10000

def _json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False)
def _hash(value):return hashlib.sha256(_json(value).encode()).hexdigest()
def _now():return datetime.now(timezone.utc)
def _date(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace("Z","+00:00"))
    if not isinstance(value,datetime) or value.tzinfo is None:raise ValueError("A timezone-aware visibility timestamp is required.")
    return value.astimezone(timezone.utc)
def _text(value,limit,empty=False):
    if not isinstance(value,str) or len(value)>limit or any(ord(c)<32 for c in value) or (not empty and not value.strip()):
        raise ValueError("Visibility selection contains an invalid identity or label.")
    return value
def _ids(values):
    if not isinstance(values,list) or len(values)>MAX_VARIATIONS:raise ValueError("Choose a bounded list of exact Square variations.")
    clean=[_text(value,255) for value in values]
    if len(set(clean))!=len(clean):raise ValueError("Square variation selections must be distinct.")
    return sorted(clean)
def _products(values):
    if not isinstance(values,list) or not 1<=len(values)<=MAX_PRODUCTS:
        raise ValueError("Choose one to 500 customer catalog products.")
    result=[];seen_products=set();seen_variations=set()
    for product in values:
        if not isinstance(product,dict):raise ValueError("A current customer product selection is required.")
        identity=_text(product.get("id"),255);variations=_ids(product.get("squareVariationIds"))
        if not variations or identity in seen_products or seen_variations.intersection(variations):
            raise ValueError("Choose distinct current products and their complete variation sets.")
        seen_products.add(identity);seen_variations.update(variations)
        result.append({"id":identity,"brand":_text(product.get("brand",""),300,True),
            "name":_text(product.get("name",""),1000),"type":_text(product.get("type",""),50,True),
            "squareVariationIds":variations})
    if len(seen_variations)>MAX_VARIATIONS:raise ValueError("Too many selected Square variations.")
    return sorted(result,key=lambda row:row["id"])

def _metadata(storage):
    metadata=MetaData()
    for name in ("stores","principals"):storage.table(name).to_metadata(metadata)
    Table(NAMES[0],metadata,Column("id",Integer,primary_key=True),
        Column("version",Integer,nullable=False),CheckConstraint("id=1"))
    operations=Table(NAMES[1],metadata,
        Column("id",String(36),primary_key=True),Column("preview_id",String(36),nullable=False,unique=True),
        Column("actor_id",ForeignKey("principals.id",ondelete="RESTRICT"),nullable=False),
        Column("excluded",Boolean,nullable=False),Column("reason",Text,nullable=False),
        Column("plan_fingerprint",String(64),nullable=False),
        Column("products",JSON,nullable=False),Column("created_at",DateTime(timezone=True),nullable=False),
        CheckConstraint("length(plan_fingerprint)=64"))
    changes=Table(NAMES[2],metadata,
        Column("id",Integer,primary_key=True,autoincrement=True),
        Column("operation_id",ForeignKey(NAMES[1]+".id",ondelete="RESTRICT"),nullable=False),
        Column("square_variation_id",Text,nullable=False),
        Column("before_event_id",Integer),Column("before_excluded",Boolean,nullable=False),
        Column("excluded",Boolean,nullable=False),
        UniqueConstraint("id","square_variation_id"),
        UniqueConstraint("operation_id","square_variation_id"),
        ForeignKeyConstraint(["before_event_id","square_variation_id"],
            [NAMES[2]+".id",NAMES[2]+".square_variation_id"],ondelete="RESTRICT"),
        CheckConstraint("excluded<>before_excluded"),
        CheckConstraint("before_event_id IS NULL OR before_event_id<id"))
    Index("fi_discovery_variation_latest",changes.c.square_variation_id,changes.c.id)
    return metadata

def _feature(connection,metadata):
    present=set(inspect(connection).get_table_names()).intersection(NAMES)
    if not present:return False
    if present!=set(NAMES):raise ValueError("Discovery visibility schema is incomplete.")
    rows=connection.execute(select(metadata.tables[NAMES[0]])).mappings().all()
    if len(rows)!=1 or rows[0]["id"]!=1 or rows[0]["version"]!=FEATURE_VERSION:
        raise ValueError("Unsupported discovery visibility schema.")
    return True

def initialize_discovery(storage):
    """Explicit additive developmental setup; never production runtime DDL."""
    if storage.engine.dialect.name!="sqlite":raise ValueError("Production discovery visibility requires a reviewed migration.")
    metadata=_metadata(storage)
    with storage._write() as connection:
        ready=_feature(connection,metadata)
        for name in NAMES:metadata.tables[name].create(connection,checkfirst=True)
        if not ready:connection.execute(insert(metadata.tables[NAMES[0]]).values(id=1,version=FEATURE_VERSION))
        for name in NAMES:
            for action in ("UPDATE","DELETE"):
                connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS "+name+"_no_"+action.lower()
                    +" BEFORE "+action+" ON "+name
                    +" BEGIN SELECT RAISE(ABORT,'Discovery visibility history is append-only'); END")
        connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS fi_discovery_admin_required
            BEFORE INSERT ON fi_discovery_operations BEGIN
            SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.actor_id
                AND active=1 AND role IN ('ADMIN','MANAGER'))
                THEN RAISE(ABORT,'Active administrator required') END; END""")
        connection.exec_driver_sql("""CREATE TRIGGER IF NOT EXISTS fi_discovery_head_required
            BEFORE INSERT ON fi_discovery_changes BEGIN
            SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM fi_discovery_operations AS o,
                json_each(o.products) AS p, json_each(p.value,\'$.squareVariationIds\') AS v
                WHERE o.id=NEW.operation_id AND v.value=NEW.square_variation_id AND o.excluded=NEW.excluded)
                THEN RAISE(ABORT,\'Variation outside recorded discovery action\') END;
            SELECT CASE WHEN COALESCE(NEW.before_event_id,0)<>COALESCE(
                (SELECT MAX(id) FROM fi_discovery_changes WHERE square_variation_id=NEW.square_variation_id),0)
                THEN RAISE(ABORT,'Discovery visibility head changed') END;
            SELECT CASE WHEN NEW.before_excluded<>COALESCE(
                (SELECT excluded FROM fi_discovery_changes WHERE square_variation_id=NEW.square_variation_id ORDER BY id DESC LIMIT 1),0)
                THEN RAISE(ABORT,'Discovery visibility baseline changed') END; END""")
    return storage

def _states(connection,metadata,variation_ids):
    result={identity:{"excluded":False,"version":0} for identity in variation_ids}
    if not _feature(connection,metadata):return result
    changes=metadata.tables[NAMES[2]]
    for offset in range(0,len(variation_ids),800):
        chunk=variation_ids[offset:offset+800]
        latest=select(func.max(changes.c.id).label("id")).where(changes.c.square_variation_id.in_(chunk)).group_by(changes.c.square_variation_id).subquery()
        for row in connection.execute(select(changes).join(latest,latest.c.id==changes.c.id)).mappings():
            result[row["square_variation_id"]]={"excluded":bool(row["excluded"]),"version":row["id"]}
    return result

def excluded_variation_ids(storage):
    """Read only. An absent optional legacy feature means no exclusions."""
    metadata=_metadata(storage)
    with storage.engine.connect() as connection:
        if not _feature(connection,metadata):return set()
        changes=metadata.tables[NAMES[2]]
        latest=select(func.max(changes.c.id).label("id")).group_by(changes.c.square_variation_id).subquery()
        return set(connection.scalars(select(changes.c.square_variation_id).join(latest,latest.c.id==changes.c.id)
            .where(changes.c.excluded.is_(True))))

class DiscoveryVisibility:
    def __init__(self,storage,clock=None):
        self.storage=storage;self.metadata=_metadata(storage);self.clock=clock or _now
    def states(self,variation_ids):
        values=_ids(variation_ids)
        with self.storage.engine.connect() as connection:return _states(connection,self.metadata,values)
    def preview(self,*,actor_id,products,excluded,reason):
        if type(excluded) is not bool:raise ValueError("Choose Exclude or Restore.")
        reason=_text(reason,2000).strip();selected=_products(products)
        ids=sorted({value for row in selected for value in row["squareVariationIds"]})
        with self.storage.engine.connect() as connection:
            self.storage._principal(connection,actor_id,admin=True)
            states=_states(connection,self.metadata,ids)
        changes=[{"variationId":identity,"beforeVersion":states[identity]["version"],
            "beforeExcluded":states[identity]["excluded"],"afterExcluded":excluded,
            "changed":states[identity]["excluded"]!=excluded} for identity in ids]
        for row in selected:
            count=sum(states[value]["excluded"] for value in row["squareVariationIds"])
            row.update(beforeVisibility="included" if not count else "excluded" if count==len(row["squareVariationIds"]) else "partial",
                afterVisibility="excluded" if excluded else "included",variationCount=len(row["squareVariationIds"]),
                excludedVariationCount=count,afterExcludedVariationCount=len(row["squareVariationIds"]) if excluded else 0)
        changed=sum(row["changed"] for row in changes)
        changed_products=sum(row["beforeVisibility"]!=row["afterVisibility"] for row in selected)
        plan={"id":str(uuid.uuid4()),"actorId":actor_id,
            "expiresAt":(_date(self.clock())+timedelta(minutes=10)).isoformat(),
            "excluded":excluded,"reason":reason,"products":selected,"changes":changes,
            "changedCount":changed,"unchangedCount":len(ids)-changed,
            "changedProductCount":changed_products,"unchangedProductCount":len(selected)-changed_products}
        if len(_json(plan).encode())>4000000:raise ValueError("Visibility selection is too large.")
        plan["fingerprint"]=_hash(plan)
        return plan
    def _receipt(self,connection,operation_id,replayed=False):
        operations=self.metadata.tables[NAMES[1]];events=self.metadata.tables[NAMES[2]]
        header=connection.execute(select(operations).where(operations.c.id==operation_id)).mappings().one()
        changes=[{"variationId":row["square_variation_id"],"beforeVersion":row["before_event_id"] or 0,
            "afterVersion":row["id"],"beforeExcluded":bool(row["before_excluded"]),"afterExcluded":bool(row["excluded"])}
            for row in connection.execute(select(events).where(events.c.operation_id==operation_id).order_by(events.c.square_variation_id)).mappings()]
        products=deepcopy(header["products"]);total=sum(row["variationCount"] for row in products)
        changed_products=sum(row["beforeVisibility"]!=row["afterVisibility"] for row in products)
        at=header["created_at"];at=at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
        return {"id":header["id"],"previewId":header["preview_id"],"actorId":header["actor_id"],"createdAt":at.isoformat(),
            "excluded":bool(header["excluded"]),"action":"exclude" if header["excluded"] else "restore",
            "reason":header["reason"],"products":products,"changes":changes,"changedCount":len(changes),
            "unchangedCount":total-len(changes),"changedProductCount":changed_products,
            "unchangedProductCount":len(products)-changed_products,"replayed":replayed}
    def commit(self,*,actor_id,plan,currentproducts):
        if not isinstance(plan,dict) or plan.get("actorId")!=actor_id:raise PermissionError("Visibility preview belongs to another administrator.")
        signed={key:value for key,value in plan.items() if key!="fingerprint"}
        if plan.get("fingerprint")!=_hash(signed):raise ValueError("Visibility preview changed; prepare it again.")
        if type(plan.get("excluded")) is not bool:raise ValueError("Invalid visibility action.")
        selected=_products(plan.get("products"))
        identities=sorted({vid for row in selected for vid in row["squareVariationIds"]})
        planned=plan.get("changes")
        if (not isinstance(planned,list) or len(planned)!=len(identities)
                or {row.get("variationId") for row in planned}!=set(identities)):
            raise ValueError("Visibility preview has inconsistent identities.")
        for row in planned:
            if (type(row.get("beforeVersion")) is not int or row["beforeVersion"]<0
                    or type(row.get("beforeExcluded")) is not bool or type(row.get("afterExcluded")) is not bool
                    or row["afterExcluded"]!=plan["excluded"] or row.get("changed")!=(row["beforeExcluded"]!=row["afterExcluded"])):
                raise ValueError("Visibility preview has an invalid baseline.")
        operations=self.metadata.tables[NAMES[1]];events=self.metadata.tables[NAMES[2]]
        with self.storage._write() as connection:
            self.storage._principal(connection,actor_id,admin=True)
            if not _feature(connection,self.metadata):raise ValueError("Discovery visibility has not been initialized.")
            prior=connection.execute(select(operations).where(operations.c.preview_id==plan["id"])).mappings().one_or_none()
            if prior:
                if prior["actor_id"]!=actor_id or prior["plan_fingerprint"]!=plan["fingerprint"]:
                    raise ValueError("Visibility preview was already used.")
                return self._receipt(connection,prior["id"],True)
            if _date(self.clock())>=_date(plan["expiresAt"]):raise ValueError("Visibility preview expired.")
            if not isinstance(currentproducts,list):raise ValueError("Current customer catalog is required.")
            chosen={row["id"] for row in selected};current={}
            for row in currentproducts:
                if isinstance(row,dict) and row.get("id") in chosen:
                    if row["id"] in current:raise ValueError("Customer catalog identities are ambiguous.")
                    current[row["id"]]=_products([row])[0]
            for row in selected:
                actual=current.get(row["id"])
                if actual is None or actual["squareVariationIds"]!=row["squareVariationIds"]:
                    raise ValueError("Selected catalog product or variations changed. The entire action was cancelled.")
            states=_states(connection,self.metadata,identities)
            for row in planned:
                if states[row["variationId"]]!={"excluded":row["beforeExcluded"],"version":row["beforeVersion"]}:
                    raise ValueError("Discovery visibility changed. The entire action was cancelled.")
            operation_id=str(uuid.uuid4())
            connection.execute(insert(operations).values(id=operation_id,preview_id=plan["id"],actor_id=actor_id,
                excluded=plan["excluded"],reason=plan["reason"],plan_fingerprint=plan["fingerprint"],
                products=deepcopy(plan["products"]),created_at=_date(self.clock())))
            for row in planned:
                if row["changed"]:
                    connection.execute(insert(events).values(operation_id=operation_id,square_variation_id=row["variationId"],
                        before_event_id=row["beforeVersion"] or None,before_excluded=row["beforeExcluded"],excluded=row["afterExcluded"]))
            return self._receipt(connection,operation_id)
