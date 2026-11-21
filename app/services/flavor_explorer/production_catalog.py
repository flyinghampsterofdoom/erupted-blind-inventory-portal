"""Durable read-only Square observations on the existing portal PostgreSQL DB.

Search reads cached identity only. A leased observer refreshes one bounded
last-good snapshot; explicit staff stock checks never persist or sum packages.
"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
import threading
import uuid

from sqlalchemy import select,insert,update,func
from app.services.flavor_explorer.square_catalog import (
    STORE_SPECS,capture_readonly,normalize_catalog)
from app.services.flavor_explorer.auto_sync import SyncPolicy,refresh_inventory_readonly
from app.services.flavor_explorer.compatibility.catalog_identity import capture_identity
from app.services.square_request_policy import enforce_square_request_policy

POLICY=SyncPolicy(inventory_interval=120,inventory_max_age=300,catalog_interval=3600)
IDENTITY_INTERVAL=900
IDENTITY_MAX_AGE=3600
LEASE_SECONDS=60

def _now():return datetime.now(timezone.utc)
def _date(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace("Z","+00:00"))
    if not isinstance(value,datetime) or value.tzinfo is None:raise ValueError("Source timezone required.")
    return value.astimezone(timezone.utc)
def _iso(value):return _date(value).isoformat().replace("+00:00","Z")
def _age(now,value):
    if not value:return float("inf")
    try:
        seconds=(_date(now)-_date(value)).total_seconds()
        return float("inf") if seconds < -5 else max(0,seconds)
    except (ValueError,TypeError):return float("inf")
def _bounded(value):
    if len(json.dumps(value,allow_nan=False,separators=(",",":")).encode())>4_000_000:
        raise ValueError("Source observation exceeded its bound.")
def _key(value):return "".join(c for c in str(value).casefold() if c.isalnum())

class ProductionCatalog:
    def __init__(self,storage,get=None,post=None):
        self.storage=storage;self.observations=storage.table("fi_catalog_observations")
        self.lease=storage.table("fi_refresh_lease")
        if get is None or post is None:
            from app.services.square_ordering_data_service import _square_request,_square_post
            if get is None:get=lambda path:_square_request("GET",path,None,timeout_seconds=15)
            if post is None:post=lambda path,payload:_square_post(path,payload,timeout_seconds=15,
                read_api_version="2026-09-16" if path=="/v2/inventory/counts/batch-retrieve" else None)
        self.get=get;self.post=post
        self._stop=threading.Event();self._thread=None;self._step_lock=threading.Lock();self._thread_lock=threading.Lock()
        self.last_result={"status":"uninitialized","sourceMutations":0}

    def _stores(self,c=None):
        def read(connection):
            table=self.storage.table("stores")
            rows={row["id"]:dict(row) for row in connection.execute(select(table).where(
                table.c.id.in_([spec[2] for spec in STORE_SPECS]))).mappings()}
            result=[]
            for identity,label,local_id,aliases in STORE_SPECS:
                row=rows.get(local_id)
                if (not row or not row.get("active") or not row.get("square_location_id")
                        or _key(row["name"]) not in aliases):
                    raise ValueError("Four verified active store mappings are required.")
                result.append({"id":identity,"name":row["name"],"localStoreId":local_id,
                    "squareLocationId":row["square_location_id"],"verified":True})
            if len({row["squareLocationId"] for row in result})!=4:
                raise ValueError("Store locations must be distinct.")
            return result
        if c is not None:return read(c)
        with self.storage.engine.connect() as connection:return read(connection)

    def _check_stores(self,stores,c=None):
        actual=self._stores(c)
        pairs=lambda rows:sorted((row["id"],row.get("localStoreId"),row["squareLocationId"]) for row in rows)
        supplied=[]
        for row in stores:
            if not isinstance(row,dict) or row.get("verified") is False:raise ValueError("Unverified provider store mapping.")
            spec=next((spec for spec in STORE_SPECS if spec[0]==row.get("id")),None)
            if spec is None:raise ValueError("Unexpected provider store identity.")
            supplied.append({**row,"localStoreId":row.get("localStoreId",spec[2])})
        if len(supplied)!=4 or pairs(supplied)!=pairs(actual):
            raise ValueError("Provider and portal store identities differ.")
        return actual

    def _record(self):
        with self.storage.engine.connect() as c:
            row=c.execute(select(self.observations).where(self.observations.c.id==1)).mappings().one()
            return deepcopy(dict(row))

    def snapshot(self):
        value=self._record()["snapshot"]
        if not isinstance(value,dict):raise ValueError("The Square catalog is not ready.")
        self._check_stores(value.get("stores",[]))
        return value

    def identity(self):
        row=self._record();value=row["identity"]
        if not isinstance(value,dict):
            return {"observedAt":None,"variations":[],"stores":[],"available":False}
        try:self._check_stores(value.get("stores",[]))
        except ValueError:
            return {"observedAt":None,"variations":[],"stores":[],"available":False}
        return {**value,"available":_age(_now(),value.get("observedAt"))<=IDENTITY_MAX_AGE}

    def _claim(self,force):
        with self.storage._write() as c:
            now=c.scalar(select(func.clock_timestamp()))
            lease=c.execute(select(self.lease).where(self.lease.c.id==1)).mappings().one()
            if lease["owner"] is not None and lease["expires_at"]>now:return None,"busy"
            row=dict(c.execute(select(self.observations).where(self.observations.c.id==1)).mappings().one())
            if not force and row["last_error"] and _age(now,row["last_attempt_at"])<15:return None,"idle"
            snapshot=row["snapshot"] or {};identity=row["identity"] or {}
            due={"catalog":force or _age(now,snapshot.get("catalogObservedAt"))>=POLICY.catalog_interval,
                 "inventory":force or _age(now,snapshot.get("inventoryObservedAt"))>=POLICY.inventory_interval,
                 "identity":force or _age(now,identity.get("observedAt"))>=IDENTITY_INTERVAL}
            if not any(due.values()):return None,"idle"
            owner=str(uuid.uuid4())
            c.execute(update(self.lease).where(self.lease.c.id==1).values(owner=owner,expires_at=now+timedelta(seconds=LEASE_SECONDS)))
            c.execute(update(self.observations).where(self.observations.c.id==1).values(last_attempt_at=now))
            return {"owner":owner,"record":deepcopy(row),"due":due},None

    def _renew(self,owner):
        if self._stop.is_set():raise InterruptedError("Observer stopped.")
        with self.storage._write() as c:
            now=c.scalar(select(func.clock_timestamp()))
            lease=c.execute(select(self.lease).where(self.lease.c.id==1)).mappings().one()
            if lease["owner"]!=owner or lease["expires_at"] is None or lease["expires_at"]<=now:
                raise ValueError("Observation lease expired.")
            c.execute(update(self.lease).where(self.lease.c.id==1).values(expires_at=now+timedelta(seconds=LEASE_SECONDS)))

    def _read(self,owner,method,path,payload=None):
        self._renew(owner)
        if (method,path) not in {("GET","/v2/locations"),("POST","/v2/catalog/search"),
                ("POST","/v2/catalog/search-catalog-items"),("POST","/v2/inventory/counts/batch-retrieve")}:
            raise PermissionError("Explorer source reads only.")
        enforce_square_request_policy(method,path,payload)
        return self.get(path) if method=="GET" else self.post(path,payload)

    def _release(self,owner):
        with self.storage._write() as c:
            c.execute(update(self.lease).where(self.lease.c.id==1,self.lease.c.owner==owner).values(owner=None,expires_at=None))

    def _mirror(self,c,snapshot):
        products=normalize_catalog(snapshot)["products"]
        if not products:raise ValueError("The resolved E-liquid catalog is empty.")
        links=self.storage.table("fi_square_flavor_links");flavors=self.storage.table("touchscreen_flavors")
        ids=sorted({variation["id"] for product in products for variation in product["variations"]})
        known={}
        for offset in range(0,len(ids),800):
            known.update({row["square_variation_id"]:dict(row) for row in c.execute(select(links).where(
                links.c.square_variation_id.in_(ids[offset:offset+800]))).mappings()})
        pending=[]
        for product in products:
            variants=product["variations"];owners={known[v["id"]]["flavor_id"] for v in variants if v["id"] in known}
            if len(owners)>1:raise ValueError("Existing flavor identities cannot be merged automatically.")
            flavor=next(iter(owners)) if owners else c.execute(insert(flavors).values()).inserted_primary_key[0]
            for variation in variants:
                parent=variation.get("squareItemId")
                if not isinstance(parent,str) or not parent:raise ValueError("Exact Square parent identity is required.")
                previous=known.get(variation["id"])
                if previous:
                    if previous["square_item_id"]!=parent or previous["flavor_id"]!=flavor:
                        raise ValueError("Square variation identity cannot be reassigned.")
                else:
                    pending.append({"square_variation_id":variation["id"],"square_item_id":parent,
                        "flavor_id":flavor,"observed_at":_date(snapshot["catalogObservedAt"])})
        for offset in range(0,len(pending),800):c.execute(insert(links),pending[offset:offset+800])

    def _finish(self,claim,snapshot,identity,changed,errors):
        if self._stop.is_set():return {"status":"stopped","sourceMutations":0}
        if snapshot is not None:
            self._check_stores(snapshot.get("stores",[]));_bounded(snapshot)
        if identity is not None:
            self._check_stores(identity.get("stores",[]));_bounded(identity)
            rows=identity.get("variations",[])
            if (not isinstance(rows,list) or not rows or len(rows)>15000 or len({row["id"] for row in rows})!=len(rows)
                    or any(set(row)!={"id","itemId","sku","upc","itemName","variationName"}
                        or any(not isinstance(value,str) or len(value)>4096 for value in row.values()) for row in rows)):
                raise ValueError("Complete identity observation is invalid.")
        with self.storage._write() as c:
            now=c.scalar(select(func.clock_timestamp()))
            lease=c.execute(select(self.lease).where(self.lease.c.id==1)).mappings().one()
            current=c.execute(select(self.observations).where(self.observations.c.id==1)).mappings().one()
            if (lease["owner"]!=claim["owner"] or lease["expires_at"] is None or lease["expires_at"]<=now
                    or current["generation"]!=claim["record"]["generation"]):
                raise ValueError("Observation lease or generation changed.")
            self._check_stores(snapshot.get("stores",[]),c) if snapshot is not None else self._stores(c)
            if changed["snapshot"]:self._mirror(c,snapshot)
            values={"snapshot":snapshot,"identity":identity,"generation":current["generation"]+1,
                "last_attempt_at":now,"last_error":errors[0] if errors else None}
            if changed["snapshot"] or changed["identity"]:
                values.update(observed_at=now,last_success_at=now)
            c.execute(update(self.observations).where(self.observations.c.id==1).values(**values))
            return {"status":"partial" if errors and any(changed.values()) else "failed" if errors else "refreshed",
                "generation":values["generation"],"catalogRefreshed":changed["catalog"],
                "inventoryRefreshed":changed["snapshot"],"identityRefreshed":changed["identity"],
                "errorCode":errors[0] if errors else None,"sourceMutations":0}

    def refresh_once(self,force=False):
        """True only after a complete fresh read; diagnostics remain available separately."""
        self.last_result=self._refresh_once(force)
        return self.last_result["status"]=="refreshed"

    def _refresh_once(self,force=False):
        if type(force) is not bool:raise ValueError("Invalid refresh request.")
        if self._stop.is_set():return {"status":"stopped","sourceMutations":0}
        if not self._step_lock.acquire(blocking=False):return {"status":"busy","sourceMutations":0}
        claim=None
        try:
            claim,status=self._claim(force)
            if claim is None:return {"status":status,"sourceMutations":0}
            row=claim["record"];snapshot=row["snapshot"];identity=row["identity"]
            changed={"snapshot":False,"identity":False,"catalog":False};errors=[]
            get=lambda path:self._read(claim["owner"],"GET",path)
            inventory_seen=set();inventory_duplicates=set()
            def post(path,payload):
                response=self._read(claim["owner"],"POST",path,payload)
                if (path=="/v2/inventory/counts/batch-retrieve" and isinstance(response,dict)
                        and isinstance(response.get("counts"),list)):
                    locations=set(payload.get("location_ids",[]));variations=set(payload.get("catalog_object_ids",[]))
                    for raw in response["counts"]:
                        if not isinstance(raw,dict) or str(raw.get("state") or "").strip().upper()!="IN_STOCK":continue
                        pair=(str(raw.get("location_id") or "").strip(),str(raw.get("catalog_object_id") or "").strip())
                        if pair[0] not in locations or pair[1] not in variations:continue
                        if pair in inventory_seen:inventory_duplicates.add(pair)
                        inventory_seen.add(pair)
                return response
            # Portal mapping validation precedes every provider request.
            self._stores()
            if claim["due"]["catalog"] or claim["due"]["inventory"]:
                try:
                    if claim["due"]["catalog"]:
                        fresh=capture_readonly(get,post,now=_now)
                        changed["catalog"]=True
                    else:fresh=refresh_inventory_readonly(snapshot,post,now=_now,policy=POLICY)
                    # The shared gateway chooses one row; retain uncertainty instead
                    # of persisting a conflicted pair as a confirmed quantity.
                    fresh["inventory"]=[record for record in fresh.get("inventory",[])
                        if (record.get("locationId"),record.get("variationId")) not in inventory_duplicates]
                    fresh["gaps"]=[gap for gap in fresh.get("gaps",[]) if gap.get("code")!="duplicate_inventory_count"]
                    fresh["gaps"].extend({"code":"duplicate_inventory_count","squareLocationId":location,
                        "squareVariationId":variation} for location,variation in sorted(inventory_duplicates))
                    coverage=fresh.setdefault("inventoryCoverage",{})
                    coverage["observedPairs"]=len(fresh["inventory"])
                    coverage["missingPairs"]=coverage["requestedPairs"]-coverage["observedPairs"]
                    coverage["duplicatePairs"]=len(inventory_duplicates)
                    self._check_stores(fresh.get("stores",[]));snapshot=fresh;changed["snapshot"]=True
                except Exception as error:errors.append(type(error).__name__)
            if claim["due"]["identity"] and snapshot is not None and not self._stop.is_set():
                try:
                    fresh_identity=capture_identity(post,snapshot["stores"],clock=lambda:_now().timestamp(),cancelled=self._stop.is_set)
                    identity=fresh_identity;changed["identity"]=True
                except Exception as error:errors.append(type(error).__name__)
            if snapshot is not None:
                snapshot=deepcopy(snapshot);snapshot["inventoryTtlSeconds"]=POLICY.inventory_max_age
                snapshot["sync"]={"mode":"automatic","lastSuccess":snapshot.get("inventoryObservedAt"),
                    "lastAttempt":_iso(_now()),"failures":(snapshot.get("sync",{}).get("failures",0)+1) if errors else 0,
                    "inventoryIntervalSeconds":POLICY.inventory_interval,"inventoryMaxAgeSeconds":POLICY.inventory_max_age}
                if errors:snapshot["sync"]["errorCode"]=errors[0]
            return self._finish(claim,snapshot,identity,changed,errors)
        except Exception as error:
            if claim is not None:
                # Rejected identity/mirror/mapping data must never replace good observations.
                try:
                    with self.storage._write() as c:
                        owner=c.scalar(select(self.lease.c.owner).where(self.lease.c.id==1))
                        if owner==claim["owner"]:
                            c.execute(update(self.observations).where(self.observations.c.id==1,
                                self.observations.c.generation==claim["record"]["generation"]).values(
                                generation=claim["record"]["generation"]+1,
                                last_attempt_at=func.clock_timestamp(),last_error=type(error).__name__))
                except Exception:pass
            return {"status":"failed","errorCode":type(error).__name__,"sourceMutations":0}
        finally:
            if claim is not None:
                try:self._release(claim["owner"])
                except Exception:pass
            self._step_lock.release()

    def _unknown(self,ids,reason,stores=None):
        stores=stores or [{"id":spec[0],"name":spec[1]} for spec in STORE_SPECS]
        return {"stores":[{"id":store["id"],"name":store["name"],"quantity":None,"state":"unknown","reason":reason,
            "variations":[{"id":identity,"quantity":None,"state":"unknown","reason":reason} for identity in ids]} for store in stores],
            "observedAt":None}

    def stock(self,variation_ids):
        if (not isinstance(variation_ids,(list,tuple)) or not 0<len(variation_ids)<=64
                or any(not isinstance(value,str) or not value or len(value)>255 for value in variation_ids)):
            raise ValueError("Choose bounded exact catalog variations.")
        ids=sorted(set(variation_ids));identity=self.identity()
        labels={row["id"]:row for row in identity.get("variations",[])}
        if not identity.get("available") or not set(ids)<=set(labels):
            return self._unknown(ids,"catalog_refresh_required",identity.get("stores"))
        try:stores=self._check_stores(identity["stores"])
        except ValueError:return self._unknown(ids,"store_mapping_changed")
        from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
        requested={(store["squareLocationId"],vid) for store in stores for vid in ids}
        seen=set();duplicates=set()
        def post(path,payload):
            if path!="/v2/inventory/counts/batch-retrieve":raise PermissionError("Explicit inventory reads only.")
            enforce_square_request_policy("POST",path,payload)
            response=self.post(path,payload)
            # Keep raw ambiguity before the shared gateway chooses one/latest row.
            # State and identity normalization match the gateway; scope stays exact.
            if isinstance(response,dict) and isinstance(response.get("counts"),list):
                for raw in response["counts"]:
                    if not isinstance(raw,dict) or str(raw.get("state") or "").strip().upper()!="IN_STOCK":continue
                    pair=(str(raw.get("location_id") or "").strip(),str(raw.get("catalog_object_id") or "").strip())
                    if pair not in requested:continue
                    if pair in seen:duplicates.add(pair)
                    seen.add(pair)
            return response
        try:
            counts=SquareOrderingReadGateway(post=post).fetch_current_inventory_counts(
                location_ids=[store["squareLocationId"] for store in stores],variation_ids=ids)
            checked=_now();after=self.identity()
            after_labels={row["id"]:row for row in after.get("variations",[])}
            if not after.get("available") or any(after_labels.get(key)!=labels[key] for key in ids):
                return self._unknown(ids,"catalog_changed",stores)
            if self._stores()!=stores:return self._unknown(ids,"store_mapping_changed",stores)
        except Exception:return self._unknown(ids,"stock_reader_unavailable",stores)
        output=[]
        for store in stores:
            variants=[]
            for vid in ids:
                record=counts.counts.get((store["squareLocationId"],vid))
                quantity=record.quantity if record else None
                conflict=(store["squareLocationId"],vid) in duplicates
                valid=not conflict and quantity is not None and quantity>=Decimal(0)
                state="unknown" if not valid else "in_stock" if quantity>0 else "out_of_stock"
                variants.append({"id":vid,"sku":labels[vid]["sku"],"name":labels[vid]["variationName"],
                    "quantity":str(quantity) if valid else None,"state":state,
                    "reason":"duplicate_count" if conflict else "missing_count" if record is None else "negative_count" if not valid else None,
                    "calculatedAt":_iso(record.calculated_at) if record and record.calculated_at and not conflict else None})
            state="in_stock" if any(row["state"]=="in_stock" for row in variants) else "out_of_stock" if all(
                row["state"]=="out_of_stock" for row in variants) else "unknown"
            output.append({"id":store["id"],"name":store["name"],"quantity":variants[0]["quantity"] if len(variants)==1 else None,
                "state":state,"variations":variants})
        return {"stores":output,"observedAt":_iso(checked),"validUntil":_iso(checked+timedelta(seconds=60))}

    def start(self):
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():return
            self._stop.clear()
            def observe():
                while not self._stop.is_set():
                    self.refresh_once()
                    self._stop.wait(5)
            self._thread=threading.Thread(target=observe,name="product-explorer-source-refresh",daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        with self._thread_lock:thread=self._thread
        if thread is not None and thread is not threading.current_thread():thread.join(timeout=17)
