"""Isolated owner commands. No purchasing, financial, inventory or Square writer."""
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
import json,re
from uuid import UUID,uuid4
from sqlalchemy import text
from .isolation import require_isolated
from .identity import digest
from .calculation_documents import configuration_from_document,exact

KINDS={'GLOBAL','CATEGORY','PRODUCT','MANUAL','SUPPLIER','OFFER','ASSIGNMENT','REVIEW'}
REMOVED={'_removed':True}
PROFILE_FIELDS={'name','primary_contact','secondary_contact','email','phone','address','account_number','payment_terms','order_minimum','internal_notes','active','confirmation_enabled','automatic_spreadsheet_email','automatic_po_email','preferred_po_format','use_vendor_sku'}
GLOBAL_FIELDS={'lookback_days','recent_days','minimum_available_days','recent_influence','increasing_sensitivity','decreasing_sensitivity','increasing_limit','decreasing_limit','reliability_depth_multiple','stockout_kind','stockout_threshold','negative_inventory','reorder_weeks','target_weeks','status'}

class Conflict(ValueError):pass
class InvalidPolicy(ValueError):pass


def uuid(value):
    if not isinstance(value,str):raise InvalidPolicy('Immutable internal identity required')
    try:return str(UUID(value))
    except ValueError:raise InvalidPolicy('Invalid internal identity')


def owner(connection,actor_id):
    require_isolated(connection)
    role=connection.execute(text('''SELECT role::text FROM public.principals p WHERE p.id=:id AND p.active
      AND NOT EXISTS(SELECT 1 FROM public.employees e WHERE e.principal_id=p.id AND NOT e.active)'''),{'id':actor_id}).scalar_one_or_none()
    if role!='ADMIN':raise PermissionError('Active owner ADMIN authorization required')


def heads(connection,keys):
    if not keys:return {}
    rows=connection.execute(text('''SELECT h.* FROM purchasing_v4.control_head h JOIN
      jsonb_to_recordset(CAST(:keys AS jsonb)) AS k(kind text,record_key text)
      USING(kind,record_key)'''),{'keys':json.dumps([dict(kind=k,record_key=v) for k,v in sorted(keys)])}).mappings()
    return {(r['kind'],r['record_key']):dict(r) for r in rows}


def live(record):
    return record['payload'] if record and record['payload']!=REMOVED else None


def _fields(value,required):
    if not isinstance(value,dict) or set(value)!=set(required):raise InvalidPolicy('Fields must be exactly: '+', '.join(sorted(required)))


def _coverage(value):
    if value is not None:
        _fields(value,{'reorder_weeks','target_weeks'})
        if not 0<exact(value['reorder_weeks'])<exact(value['target_weeks']):raise InvalidPolicy('Coverage requires positive weeks and a higher target')


def _integer(value):return type(value) is int and value>=0


def validate(kind,key,value,*,conversion=False,removal=False):
    if value==REMOVED:
        if not removal or kind=='MANUAL':raise InvalidPolicy('Manual rules persist; explicitly inactivate or Use Automatic Engine')
        return
    if kind=='GLOBAL':
        if not isinstance(value,dict) or not GLOBAL_FIELDS<=set(value) or set(value)-GLOBAL_FIELDS-{'max_source_age_seconds'}:raise InvalidPolicy('Complete explicit global parameters required')
        if value.get('max_source_age_seconds') is not None and not _integer(value['max_source_age_seconds']):raise InvalidPolicy('Source freshness requires nonnegative whole seconds or unset')
        if key!='global' or value['status'] not in ('DRAFT','OWNER_APPROVED'):raise InvalidPolicy('Invalid global control status')
        if value['negative_inventory']!='abstain_on_negative':raise InvalidPolicy('Negative inventory must abstain under Phase3 policy')
        configuration_from_document(dict(value,id='validation'))
    elif kind=='CATEGORY':
        _fields(value,{'category_id','parameters','coverage'})
        if value['category_id']!=key or not isinstance(value['parameters'],dict):raise InvalidPolicy('Category identity mismatch')
        _coverage(value['coverage'])
        if value['parameters'].get('negative_inventory') not in (None,'abstain_on_negative'):raise InvalidPolicy('Category cannot authorize unreliable negative stock')
    elif kind=='PRODUCT':
        _fields(value,{'variation_id','no_reorder','coverage'})
        if uuid(value['variation_id'])!=key or type(value['no_reorder']) is not bool:raise InvalidPolicy('Global product identity/eligibility required')
        _coverage(value['coverage'])
    elif kind=='MANUAL':
        _fields(value,{'variation_id','store_id','state','par_units','level_units'})
        if not _integer(value['store_id']) or value['store_id']<1 or key!=uuid(value['variation_id'])+':'+str(value['store_id']):raise InvalidPolicy('Store-specific internal identity required')
        if value['state'] not in ('ACTIVE','INACTIVE','AUTOMATIC'):raise InvalidPolicy('Invalid manual policy state')
        if value['state']=='AUTOMATIC' and not conversion:raise InvalidPolicy('Only explicit Use Automatic Engine may convert a rule')
        if value['state']=='ACTIVE' and not (_integer(value['par_units']) and _integer(value['level_units']) and value['par_units']<value['level_units']):raise InvalidPolicy('Active manual policies require individual units: 0 <= PAR < LEVEL')
        if value['state']=='INACTIVE' and any(n is not None and not _integer(n) for n in (value['par_units'],value['level_units'])):raise InvalidPolicy('Inactive quantities must be nonnegative integral units or unknown')
    elif kind=='SUPPLIER':
        _fields(value,PROFILE_FIELDS|{'supplier_id'})
        if uuid(value['supplier_id'])!=key:raise InvalidPolicy('Supplier identity mismatch')
        for k in PROFILE_FIELDS-{'active','confirmation_enabled','automatic_spreadsheet_email','automatic_po_email','use_vendor_sku','order_minimum'}:
            if value[k] is not None and (not isinstance(value[k],str) or len(value[k])>2000):raise InvalidPolicy('Invalid supplier profile text')
        if not value['name'] or not value['name'].strip():raise InvalidPolicy('Supplier name required')
        for k in ('active','confirmation_enabled','automatic_spreadsheet_email','automatic_po_email','use_vendor_sku'):
            if type(value[k]) is not bool:raise InvalidPolicy('Explicit supplier preference required')
        if value['preferred_po_format'] not in ('PDF','SPREADSHEET'):raise InvalidPolicy('Unsupported preferred format')
        if value['order_minimum'] is not None and exact(value['order_minimum'])<0:raise InvalidPolicy('Minimum cannot be negative; it is descriptive, never an engine clamp')
    elif kind=='OFFER':
        _fields(value,{'variation_id','supplier_id','approved','pack_size','expected_unit_cost','cost_provenance','vendor_sku'})
        if key!=uuid(value['variation_id'])+':'+uuid(value['supplier_id']):raise InvalidPolicy('Offer identity mismatch')
        if type(value['approved']) is not bool or (value['pack_size'] is not None and (not _integer(value['pack_size']) or value['pack_size']<1)):raise InvalidPolicy('Explicit approval and positive individual units per pack required')
        if value['expected_unit_cost'] is not None:
            if not isinstance(value['expected_unit_cost'],str):raise InvalidPolicy('Exact decimal cost string required')
            try:cost=Decimal(value['expected_unit_cost'])
            except InvalidOperation:raise InvalidPolicy('Invalid expected unit cost')
            if not cost.is_finite() or cost<0:raise InvalidPolicy('Invalid expected unit cost')
        if not isinstance(value['cost_provenance'],str) or not value['cost_provenance'].strip():raise InvalidPolicy('Known or unknown cost needs provenance')
        if value['vendor_sku'] is not None and (not isinstance(value['vendor_sku'],str) or len(value['vendor_sku'])>200):raise InvalidPolicy('Vendor SKU must remain text')
    elif kind=='ASSIGNMENT':
        _fields(value,{'variation_id','supplier_id'})
        if uuid(value['variation_id'])!=key:raise InvalidPolicy('Assignment is global per internal variation')
        if value['supplier_id'] is not None:uuid(value['supplier_id'])
    elif kind=='REVIEW':
        _fields(value,{'status','notes'})
        if value['status'] not in ('OPEN','ACKNOWLEDGED') or not isinstance(value['notes'],str) or len(value['notes'])>4000:raise InvalidPolicy('Review cannot repair or close underlying evidence')
    else:raise InvalidPolicy('Unsupported control kind')


def configuration(connection,*,overrides=None):
    rows=connection.execute(text("SELECT * FROM purchasing_v4.control_head WHERE kind IN ('GLOBAL','CATEGORY') ORDER BY kind,record_key")).mappings().all()
    state={(r['kind'],r['record_key']):dict(r) for r in rows}
    state.update(overrides or {})
    global_row=state.get(('GLOBAL','global'));base=live(global_row)
    if not base:return None
    categories=[r for (kind,key),r in sorted(state.items()) if kind=='CATEGORY' and live(r)]
    token=digest([(r['kind'],r['record_key'],r['version']) for r in [global_row]+categories])
    return configuration_from_document(dict(base,id='controls:'+token,categories=[live(r) for r in categories]))


def _apply(connection,*,actor_id,command_id,reason,changes,action='EDIT',conversion=False,removal=False,new_supplier=None,provenance=None):
    owner(connection,actor_id);command_id=uuid(command_id)
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:raise InvalidPolicy('An audit reason is required')
    if not isinstance(changes,list) or not 1<=len(changes)<=100:raise InvalidPolicy('Select between1 and100 records per atomic command')
    # One short management writer lock serializes dependent owner controls; versions still reject stale clients.
    connection.execute(text("SET LOCAL lock_timeout='5s'"));connection.execute(text("SET LOCAL statement_timeout='15s'"))
    connection.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
    request_document=dict(actor_id=actor_id,reason=reason,changes=changes,action=action,new_supplier=new_supplier)
    if provenance is not None:request_document['source_provenance']=provenance
    request_digest=digest(request_document)
    previous=connection.execute(text('SELECT request_digest,result FROM purchasing_v4.control_command WHERE id=:id'),{'id':command_id}).mappings().one_or_none()
    if previous:
        if previous['request_digest']!=request_digest:raise Conflict('Command identity already used for different content')
        return dict(previous['result'],replayed=True)
    keys=[]
    for change in changes:
        _fields(change,{'kind','key','expected_version','value'})
        if change['kind'] not in KINDS or not isinstance(change['key'],str) or not 1<=len(change['key'])<=200 or not _integer(change['expected_version']):raise InvalidPolicy('Invalid control/version')
        keys.append((change['kind'],change['key']))
        validate(change['kind'],change['key'],change['value'],conversion=conversion,removal=removal)
    if len(set(keys))!=len(keys):raise InvalidPolicy('Duplicate record in command')
    current=heads(connection,set(keys));proposed={}
    for change,key in zip(changes,keys):
        actual=current[key]['version'] if key in current else 0
        if actual!=change['expected_version']:raise Conflict('Saved version changed: '+key[0]+':'+key[1])
        proposed[key]=dict(kind=key[0],record_key=key[1],version=actual+1,payload=change['value'])
    if new_supplier:
        if new_supplier not in [key for kind,key in keys if kind=='SUPPLIER']:raise InvalidPolicy('New supplier requires its explicit profile')
        connection.execute(text("INSERT INTO purchasing_v4.authority_supplier(id,origin,provenance) VALUES (:id,'LOCAL',:source)"),{'id':uuid(new_supplier),'source':'Owner command:'+command_id})
    # Validate dependencies against the entire proposed command, never a partially saved batch.
    dependency_keys=set()
    for (kind,key),record in proposed.items():
        value=live(record)
        if kind=='OFFER':dependency_keys.add(('ASSIGNMENT',key.split(':')[0]))
        if not value:continue
        if kind=='ASSIGNMENT' and value['supplier_id']:
            dependency_keys|={('OFFER',key+':'+value['supplier_id']),('SUPPLIER',value['supplier_id'])}
        if kind=='OFFER':dependency_keys.add(('ASSIGNMENT',value['variation_id']))
    dependencies=heads(connection,dependency_keys);dependencies.update(proposed)
    for (kind,key),record in proposed.items():
        value=live(record)
        if kind=='ASSIGNMENT' and value and value['supplier_id']:
            offer=live(dependencies.get(('OFFER',key+':'+value['supplier_id'])));supplier=live(dependencies.get(('SUPPLIER',value['supplier_id'])))
            if not offer or not offer['approved'] or not supplier or not supplier['active']:raise InvalidPolicy('Default supplier must be an explicitly approved offer of an active supplier')
        if kind=='OFFER':
            vid=key.split(':')[0];assignment=live(dependencies.get(('ASSIGNMENT',vid)))
            if assignment and assignment['supplier_id']==key.split(':')[1] and (not value or not value['approved']):raise InvalidPolicy('Resolve the current default before removing its approved alternative')
        if kind=='REVIEW' and not connection.execute(text('SELECT 1 FROM purchasing_v4.review_evidence WHERE id=:id'),{'id':key}).scalar_one_or_none():raise InvalidPolicy('Unknown evidence review')
    if any(k in ('GLOBAL','CATEGORY') for k,_ in keys):
        try:resolved=configuration(connection,overrides=proposed)
        except (ValueError,KeyError,TypeError,ZeroDivisionError) as exc:raise InvalidPolicy(str(exc))
        if not resolved and any(k=='CATEGORY' and live(v) for (k,_),v in proposed.items()):raise InvalidPolicy('Explicit global settings required before a category override')
    result=dict(command_id=command_id,records=[dict(kind=k,record_key=key,version=proposed[(k,key)]['version'],payload=proposed[(k,key)]['payload']) for k,key in keys],replayed=False)
    if provenance is not None:result['source_provenance']=provenance
    connection.execute(text('INSERT INTO purchasing_v4.control_command(id,request_digest,actor_id,reason,action,result) VALUES (:id,:sha,:actor,:reason,:action,CAST(:result AS jsonb))'),dict(id=command_id,sha=request_digest,actor=actor_id,reason=reason,action=action,result=json.dumps(result)))
    for key in sorted(keys):
        record=proposed[key];before=current.get(key)
        params=dict(kind=key[0],key=key[1],version=record['version'],payload=json.dumps(record['payload']),before=json.dumps(before['payload']) if before else None,command=command_id,actor=actor_id,reason=reason)
        connection.execute(text('''INSERT INTO purchasing_v4.control_revision(kind,record_key,version,before_payload,payload,command_id,actor_id,reason)
          VALUES (:kind,:key,:version,CAST(:before AS jsonb),CAST(:payload AS jsonb),:command,:actor,:reason)'''),params)
        connection.execute(text('''INSERT INTO purchasing_v4.control_head(kind,record_key,version,payload,last_command)
          VALUES (:kind,:key,:version,CAST(:payload AS jsonb),:command) ON CONFLICT(kind,record_key) DO UPDATE
          SET version=EXCLUDED.version,payload=EXCLUDED.payload,last_command=EXCLUDED.last_command,updated_at=now()'''),params)
    return result


def _restore(connection,*,actor_id,command_id,reason,target_command,expected):
    owner(connection,actor_id)
    if not isinstance(expected,dict) or len(expected)>100 or any(not _integer(v) for v in expected.values()):raise InvalidPolicy('Explicit current versions required for restore')
    rows=connection.execute(text('SELECT kind,record_key,before_payload FROM purchasing_v4.control_revision WHERE command_id=:id ORDER BY kind,record_key'),{'id':uuid(target_command)}).mappings().all()
    if not rows or len(rows)>100:raise InvalidPolicy('Unknown or oversized restore')
    changes=[]
    for row in rows:
        key=row['kind']+':'+row['record_key']
        if key not in expected:raise InvalidPolicy('Every restored record requires its current expected version')
        changes.append(dict(kind=row['kind'],key=row['record_key'],expected_version=expected[key],value=row['before_payload'] or REMOVED))
    return _apply(connection,actor_id=actor_id,command_id=command_id,reason=reason,changes=changes,action='RESTORE:'+target_command,removal=True)


def _create_supplier(connection,*,actor_id,command_id,reason,supplier_id,profile):
    value=dict(profile,supplier_id=uuid(supplier_id))
    return _apply(connection,actor_id=actor_id,command_id=command_id,reason=reason,
        changes=[dict(kind='SUPPLIER',key=supplier_id,expected_version=0,value=value)],action='CREATE_SUPPLIER',new_supplier=supplier_id)


def apply(connection,*,actor_id,command_id,reason,changes):
    with connection.begin_nested():
        return _apply(connection,actor_id=actor_id,command_id=command_id,reason=reason,changes=changes)


def restore(connection,**kwargs):
    with connection.begin_nested():return _restore(connection,**kwargs)


def create_supplier(connection,**kwargs):
    with connection.begin_nested():return _create_supplier(connection,**kwargs)


def adopt_legacy_manual(connection,*,actor_id,command_id,reason,selections,source_namespace):
    """Explicit owner selection copies verified unit rules; never rewrites their sources."""
    owner(connection,actor_id);command_id=uuid(command_id)
    if not isinstance(selections,list) or not 1<=len(selections)<=100:raise InvalidPolicy('Explicitly select1..100 legacy manual records')
    for row in selections:
        _fields(row,{'legacy_id','source_digest','expected_version'})
        if not _integer(row['legacy_id']) or not _integer(row['expected_version']) or not isinstance(row['source_digest'],str):raise InvalidPolicy('Pinned legacy observations and versions required')
    if len({r['legacy_id'] for r in selections})!=len(selections):raise InvalidPolicy('Duplicate legacy selection')
    selection_digest=digest(sorted(selections,key=lambda r:r['legacy_id']))
    with connection.begin_nested():
        connection.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
        previous=connection.execute(text('SELECT * FROM purchasing_v4.control_command WHERE id=:id'),{'id':command_id}).mappings().one_or_none()
        if previous:
            if (previous['actor_id']!=actor_id or previous['reason']!=reason or previous['action']!='ADOPT_LEGACY_MANUAL'
                    or previous['result'].get('source_provenance',{}).get('selection_digest')!=selection_digest):raise Conflict('Command identity already used for different legacy adoption')
            return dict(previous['result'],replayed=True)
        rows=connection.execute(text('''SELECT p.*,s.active AS store_active,x.ids AS possible_variations FROM public.par_levels p
          LEFT JOIN public.stores s ON s.id=p.store_id
          LEFT JOIN LATERAL(SELECT array_agg(DISTINCT r.variation_id::text ORDER BY r.variation_id::text) AS ids
           FROM public.vendor_sku_configs c JOIN purchasing_v4.variation_reference r ON r.square_variation_id=c.square_variation_id AND r.source_namespace=:ns
           WHERE c.active AND c.vendor_id=p.vendor_id AND c.sku=p.sku) x ON true
          WHERE p.id=ANY(:ids) AND p.par_source='MANUAL' ORDER BY p.id'''),{'ids':[r['legacy_id'] for r in selections],'ns':source_namespace}).mappings().all()
        if len(rows)!=len(selections):raise InvalidPolicy('A selected legacy rule is missing')
        by_id={r['legacy_id']:r for r in selections};changes={};provenance=[]
        for row in rows:
            source=legacy_manual_observation(row)
            if digest(source)!=by_id[row['id']]['source_digest']:raise Conflict('Legacy values or verified mapping changed; review again')
            if not row['store_active'] or not row['possible_variations'] or len(row['possible_variations'])!=1:raise InvalidPolicy('Unresolved legacy identity/store cannot be adopted by inference')
            value=dict(variation_id=row['possible_variations'][0],store_id=row['store_id'],state='ACTIVE',par_units=row['manual_par_level'],level_units=row['manual_stock_up_level'])
            key=value['variation_id']+':'+str(value['store_id']);validate('MANUAL',key,value)
            change=dict(kind='MANUAL',key=key,value=value,expected_version=by_id[row['id']]['expected_version'])
            if key in changes and changes[key]!=change:raise InvalidPolicy('Selected legacy sources disagree for one product-store policy')
            changes[key]=change;provenance.append(source)
        return _apply(connection,actor_id=actor_id,command_id=command_id,reason=reason,changes=list(changes.values()),action='ADOPT_LEGACY_MANUAL',
                      provenance=dict(selection_digest=selection_digest,legacy_records=provenance))


def legacy_manual_observation(row):
    return {key:row[key] for key in ('id','vendor_id','sku','store_id','par_source','manual_par_level','manual_stock_up_level','possible_variations')}


def approved_configuration(connection):
    """Approval gate for future consumers; this function never activates the engine."""
    global_row=heads(connection,{('GLOBAL','global')}).get(('GLOBAL','global'))
    value=live(global_row)
    return configuration(connection) if value and value['status']=='OWNER_APPROVED' else None
