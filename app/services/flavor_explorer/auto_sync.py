"""Read-only Square observation and bounded cache synchronization policy."""
from __future__ import annotations
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import base64
import hashlib
import hmac
import json
from app.services.flavor_explorer.square_catalog import normalize_catalog
from app.services.square_request_policy import enforce_square_request_policy

INVENTORY_PATH="/v2/inventory/counts/batch-retrieve"
def utc(value=None):
    value=value() if callable(value) else value
    if value is None:return datetime.now(timezone.utc)
    if isinstance(value,str):value=datetime.fromisoformat(value.replace("Z","+00:00"))
    if value.tzinfo is None:raise ValueError("Timezone required")
    return value.astimezone(timezone.utc)
def iso(value):return utc(value).isoformat().replace("+00:00","Z")

@dataclass(frozen=True)
class SyncPolicy:
    inventory_interval:int=120
    inventory_max_age:int=300
    catalog_interval:int=3600
    retry_min:int=15
    retry_max:int=120
    def __post_init__(self):
        if not 30<=self.inventory_interval<=300:raise ValueError("Inventory interval must be 30..300 seconds")
        if not self.inventory_interval<self.inventory_max_age<=900:raise ValueError("Freshness must exceed normal polling")
        if self.catalog_interval<300 or not 1<=self.retry_min<=self.retry_max<=300:raise ValueError("Invalid sync policy")

def refresh_inventory_readonly(snapshot,post,*,now=None,policy=SyncPolicy(),variation_ids=None):
    """Read exact existing variation/location pairs; never manufacture omitted zeros."""
    from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
    stores=snapshot.get("stores",[])
    if len(stores)!=4 or any(not s.get("verified") or not s.get("squareLocationId") for s in stores):
        raise ValueError("Four verified locations required")
    all_ids={v["id"] for p in normalize_catalog(snapshot)["products"] for v in p["variations"]}
    selected=all_ids if variation_ids is None else all_ids.intersection(variation_ids)
    if not selected:raise ValueError("No qualifying known variations")
    location_ids={s["squareLocationId"] for s in stores}
    observed={}
    def read(path,payload):
        if path!=INVENTORY_PATH:raise PermissionError("Inventory observation only")
        enforce_square_request_policy("POST",path,payload)
        response=post(path,payload)
        stamp=iso(now)
        for row in response.get("counts",[]):
            key=(row.get("location_id"),row.get("catalog_object_id"))
            if key[0] in location_ids and key[1] in selected and row.get("state")=="IN_STOCK":
                observed[key]=stamp
        return response
    gateway=SquareOrderingReadGateway(post=read)
    result=gateway.fetch_current_inventory_counts(location_ids=sorted(location_ids),variation_ids=sorted(selected))
    current=utc(now);output=deepcopy(snapshot)
    # Queried-but-omitted pairs become unknown, while unqueried evidence keeps its original time.
    output["inventory"]=[row for row in snapshot.get("inventory",[]) if row.get("variationId") not in selected]
    output["inventory"] += [{"locationId":location,"variationId":variation,"quantity":str(row.quantity),
        "calculatedAt":iso(row.calculated_at) if row.calculated_at else None,"observedAt":observed[(location,variation)]}
        for (location,variation),row in sorted(result.counts.items())]
    output["inventoryObservedAt"]=iso(current);output["inventoryTtlSeconds"]=policy.inventory_max_age
    output["inventoryCoverage"]={"requestedPairs":len(all_ids)*4,"observedPairs":len(output["inventory"]),
        "missingPairs":len(all_ids)*4-len(output["inventory"])}
    output["inventoryReadMetrics"]=dict(result.metrics.endpoint_request_counts)
    output["sync"]={"mode":"automatic","lastSuccess":iso(current),"lastAttempt":iso(current),
        "failures":0,"inventoryIntervalSeconds":policy.inventory_interval,"inventoryMaxAgeSeconds":policy.inventory_max_age}
    return output

def failed_observation(snapshot,error,*,now=None,failures=1,policy=SyncPolicy()):
    """Preserve all success timestamps and data. Private errors never enter the public projection."""
    output=deepcopy(snapshot)
    delay=min(policy.retry_max,policy.retry_min*2**min(max(failures-1,0),8))
    output["sync"]={**output.get("sync",{}),"mode":"automatic","lastAttempt":iso(now),
        "failures":failures,"errorCode":type(error).__name__,"retryAt":iso(utc(now)+timedelta(seconds=delay)),
        "inventoryIntervalSeconds":policy.inventory_interval,"inventoryMaxAgeSeconds":policy.inventory_max_age}
    output["inventoryTtlSeconds"]=policy.inventory_max_age
    return output,delay

class WebhookInvalidations:
    """Signature-checked, bounded invalidations; never applies event quantities."""
    def __init__(self,*,signature_key,notification_url,merchant_id,capacity=2048):
        if not signature_key or not notification_url.startswith("https://") or not merchant_id:raise ValueError("Configured subscription required")
        if type(capacity) is not int or not 1<=capacity<=8192:raise ValueError("Invalid webhook capacity")
        self.key=signature_key.encode();self.url=notification_url.encode();self.merchant=merchant_id
        self.seen=OrderedDict();self.capacity=capacity;self.variation_ids=set();self.catalog=False
    def receive(self,body,signature):
        if not isinstance(body,bytes) or len(body)>262144:raise ValueError("Webhook body exceeds bound")
        expected=base64.b64encode(hmac.new(self.key,self.url+body,hashlib.sha256).digest()).decode()
        if not isinstance(signature,str) or not hmac.compare_digest(expected,signature):raise PermissionError("Invalid Square signature")
        event=json.loads(body)
        if not isinstance(event,dict):raise ValueError("Invalid Square notification")
        if event.get("merchant_id")!=self.merchant:raise PermissionError("Unexpected merchant")
        event_id=event.get("event_id")
        if not isinstance(event_id,str) or not 1<=len(event_id)<=128:raise ValueError("Event ID required")
        if event_id in self.seen:return {"duplicate":True,"accepted":False}
        event_type=event.get("type")
        if event_type=="inventory.count.updated":
            data=event.get("data") or {};obj=data.get("object") if isinstance(data,dict) else None
            if not isinstance(obj,dict):raise ValueError("Invalid inventory notification")
            counts=obj.get("inventory_counts",[])
            if not isinstance(counts,list) or len(counts)>100:raise ValueError("Invalid inventory notification")
            ids={row["catalog_object_id"] for row in counts if isinstance(row,dict)
                 and isinstance(row.get("catalog_object_id"),str) and 1<=len(row["catalog_object_id"])<=128}
            if not self.catalog:
                if len(self.variation_ids|ids)>self.capacity:
                    # A full authoritative read subsumes all accumulated IDs.
                    self.catalog=True;self.variation_ids.clear()
                else:self.variation_ids.update(ids)
        elif event_type=="catalog.version.updated":
            self.catalog=True;self.variation_ids.clear()
        else:return {"accepted":False,"ignored":True}
        self.seen[event_id]=True
        while len(self.seen)>self.capacity:self.seen.popitem(last=False)
        return {"accepted":True,"duplicate":False}
    def drain(self):
        result={"catalog":self.catalog,"variationIds":set(self.variation_ids)}
        self.catalog=False;self.variation_ids.clear()
        return result
