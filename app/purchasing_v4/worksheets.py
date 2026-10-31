"""Owner worksheets consume pinned advice. No commitment, external call or history on patch."""
import json
from uuid import UUID,uuid4,uuid5
from decimal import Decimal
from fractions import Fraction
from sqlalchemy import text
from . import controls
from .identity import digest
from .calculation import final_pack_conversion,recommend_batch
from .calculation_serialization import primitive
from .isolation import require_isolated,readonly_snapshot
from .management_read import list_catalog

MAX_ROWS=5000
FIELDS={'packs','touched','supplier_id','pack_size','removed'}


def js(value):return json.dumps(primitive(value),sort_keys=True)


def editable(value):return {key:value[key] for key in FIELDS}


def effective_original(record):
    # Explicit owner-reviewed additions are pinned separately; original advice
    # and historical prices are never rewritten to admit a newly linked supplier.
    return dict(record['original'],offers={**record['original']['offers'],**record['working'].get('_supplier_offers',{})})


def complete_supplier_offer(offer):
    return bool(offer and offer.get('approved') and offer.get('pack_size') is not None and offer.get('expected_unit_cost') is not None and (not offer.get('use_vendor_sku') or offer.get('vendor_sku')))


def snapshot(c,session_id,actor_id):
    controls.owner(c,actor_id)
    session=c.execute(text('SELECT * FROM purchasing_v4.worksheet WHERE id=:id AND owner_id=:owner'),dict(id=controls.uuid(session_id),owner=actor_id)).mappings().one_or_none()
    if not session:raise controls.InvalidPolicy('Unknown owner worksheet')
    rows=c.execute(text('SELECT id::text,original,working,position FROM purchasing_v4.worksheet_row WHERE session_id=:id ORDER BY position,id'),{'id':session_id}).mappings().all()
    effective={r['id']:effective_original(r) for r in rows}
    state=controls.heads(c,{('ASSIGNMENT',r['original']['variation_id']) for r in rows})
    pack_heads=controls.heads(c,{('OFFER',r['original']['variation_id']+':'+supplier) for r in rows for supplier in effective[r['id']]['offers']})
    seals={}
    if c.execute(text("SELECT to_regclass('purchasing_v4.worksheet_seal')")).scalar_one():
        seals={r['row_id']:dict(r) for r in c.execute(text('SELECT s.row_id::text,v.id::text AS authorization_id,v.kind,v.purchase_order_id FROM purchasing_v4.worksheet_seal s JOIN purchasing_v4.authorized_vendor v ON v.id=s.vendor_authorization_id WHERE s.session_id=:id'),{'id':session_id}).mappings()}
    request_ids=sorted({v['authorization_id'] for v in seals.values() if v['kind']=='REQUEST'})
    if request_ids and c.execute(text("SELECT to_regclass('purchasing_v4.quotation_commitment')")).scalar_one():
        resolutions={(r['request_id'],r['row_id']):dict(r) for r in c.execute(text("""SELECT h.request_id::text,i.row_id::text,h.state,v.purchase_order_id,coalesce(sum(x.approved_units),0)::bigint AS approved_units
          FROM purchasing_v4.quotation_head h JOIN purchasing_v4.purchasing_intent i ON i.vendor_authorization_id=h.request_id
          LEFT JOIN purchasing_v4.quotation_commitment b ON b.request_id=h.request_id LEFT JOIN purchasing_v4.authorized_vendor v ON v.id=b.vendor_authorization_id
          LEFT JOIN purchasing_v4.quotation_intent_resolution x ON x.intent_id=i.id
          WHERE h.request_id=ANY(CAST(:ids AS uuid[])) GROUP BY h.request_id,i.row_id,h.state,v.purchase_order_id"""),{'ids':request_ids}).mappings()}
        for row_id,seal in seals.items():
            resolution=resolutions.get((seal['authorization_id'],row_id))
            if resolution:seal.update(request_state=resolution['state'],approved_quote_units=resolution['approved_units'] if resolution['state']=='COMMITTED' else None,resolved_purchase_order_id=resolution['purchase_order_id'] if resolution['approved_units']>0 else None)
    from .recommendation_status import describe
    advice={}
    if session['generation_id']:
        for item in c.execute(text("""SELECT x.value->>'variation_id' AS vid,x.value->>'store_id' AS sid,
          x.value->>'state' AS state,x.value->'reasons' AS reasons
          FROM purchasing_v4.worksheet_generation g CROSS JOIN LATERAL
          jsonb_array_elements(g.evidence#>'{recommendations,recommendations}') x(value)
          WHERE g.id=:id"""),{'id':session['generation_id']}).mappings():
            advice[(item['vid'],item['sid'])]=describe(item['state'],item['reasons'])
    calculated_at=c.execute(text('SELECT as_of FROM purchasing_v4.worksheet_generation WHERE id=:id'),{'id':session['generation_id']}).scalar_one_or_none() if session['generation_id'] else None
    result=[]
    for r in rows:
        head=state.get(('ASSIGNMENT',r['original']['variation_id']));assignment=controls.live(head)
        current=effective[r['id']];selected=current['offers'].get(r['working']['supplier_id']) or {}
        pack_sizes={supplier:(controls.live(pack_heads.get(('OFFER',r['original']['variation_id']+':'+supplier))) or {}).get('pack_size') for supplier in current['offers']}
        projected=dict(current,pack_sizes=pack_sizes,unit_cost=selected.get('expected_unit_cost'),vendor_sku=selected.get('vendor_sku'),recommendation_status=[advice.get((r['original']['variation_id'],str(store['id']))) for store in session['stores']])
        linked=r['working']['supplier_id'] in r['working'].get('_supplier_offers',{})
        if linked:
            complete=complete_supplier_offer(selected);projected['supplier_setup_complete']=complete
            projected['warnings']=sorted(set(projected['warnings'])|({'SUPPLIER_SETUP_INCOMPLETE'} if not complete else set()))
            statuses=list(projected['recommendation_status'])
            for i,status in enumerate(statuses):
                if status and status['state']=='NO_REORDER' or 'GLOBAL_NO_REORDER' in projected['warnings']:continue
                if not complete:statuses[i]=dict(state='UNRESOLVED',explanation='Supplier setup is incomplete. Confirm the product mapping, pack size and cost before approving calculated purchasing.')
                elif r['working']['packs'][i] is not None and not r['working']['touched'][i] and current['advice_units'][i] is not None:statuses[i]=dict(state='RECOMMENDED',explanation='Repacked from saved worksheet advice using the owner-approved supplier offer.')
                elif current['advice_units'][i] is None:statuses[i]=dict(state='UNRESOLVED',explanation='Saved worksheet advice is unresolved. Start fresh to calculate using the approved supplier setup.')
            projected['recommendation_status']=statuses
        result.append(dict(projected,**editable(r['working']),id=r['id'],position=r['position'],sealed=seals.get(r['id']),original=r['original'],assignment_version=head['version'] if head else 0,default_supplier_id=assignment['supplier_id'] if assignment else None))
    return primitive(dict(session=dict(session,calculated_at=calculated_at,id=str(session['id']),generation_id=str(session['generation_id']) if session['generation_id'] else None),rows=result))


def catalog_rows(c,ids,namespace):
    catalog=c.execute(text('''SELECT c.variation_id::text,c.label,c.sku,c.categories,r.evidence AS observation
      FROM purchasing_v4.management_catalog c LEFT JOIN LATERAL (SELECT to_jsonb(i) AS evidence FROM public.ordering_catalog_identity i WHERE i.square_variation_id=c.square_variation_id) r ON true
      WHERE c.variation_id=ANY(CAST(:ids AS uuid[])) ORDER BY c.variation_id'''),{'ids':ids}).mappings().all()
    state=controls.heads(c,{('ASSIGNMENT',vid) for vid in ids})
    offers=c.execute(text("SELECT record_key,payload,version FROM purchasing_v4.control_head WHERE kind='OFFER' AND payload->>'variation_id'=ANY(:ids) AND payload->>'approved'='true'"),{'ids':ids}).mappings().all()
    suppliers=controls.heads(c,{('SUPPLIER',r['payload']['supplier_id']) for r in offers})
    by={vid:{} for vid in ids}
    for offer in offers:
        p=offer['payload'];profile=controls.live(suppliers.get(('SUPPLIER',p['supplier_id'])))
        if profile and profile['active']:
            cost=p['expected_unit_cost']
            canonical_cost=None if cost is None else '0' if Decimal(cost)==0 else str(Decimal(cost))
            by[p['variation_id']][p['supplier_id']]=dict(p,expected_unit_cost=canonical_cost,recorded_unit_cost=cost,name=profile['name'],workflow='Spreadsheet confirmation' if profile['confirmation_enabled'] else 'Direct PO',profile_version=suppliers[('SUPPLIER',p['supplier_id'])]['version'],offer_version=offer['version'])
    rows=[]
    for r in catalog:
        vid=r['variation_id'];assignment=state.get(('ASSIGNMENT',vid));assigned=controls.live(assignment);sid=assigned['supplier_id'] if assigned else None
        offer=by[vid].get(sid);obs=r['observation'] or {};name=obs.get('item_name') or obs.get('product_name') or r['label'];variation=obs.get('variation_name') or ''
        rows.append(dict(id=str(uuid4()),variation_id=vid,name=name,variation=variation,sku=r['sku'],gtin=str((obs.get('catalog_attributes') or {}).get('gtin') or ''),categories=r['categories'],supplier_id=sid,offers=by[vid],pack_size=offer['pack_size'] if offer else None,unit_cost=offer['expected_unit_cost'] if offer else None,vendor_sku=offer['vendor_sku'] if offer else None,assignment_version=assignment['version'] if assignment else 0,packs=[None]*4,touched=[False]*4,advice_units=[None]*4,policy=['Unqualified']*4,warnings=['Supplier required'] if not offer else [],removed=False))
    return rows


def worksheet_stores(c):
    rows=[dict(r) for r in c.execute(text('SELECT id,name FROM public.stores WHERE active ORDER BY id')).mappings()]
    if len(rows)!=4:raise controls.InvalidPolicy('Exactly four active stores must be configured when starting a worksheet')
    order={name:i for i,name in enumerate(('andresen','hwy 99','sr 503','longview'))}
    # Store identities come from the existing registry; Addendum A supplies display order.
    if {r['name'].casefold() for r in rows}==set(order):rows.sort(key=lambda r:order[r['name'].casefold()])
    else:raise controls.InvalidPolicy('Store display identity/order needs explicit review')
    return rows


def create(c,*,actor_id,command_id,title,rows,generation=None):
    controls.owner(c,actor_id);require_isolated(c);sid=controls.uuid(command_id)
    existing=c.execute(text('SELECT owner_id FROM purchasing_v4.worksheet WHERE id=:id'),{'id':sid}).scalar_one_or_none()
    if existing is not None:
        if existing!=actor_id:raise controls.InvalidPolicy('Worksheet identity belongs to another owner')
        return snapshot(c,sid,actor_id)
    if len(rows)>MAX_ROWS:raise controls.InvalidPolicy('Worksheet exceeds qualified row limit')
    gid=None
    if generation:
        gid=str(uuid4());c.execute(text('''INSERT INTO purchasing_v4.worksheet_generation(id,owner_id,configuration_digest,input_digest,as_of,evidence)
          VALUES (:id,:owner,:config,:input,:asof,CAST(:evidence AS jsonb))'''),dict(id=gid,owner=actor_id,config=generation['configuration_digest'],input=generation['input_digest'],asof=generation['as_of'],evidence=js(generation)))
    c.execute(text("INSERT INTO purchasing_v4.worksheet(id,owner_id,generation_id,title,state,stores) VALUES (:id,:owner,:gid,:title,'EDITABLE',CAST(:stores AS jsonb))"),dict(id=sid,owner=actor_id,gid=gid,title=str(title)[:200] or 'Purchasing worksheet',stores=js(worksheet_stores(c))))
    for index,row in enumerate(rows):
        validate_row(row)
        c.execute(text('''INSERT INTO purchasing_v4.worksheet_row(session_id,id,variation_id,position,original,working)
        VALUES (:sid,:id,:vid,:position,CAST(:original AS jsonb),CAST(:working AS jsonb))'''),dict(sid=sid,id=row['id'],vid=row['variation_id'],position=index,original=js(row),working=js({k:row[k] for k in FIELDS})))
    return snapshot(c,sid,actor_id)


def validate_row(row,prior=None,pinned=None):
    if row['supplier_id'] is not None and row['supplier_id'] not in row['offers']:raise controls.InvalidPolicy('Choose an approved supplier offer; no inferred assignment')
    if row['pack_size'] is not None and (type(row['pack_size']) is not int or not 1<=row['pack_size']<=1000000):raise controls.InvalidPolicy('Pack size must be a positive whole number or unresolved')
    if not isinstance(row['packs'],list) or len(row['packs'])!=4 or any(v is not None and (type(v) is not int or not 0<=v<=1000000) for v in row['packs']):raise controls.InvalidPolicy('Four nonnegative whole-pack values or unresolved states required')
    if not isinstance(row['touched'],list) or len(row['touched'])!=4 or any(type(v) is not bool for v in row['touched']):raise controls.InvalidPolicy('Four owner-edit indicators required')
    if type(row['removed']) is not bool:raise controls.InvalidPolicy('Explicit removal state required')
    for index,(quantity,touched,advice) in enumerate(zip(row['packs'],row['touched'],row['advice_units'])):
        # Retain advice pinned by prior algorithm versions; do not recalculate saved rows.
        retained=any(source is not None and row['supplier_id']==source['supplier_id'] and row['pack_size']==source['pack_size'] and quantity==source['packs'][index] and touched==source['touched'][index] for source in (prior,pinned))
        if not touched and not retained:
            selected=row['offers'].get(row['supplier_id']) or {}
            pending=selected.get('_worksheet_link') and not complete_supplier_offer(selected)
            expected=0 if pending and 'GLOBAL_NO_REORDER' in row.get('warnings',[]) else None if pending else final_pack_conversion(Fraction(advice),row['pack_size']) if advice is not None and row['pack_size'] is not None else None
            if quantity!=expected:raise controls.InvalidPolicy('Untouched quantities must match pinned unit advice at the chosen pack size')


def totals(rows):
    packs=0;known=Fraction();unknown=0;unresolved=0;excluded=0;products=0
    for row in rows:
        if row['removed']:continue
        products+=1;values=row['packs'];packs+=sum(v for v in values if v is not None);exclusions=[not row['touched'][i] and ('GLOBAL_NO_REORDER' in row.get('warnings',[]) or (row.get('recommendation_status') or [None]*4)[i] and row['recommendation_status'][i]['state']=='NO_REORDER') for i in range(4)];unresolved+=sum((v is None or row.get('supplier_setup_complete') is False) and not exclusions[i] for i,v in enumerate(values));excluded+=sum(bool(x) for x in exclusions)
        offer=row['offers'].get(row['supplier_id']) or {};cost=offer.get('expected_unit_cost')
        if any(v and v>0 for v in values):
            if cost is None or row['pack_size'] is None:unknown+=1
            else:known+=Fraction(Decimal(cost))*row['pack_size']*sum(v for v in values if v is not None)
    denominator=known.denominator;twos=fives=0
    while denominator%2==0:twos+=1;denominator//=2
    while denominator%5==0:fives+=1;denominator//=5
    assert denominator==1
    scale=max(twos,fives);digits=str(known.numerator*(10**scale//known.denominator)).rjust(scale+1,'0')
    exact_cost=digits[:-scale]+'.'+digits[-scale:] if scale else digits
    return dict(products=products,packs=packs,known_cost=exact_cost,unknown_cost_rows=unknown,unresolved_cells=unresolved,no_reorder_exclusions=excluded)


def patch(c,*,actor_id,session_id,command_id,expected_version,changes,defaults=None,discard=False,add=None,pack_authority_enabled=True):
    controls.owner(c,actor_id);sid=controls.uuid(session_id);cid=controls.uuid(command_id);defaults=defaults or [];add=add or []
    if not isinstance(changes,list) or len(changes)>MAX_ROWS or len({v['id'] for v in changes})!=len(changes):raise controls.InvalidPolicy('Bounded distinct row changes required')
    if type(discard) is not bool:raise controls.InvalidPolicy('Explicit draft disposal state required')
    if type(expected_version) is not int:raise controls.InvalidPolicy('Expected worksheet version required')
    request_digest=digest(dict(expected_version=expected_version,changes=changes,defaults=defaults,discard=discard,add=add,actor=actor_id))
    with c.begin_nested():
        session=c.execute(text('SELECT * FROM purchasing_v4.worksheet WHERE id=:id AND owner_id=:owner FOR UPDATE'),dict(id=sid,owner=actor_id)).mappings().one_or_none()
        if not session:raise controls.InvalidPolicy('Unknown owner worksheet')
        receipt=c.execute(text('SELECT * FROM purchasing_v4.worksheet_command WHERE session_id=:sid AND id=:id'),dict(sid=sid,id=cid)).mappings().one_or_none()
        if receipt:
            if receipt['request_digest']!=request_digest:raise controls.Conflict('Command identity reused for different changes')
            return dict(receipt['result'],replayed=True)
        if session['state']!='EDITABLE' or session['version']!=expected_version:raise controls.Conflict('Worksheet changed. Both versions are retained; compare before rebasing.')
        if add:
            if len(add)>100 or len({r['variation_id'] for r in add})!=len(add):raise controls.InvalidPolicy('Explicit add selection bounded at 100')
            count=c.execute(text('SELECT count(*) FROM purchasing_v4.worksheet_row WHERE session_id=:id'),{'id':sid}).scalar_one()
            if count+len(add)>MAX_ROWS:raise controls.InvalidPolicy('Worksheet exceeds qualified row limit')
            for offset,row in enumerate(add):
                validate_row(row)
                c.execute(text('INSERT INTO purchasing_v4.worksheet_row(session_id,id,variation_id,position,original,working) VALUES (:sid,:id,:vid,:position,CAST(:original AS jsonb),CAST(:working AS jsonb))'),dict(sid=sid,id=row['id'],vid=row['variation_id'],position=count+offset,original=js(row),working=js({k:row[k] for k in FIELDS})))
        originals=c.execute(text('SELECT id::text,original,working FROM purchasing_v4.worksheet_row WHERE session_id=:sid AND id=ANY(CAST(:ids AS uuid[]))'),dict(sid=sid,ids=[controls.uuid(v['id']) for v in changes])).mappings().all()
        before={r['id']:r for r in originals};changed=[];inverse=[]
        for item in changes:
            if item['id'] not in before:raise controls.InvalidPolicy('Unknown worksheet row; use explicit Add Product')
            if not isinstance(item['value'],dict) or set(item['value'])!=FIELDS:raise controls.InvalidPolicy('Complete editable row fields required')
            original=effective_original(before[item['id']]);value=dict(original,**item['value']);validate_row(value,prior=before[item['id']]['working'],pinned=original)
            inverse.append(dict(id=item['id'],value=editable(before[item['id']]['working'])));changed.append(item)
        # The owner-defined spreadsheet pack is both supplier purchasing and store
        # distribution authority. Persist only explicit same-supplier pack edits,
        # atomically with this worksheet; retain costs, identity and all other fields.
        pack_mutations=[]
        for item in changed:
            old=before[item['id']]['working'];new=item['value'];original=before[item['id']]['original']
            if new['supplier_id']!=old['supplier_id']:
                # New vendor selection uses today's saved pack authority; prices
                # and original allocations stay pinned to the worksheet.
                if new['supplier_id']:
                    key=original['variation_id']+':'+new['supplier_id'];current=controls.live(controls.heads(c,{('OFFER',key)}).get(('OFFER',key)))
                    if not current or not current['approved'] or new['pack_size']!=current['pack_size']:raise controls.Conflict('Supplier pack configuration changed. Reload before selecting this supplier.')
                continue
            if new['pack_size']==old['pack_size'] or new['pack_size'] is None:continue
            if not new['supplier_id']:raise controls.InvalidPolicy('Choose an approved supplier and a positive pack size before saving distribution packs')
            key=original['variation_id']+':'+new['supplier_id']
            head=controls.heads(c,{('OFFER',key)}).get(('OFFER',key));offer=controls.live(head)
            if not offer or not offer['approved']:raise controls.InvalidPolicy('Approved supplier offer required to save product pack size')
            if offer['pack_size']!=old['pack_size']:raise controls.Conflict('Saved product pack size changed. Review the current supplier offer before editing this older worksheet.')
            pack_mutations.append(dict(kind='OFFER',key=key,expected_version=head['version'],value=dict(offer,pack_size=new['pack_size'])))
        if pack_mutations:
            if not pack_authority_enabled:raise controls.InvalidPolicy('Saved product pack configuration is paused; existing worksheet quantities remain editable')
            if len(pack_mutations)>100:raise controls.InvalidPolicy('Change saved product pack sizes for at most 100 products at a time')
            controls.apply(c,actor_id=actor_id,command_id=str(uuid5(UUID(cid),'product-packs:'+sid)),reason='Owner spreadsheet purchasing and store distribution pack sizes for worksheet '+sid+' command '+cid,changes=pack_mutations)
        inverse_defaults=[]
        if defaults:
            if not isinstance(defaults,list) or len(defaults)>100:raise controls.InvalidPolicy('Default supplier changes bounded at 100')
            mutations=[]
            for d in defaults:
                key=controls.uuid(d['variation_id']);old=controls.heads(c,{('ASSIGNMENT',key)}).get(('ASSIGNMENT',key));prior=controls.live(old)
                mutations.append(dict(kind='ASSIGNMENT',key=key,expected_version=d['expected_version'],value=dict(variation_id=key,supplier_id=d['supplier_id'])))
                inverse_defaults.append(dict(variation_id=key,supplier_id=prior['supplier_id'] if prior else None,expected_version=d['expected_version']+1))
            controls.apply(c,actor_id=actor_id,command_id=str(uuid5(UUID(cid),'supplier-defaults:'+sid)),reason='Explicit Change Default Vendor for worksheet '+sid+' command '+cid,changes=mutations)
        for item in changed:c.execute(text('UPDATE purchasing_v4.worksheet_row SET working=CAST(:value AS jsonb) WHERE session_id=:sid AND id=:id'),dict(value=js(dict(before[item['id']]['working'],**item['value'])),sid=sid,id=item['id']))
        version=expected_version+1
        c.execute(text('UPDATE purchasing_v4.worksheet SET version=:version,state=:state,updated_at=now() WHERE id=:id'),dict(id=sid,version=version,state='DISCARDED' if discard else 'EDITABLE'))
        result=dict(command_id=cid,version=version,added=add,acknowledged_ids=[r['id'] for r in changed],inverse=inverse,inverse_defaults=inverse_defaults,discarded=discard,replayed=False)
        c.execute(text('''INSERT INTO purchasing_v4.worksheet_command(session_id,id,actor_id,request_digest,before_version,after_version,changes,result)
        VALUES (:sid,:id,:actor,:sha,:before,:after,CAST(:changes AS jsonb),CAST(:result AS jsonb))'''),dict(sid=sid,id=cid,actor=actor_id,sha=request_digest,before=expected_version,after=version,changes=js(dict(rows=changed,defaults=defaults,discard=discard,add=add)),result=js(result)))
        return result


def policy_label(row):
    if row.manual is not None:return 'Manual'
    if 'EXPLICIT_INACTIVE_MANUAL_POLICY' in row.policy_issues:return 'Manual inactive'
    if any(code.startswith(('UNRESOLVED_LEGACY_MANUAL_','INVALID_LEGACY_MANUAL_','UNRESOLVED_FROZEN_LEGACY_MANUAL_')) for code in row.policy_issues):return 'Manual review'
    return 'Automatic'


def prepare_generation(engine,*,actor_id,source_namespace,as_of,max_age=None):
    from .calculation_source import load_snapshot
    from .policy_projection import project_rows
    with readonly_snapshot(engine) as c:
        controls.owner(c,actor_id);config=controls.approved_configuration(c)
        if config is None:raise controls.InvalidPolicy('Approve explicit engine settings before Start Fresh, or start an empty manual worksheet')
        max_age=controls.evidence_age(c,max_age)
        if max_age is None:raise controls.InvalidPolicy('Set maximum source verification age in Order Engine Logic before calculating')
        source=load_snapshot(c,config,as_of=as_of,max_age=max_age,source_namespace=source_namespace)
        projected=project_rows(c,source.rows);batch=recommend_batch(projected,config,as_of=as_of,input_digest=source.digest)
        stores=worksheet_stores(c)
        if len(stores)!=4:raise controls.InvalidPolicy('Four configured stores required')
        store_index={str(s['id']):i for i,s in enumerate(stores)}
        projected_by_key={(r.variation_id,r.store_id):r for r in projected}
        rows=catalog_rows(c,sorted({r.variation_id for r in projected}),source_namespace);by={r['variation_id']:r for r in rows}
        for result in batch.recommendations:
            row=by[result.variation_id];index=store_index[result.store_id]
            row['packs'][index]=result.suggested_packs if result.raw_need_units is not None and result.pack_size is not None else None
            row['advice_units'][index]=str(result.raw_need_units) if result.raw_need_units is not None else None
            row['policy'][index]=policy_label(projected_by_key[(result.variation_id,result.store_id)])
            row['warnings']=sorted(set(row['warnings'])|set(result.reasons))
        return rows,dict(configuration_digest=config.content_digest,input_digest=source.digest,as_of=as_of.isoformat(),recommendations=primitive(batch))


def owner_receipt(c,actor_id,command_id,action,request):
    controls.owner(c,actor_id)
    record=c.execute(text('SELECT * FROM purchasing_v4.worksheet_owner_command WHERE id=:id'),{'id':controls.uuid(command_id)}).mappings().one_or_none()
    if record:
        if record['owner_id']!=actor_id or record['action']!=action or record['request_digest']!=digest(request):raise controls.Conflict('Owner command identity reused for a different request')
        return record['result']
    return None


def record_owner_receipt(c,actor_id,command_id,action,request,result):
    c.execute(text('INSERT INTO purchasing_v4.worksheet_owner_command(id,owner_id,action,request_digest,result) VALUES (:id,:owner,:action,:sha,CAST(:result AS jsonb))'),dict(id=controls.uuid(command_id),owner=actor_id,action=action,sha=digest(request),result=js(result)))


def discard_selected(c,*,actor_id,command_id,selections):
    if not isinstance(selections,list) or not 1<=len(selections)<=100 or len({v['id'] for v in selections})!=len(selections):raise controls.InvalidPolicy('Select 1..100 distinct drafts explicitly')
    request=sorted(selections,key=lambda v:v['id'])
    with c.begin_nested():
        replay=owner_receipt(c,actor_id,command_id,'DISCARD_SELECTED',request)
        if replay:return dict(replay,replayed=True)
        # Acquire a deterministic set of aggregate locks and validate every version before any disposal.
        rows=c.execute(text('SELECT id::text,version,state FROM purchasing_v4.worksheet WHERE owner_id=:owner AND id=ANY(CAST(:ids AS uuid[])) ORDER BY id FOR UPDATE'),dict(owner=actor_id,ids=[controls.uuid(v['id']) for v in request])).mappings().all()
        if len(rows)!=len(request) or any(r['state']!='EDITABLE' or r['version']!=v['expected_version'] for r,v in zip(rows,request)):raise controls.Conflict('One selected draft changed; no drafts discarded')
        receipts=[patch(c,actor_id=actor_id,session_id=v['id'],command_id=str(uuid5(UUID(controls.uuid(command_id)),v['id'])),expected_version=v['expected_version'],changes=[],discard=True) for v in request]
        result=dict(discarded_ids=[v['id'] for v in request],receipts=receipts,replayed=False)
        record_owner_receipt(c,actor_id,command_id,'DISCARD_SELECTED',request,result);return result


def no_reorder(c,*,actor_id,session_id,command_id,expected_version,row_ids):
    """One owner-confirmed transaction: global exclusion and current-draft removal."""
    controls.owner(c,actor_id);sid=controls.uuid(session_id);cid=controls.uuid(command_id)
    if not isinstance(row_ids,list) or not 1<=len(row_ids)<=MAX_ROWS or any(not isinstance(v,str) for v in row_ids):raise controls.InvalidPolicy('Select worksheet rows explicitly')
    ids=sorted(controls.uuid(v) for v in row_ids)
    if len(set(ids))!=len(ids):raise controls.InvalidPolicy('Select distinct worksheet rows')
    if type(expected_version) is not int or expected_version<0:raise controls.InvalidPolicy('Worksheet version required')
    request=dict(session_id=sid,expected_version=expected_version,row_ids=ids)
    with c.begin_nested():
        session=c.execute(text('SELECT version,state FROM purchasing_v4.worksheet WHERE id=:id AND owner_id=:owner FOR UPDATE'),dict(id=sid,owner=actor_id)).mappings().one_or_none()
        if not session:raise controls.InvalidPolicy('Unknown owner worksheet')
        replay=owner_receipt(c,actor_id,cid,'NO_REORDER',request)
        if replay:return dict(replay,replayed=True)
        # No Reorder sets eligibility/removal only. Read current values under
        # the worksheet lock so an older open tab cannot overwrite newer edits.
        if session['state']!='EDITABLE' or expected_version>session['version']:raise controls.Conflict('This worksheet is no longer available for this change')
        rows=c.execute(text('SELECT id::text,variation_id::text,working FROM purchasing_v4.worksheet_row WHERE session_id=:sid AND id=ANY(CAST(:ids AS uuid[])) ORDER BY id'),dict(sid=sid,ids=ids)).mappings().all()
        if len(rows)!=len(ids):raise controls.InvalidPolicy('Selected rows must belong to this worksheet')
        if c.execute(text("SELECT to_regclass('purchasing_v4.worksheet_seal')")).scalar_one():
            if c.execute(text('SELECT EXISTS(SELECT 1 FROM purchasing_v4.worksheet_seal WHERE session_id=:sid AND row_id=ANY(CAST(:ids AS uuid[])))'),dict(sid=sid,ids=ids)).scalar_one():raise controls.Conflict('A selected row is sealed; no purchasing status changed')
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
        vids=sorted({r['variation_id'] for r in rows});heads=controls.heads(c,{('PRODUCT',v) for v in vids});mutations=[]
        for vid in vids:
            head=heads.get(('PRODUCT',vid));current=controls.live(head)
            if current and current['no_reorder']:continue
            mutations.append(dict(kind='PRODUCT',key=vid,expected_version=head['version'] if head else 0,value=dict(variation_id=vid,no_reorder=True,coverage=current['coverage'] if current else None)))
        for offset in range(0,len(mutations),100):
            controls.apply(c,actor_id=actor_id,command_id=str(uuid5(UUID(cid),'no-reorder:'+str(offset))),reason='Owner confirmed No Reorder from worksheet '+sid+' command '+cid,changes=mutations[offset:offset+100])
        receipt=patch(c,actor_id=actor_id,session_id=sid,command_id=str(uuid5(UUID(cid),'remove-rows')),expected_version=session['version'],changes=[dict(id=r['id'],value=dict(editable(r['working']),removed=True)) for r in rows])
        result=dict(command_id=cid,version=receipt['version'],removed_row_ids=ids,variation_count=len(vids),replayed=False)
        record_owner_receipt(c,actor_id,cid,'NO_REORDER',request,result)
        return result
