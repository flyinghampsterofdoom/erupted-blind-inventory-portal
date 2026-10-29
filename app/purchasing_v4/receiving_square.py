"""Version-pinned inventory adapter over an established policy-controlled client.

No HTTP client, credentials, scheduler, counts-based success inference or ambient
production connection. The standalone Phase7 application uses a durable simulator.
"""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import calendar,re
from uuid import UUID,uuid5
from app.services.square_request_policy import enforce_square_request_policy

API_VERSION='2026-09-16'
WRITE_PATH='/v2/inventory/changes/batch-create'
HISTORY_PATH='/v2/inventory/changes/batch-retrieve'



def instant(value):
    if not isinstance(value,str):raise ValueError('RFC3339 instant required')
    try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        from dateutil.parser import isoparse
        parsed=isoparse(value)  # Same existing runtime parser used by the Square gateway.
    if parsed.utcoffset() is None:raise ValueError('Aware RFC3339 instant required')
    return parsed.astimezone(timezone.utc)


def exact_instant(value):
    parsed=instant(value);fraction=re.search(r'T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.([0-9]+))?',value)
    if not fraction:raise ValueError('RFC3339 seconds required')
    return calendar.timegm(parsed.replace(microsecond=0).utctimetuple()),Decimal('0.'+(fraction.group(1) or '0'))

def payload(command_id,*,variation_id,location_id,units,from_state,to_state,occurred_at,to_location_id=None):
    UUID(command_id)
    if type(units) is not int or not 0<units<=2147483647:raise ValueError('Positive individual-unit inventory movement required')
    target_location=to_location_id or location_id
    if (from_state,to_state) not in {('NONE','IN_STOCK'),('IN_STOCK','NONE'),('IN_STOCK','WASTE'),('IN_STOCK','IN_STOCK')}:raise ValueError('Only documented quantity adjustments are qualified')
    if (from_state==to_state)!=(target_location!=location_id):raise ValueError('Cross-location adjustments preserve stock state; local adjustments change it')
    when=instant(occurred_at)
    if when.utcoffset() is None:raise ValueError('Verified physical-event instant required')
    if not variation_id or not location_id:raise ValueError('Resolved immutable Square variation and actual store location required')
    body=dict(reference_id=command_id,catalog_object_id=variation_id,catalog_object_type='ITEM_VARIATION',from_location_id=location_id,to_location_id=target_location,quantity=str(units),from_state=from_state,to_state=to_state,occurred_at=occurred_at)
    result=dict(idempotency_key=command_id,changes=[dict(type='ADJUSTMENT',adjustment=body)])
    enforce_square_request_policy('POST',WRITE_PATH,result,inventory_quantity_write=True)
    return result



def quarantine_payload(command_id,*,variation_id,location_id,units,occurred_at):
    # Square applies the two changes atomically. Damaged goods never become
    # available for sale between separate successful/failed network requests.
    first=payload(command_id,variation_id=variation_id,location_id=location_id,units=units,from_state='NONE',to_state='IN_STOCK',occurred_at=occurred_at)
    child=str(uuid5(UUID(command_id),'quarantine-state-change'))
    second=payload(child,variation_id=variation_id,location_id=location_id,units=units,from_state='IN_STOCK',to_state='WASTE',occurred_at=occurred_at)
    first['changes']+=second['changes']
    enforce_square_request_policy('POST',WRITE_PATH,first,inventory_quantity_write=True)
    return first


def match_proof(command_payload,proof):
    expected=command_payload['changes'];actual=proof.get('changes') if len(expected)>1 else [proof]
    return (isinstance(actual,list) and len(actual)==len(expected) and
            all(match_adjustment(dict(changes=[e]),a) for e,a in zip(expected,actual)) and
            len({a['adjustment']['id'] for a in actual})==len(actual))

def match_adjustment(command_payload,change):
    if not isinstance(change,dict) or change.get('type')!='ADJUSTMENT':return False
    actual=change.get('adjustment',{});expected=command_payload['changes'][0]['adjustment']
    if not isinstance(actual,dict) or not isinstance(actual.get('id'),str) or not actual['id']:return False
    for field in ['reference_id','catalog_object_id','from_location_id','to_location_id','from_state','to_state']:
        if actual.get(field)!=expected[field]:return False
    if actual.get('catalog_object_type','ITEM_VARIATION')!='ITEM_VARIATION':return False
    try:
        quantity=Decimal(str(actual.get('quantity')))
        if not quantity.is_finite() or quantity!=Decimal(expected['quantity']):return False
        return exact_instant(actual['occurred_at'])==exact_instant(expected['occurred_at'])
    except (ValueError,TypeError,KeyError,InvalidOperation):return False


class InventoryAdapter:
    def __init__(self,client):
        if getattr(client,'headers',{}).get('Square-Version')!=API_VERSION:raise PermissionError('Established Square client must be explicitly pinned to '+API_VERSION)
        self.client=client

    def send(self,command_payload):
        enforce_square_request_policy('POST',WRITE_PATH,command_payload,inventory_quantity_write=True)
        # The local command and in-flight attempt must already be committed.
        return self.client.post(WRITE_PATH,command_payload,inventory_quantity_write=True)

    def reconcile(self,command_payload):
        expected=command_payload['changes'][0]['adjustment'];cursor=None;seen=set();matches={x['adjustment']['reference_id']:{} for x in command_payload['changes']}
        # Bounded pages; incomplete coverage never proves absence or success.
        for _ in range(100):
            query=dict(catalog_object_ids=[expected['catalog_object_id']],location_ids=list({expected['from_location_id'],expected['to_location_id']}),types=['ADJUSTMENT'],limit=1000)
            if cursor:query['cursor']=cursor
            enforce_square_request_policy('POST',HISTORY_PATH,query)
            page=self.client.post(HISTORY_PATH,query)
            if not isinstance(page,dict) or page.get('errors') or not isinstance(page.get('changes',[]),list) or len(page.get('changes',[]))>1000:raise RuntimeError('Unqualified Square change-history response')
            for change in page.get('changes',[]):
                for component in command_payload['changes']:
                    if match_adjustment(dict(changes=[component]),change):matches[component['adjustment']['reference_id']][change['adjustment']['id']]=change
            cursor=page.get('cursor')
            if not cursor:break
            if not isinstance(cursor,str) or len(cursor)>4096 or cursor in seen:raise RuntimeError('Invalid Square reconciliation cursor')
            seen.add(cursor)
        else:raise RuntimeError('Square reconciliation bound reached; outcome remains ambiguous')
        if any(len(values)>1 for values in matches.values()):raise RuntimeError('Conflicting remote adjustment identities; owner reconciliation required')
        if any(not values for values in matches.values()):return None
        found=[next(iter(matches[x['adjustment']['reference_id']].values())) for x in command_payload['changes']]
        return dict(changes=found) if len(found)>1 else found[0]
