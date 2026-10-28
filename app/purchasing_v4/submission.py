"""Explicit reviewed purchasing authority. No network, generation or inventory writer."""
from datetime import datetime,timezone
from collections import Counter,defaultdict
import sys
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from uuid import UUID,uuid5
from sqlalchemy import text
from . import controls,worksheets
from .identity import digest,valid_reference
from .isolation import require_isolated,readonly_snapshot
from .evidence_rules import INCOMING_SQL,incoming_quantity
from .calculation_serialization import primitive

SUPPLY_LOCK=73520405
WRITER='erupted_v4_po_writer'


def configure_owner(c,principal_id):
    controls.owner(c,principal_id)
    prior=c.execute(text('SELECT principal_id FROM purchasing_v4.commitment_owner')).scalar_one_or_none()
    if prior is not None and prior!=principal_id:raise controls.Conflict('Authorization owner is immutable; an explicit reviewed authority migration is required')
    if prior is None:c.execute(text('INSERT INTO purchasing_v4.commitment_owner(principal_id) VALUES (:id)'),{'id':principal_id})


def owner(c,actor_id):
    controls.owner(c,actor_id)
    if c.execute(text('SELECT principal_id FROM purchasing_v4.commitment_owner')).scalar_one_or_none()!=actor_id:raise PermissionError('Only the explicitly configured purchasing owner may authorize or prepare purchasing reviews')


def current_configuration(c,rows):
    keys={('SUPPLIER',r['supplier_id']) for r in rows}|{('OFFER',r['variation_id']+':'+r['supplier_id']) for r in rows}
    values=controls.heads(c,keys)
    profiles={sid:controls.live(values.get(('SUPPLIER',sid))) for _,sid in keys if _=='SUPPLIER'}
    for sid,profile in profiles.items():
        if not profile or not profile['active']:raise controls.InvalidPolicy('Selected supplier is inactive or unresolved; review supplier authority')
    for row in rows:
        offer=controls.live(values.get(('OFFER',row['variation_id']+':'+row['supplier_id'])))
        if not offer or not offer['approved']:raise controls.InvalidPolicy('Selected supplier offer is no longer approved; review it explicitly')
    return profiles,digest([dict(kind=k,key=key,head={field:value for field,value in (values.get((k,key)) or {}).items() if field!='updated_at'}) for k,key in sorted(keys)]),values


def supply_snapshot(c,rows,stores,namespace,*,since):
    """Current authoritative PO facts plus separate outstanding-request evidence.

    Shared Phase1 incoming projection is counted once; unconfirmed requests are
    warnings/reservations, never invented physical incoming inventory.
    """
    ids=sorted({r['variation_id'] for r in rows})
    refs={r['variation_id']:r['square_variation_id'] for r in c.execute(text('SELECT variation_id::text,square_variation_id FROM purchasing_v4.variation_reference WHERE source_namespace=:ns AND variation_id=ANY(CAST(:ids AS uuid[]))'),dict(ns=namespace,ids=ids)).mappings()}
    if set(refs)!=set(ids):raise controls.InvalidPolicy('Unresolved external variation identity prevents an exact supply review')
    from .receiving_supply import incoming_sql
    legacy=[dict(r) for r in c.execute(text(incoming_sql(c,INCOMING_SQL))).mappings()]
    # Include official-but-not-issued legacy drafts as warnings, not incoming stock.
    draft=[dict(r) for r in c.execute(text('''SELECT p.id AS purchase_order_id,l.id AS line_id,l.variation_id,l.ordered_qty,l.unit_cost,
       a.store_id,a.allocated_qty FROM public.purchase_orders p JOIN public.purchase_order_lines l ON l.purchase_order_id=p.id
       LEFT JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id
       WHERE p.status='DRAFT' AND NOT l.removed AND l.ordered_qty>0 AND l.variation_id=ANY(:squares) ORDER BY p.id,l.id,a.store_id'''),dict(squares=list(refs.values()))).mappings()]
    pending_query='''SELECT i.id::text AS intent_id,w.variation_id::text,i.store_id,i.units,v.id::text AS request_id,v.supplier_id::text
       FROM purchasing_v4.request_reservation r JOIN purchasing_v4.purchasing_intent i ON i.id=r.intent_id
       JOIN purchasing_v4.worksheet_row w ON w.session_id=i.session_id AND w.id=i.row_id
       JOIN purchasing_v4.authorized_vendor v ON v.id=r.vendor_authorization_id
       WHERE w.variation_id=ANY(CAST(:ids AS uuid[])) AND NOT EXISTS(SELECT 1 FROM purchasing_v4.intent_consumption x WHERE x.intent_id=i.id)
       ORDER BY i.id'''
    if c.execute(text("SELECT to_regclass('purchasing_v4.active_request_reservation')")).scalar_one():
        pending_query=pending_query.replace('purchasing_v4.request_reservation r','purchasing_v4.active_request_reservation r').replace('i.store_id,i.units,','i.store_id,r.units,')
    pending=[dict(r) for r in c.execute(text(pending_query),dict(ids=ids)).mappings()]
    recent=[dict(r) for r in c.execute(text('''SELECT p.id AS purchase_order_id,l.id AS line_id,l.variation_id,p.status::text,l.ordered_qty,l.received_qty_total,
      extract(epoch FROM coalesce(p.ordered_at,p.submitted_at,p.created_at))::text AS ordered_epoch,
      (SELECT jsonb_agg(jsonb_build_object('receipt_id',q.id,'status',q.status,'received_epoch',extract(epoch FROM coalesce(q.received_at,q.created_at)),
        'line_received',rl.received_qty) ORDER BY q.id,rl.id) FROM public.purchase_order_receipts q
        LEFT JOIN public.purchase_order_receipt_lines rl ON rl.receipt_id=q.id AND rl.purchase_order_line_id=l.id
        WHERE q.purchase_order_id=p.id AND q.status='SUBMITTED' AND coalesce(q.received_at,q.created_at)>=:since) AS receipt_changes
      FROM public.purchase_orders p JOIN public.purchase_order_lines l ON l.purchase_order_id=p.id
      WHERE l.variation_id=ANY(:squares) AND l.ordered_qty>0 AND NOT l.removed AND p.status<>'DRAFT'
        AND (coalesce(p.ordered_at,p.submitted_at,p.created_at)>=:since OR EXISTS(SELECT 1 FROM public.purchase_order_receipts q
          WHERE q.purchase_order_id=p.id AND q.status='SUBMITTED' AND coalesce(q.received_at,q.created_at)>=:since))
      ORDER BY p.id,l.id'''),dict(squares=list(refs.values()),since=datetime.fromisoformat(since) if isinstance(since,str) else since)).mappings()]
    store_rows=[dict(r) for r in c.execute(text('SELECT id,square_location_id,active FROM public.stores ORDER BY id')).mappings()]
    locations=Counter(s['square_location_id'] for s in store_rows if s['active'] and s['square_location_id'])
    if any(not any(s['id']==b['id'] and s['active'] and s['square_location_id'] and locations[s['square_location_id']]==1 for s in store_rows) for b in stores):raise controls.InvalidPolicy('Incoming supply has unresolved store location evidence')
    known=set(c.execute(text('SELECT square_variation_id FROM public.ordering_catalog_identity WHERE square_variation_id=ANY(:refs)'),dict(refs=list(refs.values()))).scalars())
    if known!=set(refs.values()):raise controls.InvalidPolicy('Incoming supply has an unresolved variation reference')
    by_square=defaultdict(list);by_draft=defaultdict(list);by_pending=defaultdict(list);by_history=defaultdict(list);unresolved=[]
    for line in legacy:
        if valid_reference(line['variation_id']):by_square[line['variation_id']].append(line)
        else:unresolved.append(line)
    for line in draft:by_draft[line['variation_id']].append(line)
    for line in pending:by_pending[line['variation_id']].append(line)
    for line in recent:by_history[line['variation_id']].append(line)
    incoming={};overlap={}
    for r in rows:
        vid=r['variation_id'];square=refs[vid];values,reason,references=incoming_quantity(by_square[square]+unresolved,square)
        if reason:raise controls.InvalidPolicy('Incoming supply is unresolved: '+reason)
        incoming[vid]=[values.get(s['id'],0) for s in stores]
        overlaps=[]
        for line in by_square[square]:
            if line['variation_id']==square and line['store_id'] is not None:
                remaining=max(line['allocated_qty']-line['store_received_qty']-line['closed'],0)
                if remaining>0:overlaps.append(dict(kind='COMMITTED_INCOMING',reference='po:'+str(line['purchase_order_id'])+'/line:'+str(line['id']),store_id=line['store_id'],units=remaining))
        for line in by_draft[square]:
            if line['variation_id']==square:overlaps.append(dict(kind='LEGACY_DRAFT_PO',reference='po:'+str(line['purchase_order_id'])+'/line:'+str(line['line_id']),store_id=line['store_id'],units=line['allocated_qty'] if line['allocated_qty'] is not None else None))
        overlaps.extend(dict(kind='PENDING_REQUEST',reference='request:'+line['request_id'],intent_id=line['intent_id'],store_id=line['store_id'],units=line['units']) for line in by_pending[vid] if line['units']>0)
        existing={v['reference'] for v in overlaps}
        for line in by_history[square]:
            reference='po:'+str(line['purchase_order_id'])+'/line:'+str(line['line_id'])
            if reference not in existing:overlaps.append(dict(kind='PURCHASING_CHANGE_SINCE_GENERATION',reference=reference,store_id=None,units=line['ordered_qty'],status=line['status'],meaning='Historical commitment/receipt change, not additional incoming inventory'))
        overlap[vid]=overlaps
    relevant=[r for r in legacy if r['variation_id'] in refs.values() or not r['variation_id']]
    receiving_facts=[]
    if c.execute(text("SELECT to_regclass('purchasing_v4.receiving_supply')")).scalar_one():
        receiving_facts=[{key:(value.astimezone(timezone.utc).isoformat() if isinstance(value,datetime) else value) for key,value in x.items()} for x in c.execute(text('SELECT * FROM purchasing_v4.receiving_supply WHERE variation_id=ANY(:refs) ORDER BY id,store_id'),dict(refs=list(refs.values()))).mappings()]
    payload=primitive(dict(incoming=incoming,overlap=overlap,receiving_facts=receiving_facts,committed_facts=relevant,legacy_drafts=draft,pending_requests=pending,recent_purchasing_changes=recent,stores=store_rows,reference_mapping=refs))
    return payload,digest(payload)


def value_treatment(value,expected):
    if not isinstance(value,dict) or set(value)!={'source','unit_value','reason'}:raise controls.InvalidPolicy('Explicit valuation source, exact unit value and reason required')
    source=value['source'];reason=value['reason'];raw=value['unit_value']
    if source not in ('EXPECTED','OWNER_EXPLICIT') or not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:raise controls.InvalidPolicy('Valuation requires an explicit approved source and reason')
    if not isinstance(raw,str):raise controls.InvalidPolicy('Exact decimal valuation text required')
    try:number=Decimal(raw)
    except InvalidOperation:raise controls.InvalidPolicy('Invalid committed unit valuation')
    if not number.is_finite() or not 0<=number<=Decimal('9999999999.9999') or number!=number.quantize(Decimal('.0001')):raise controls.InvalidPolicy('Approved valuation must fit the existing exact four-decimal financial contract; review any required precision conversion explicitly')
    if source=='EXPECTED' and (expected is None or number!=Decimal(expected)):raise controls.InvalidPolicy('Expected cost is unavailable or differs from the reviewed valuation')
    return dict(source=source,unit_value=str(number),reason=reason.strip(),supplier_quote=None,actual_invoice=None)


def selection_digest(rows):
    return digest([dict(id=r['id'],variation_id=r['variation_id'],**{k:r[k] for k in sorted(worksheets.FIELDS)}) for r in sorted(rows,key=lambda r:r['id'])])


def prepare_review(engine,*,actor_id,review_id,session_id,expected_version,supplier_ids,source_namespace,currency=None,valuations=None,incremental_reason='',public_header=None,previous_review_id=None):
    if type(expected_version) is not int or expected_version<0 or not isinstance(supplier_ids,list):raise controls.InvalidPolicy('Explicit worksheet version and selected-supplier list required')
    if valuations is not None and not isinstance(valuations,dict):raise controls.InvalidPolicy('Valuations must be keyed by selected row identity')
    if public_header is not None and not isinstance(public_header,dict):raise controls.InvalidPolicy('Public document header must be an object')
    review_id=controls.uuid(review_id);supplier_ids=sorted({controls.uuid(s) for s in supplier_ids});valuations=valuations or {};public_header=public_header or {}
    if not 1<=len(supplier_ids)<=100:raise controls.InvalidPolicy('Select 1..100 suppliers explicitly')
    if currency not in (None,'USD'):raise controls.InvalidPolicy('The existing financial bridge supports explicitly selected USD only; no currency conversion is inferred')
    if not isinstance(incremental_reason,str) or len(incremental_reason)>2000:raise controls.InvalidPolicy('Invalid incremental authorization explanation')
    if set(public_header)-{'purchaser','ship_to'} or any(not isinstance(v,str) or len(v)>2000 for v in public_header.values()):raise controls.InvalidPolicy('Only explicit public purchaser and shipping text are accepted')
    if len(public_header.get('purchaser',''))>200:raise controls.InvalidPolicy('Public purchaser name must fit the reviewed 200-character header')
    request=dict(session_id=session_id,expected_version=expected_version,supplier_ids=supplier_ids,currency=currency,valuations=valuations,incremental_reason=incremental_reason,public_header=public_header,previous_review_id=previous_review_id);request_digest=digest(request)
    with readonly_snapshot(engine) as c:
        owner(c,actor_id)
        old=c.execute(text('SELECT * FROM purchasing_v4.submission_review WHERE id=:id'),{'id':review_id}).mappings().one_or_none()
        if old:
            if old['owner_id']!=actor_id or old['request_digest']!=request_digest:raise controls.Conflict('Review identity reused for another decision')
            return dict(id=review_id,**old['payload'])
        state=worksheets.snapshot(c,session_id,actor_id)
        if state['session']['version']!=expected_version or state['session']['state']!='EDITABLE':raise controls.Conflict('Save and review the exact current worksheet revision')
        selected=[r for r in state['rows'] if not r['removed'] and r['supplier_id'] in supplier_ids and not r.get('sealed')]
        if set(r['supplier_id'] for r in selected)!=set(supplier_ids):raise controls.InvalidPolicy('A selected supplier has no editable rows; prior submitted decisions remain sealed')
        profiles,configuration_digest,current_heads=current_configuration(c,selected)
        generation_as_of=c.execute(text('SELECT as_of FROM purchasing_v4.worksheet_generation WHERE id=:id'),{'id':state['session']['generation_id']}).scalar_one_or_none() if state['session']['generation_id'] else state['session']['created_at']
        supply,supply_digest=supply_snapshot(c,selected,state['session']['stores'],source_namespace,since=generation_as_of)
        if previous_review_id:
            previous=c.execute(text('SELECT owner_id,payload FROM purchasing_v4.submission_review WHERE id=:id'),{'id':controls.uuid(previous_review_id)}).mappings().one_or_none()
            if not previous or previous['owner_id']!=actor_id:raise controls.InvalidPolicy('Unknown prior owner review')
            if previous['payload']['session_id']!=session_id or previous['payload']['worksheet_version']!=expected_version or previous['payload']['supply_digest']!=supply_digest or previous['payload']['configuration_digest']!=configuration_digest or previous['payload']['supplier_ids']!=supplier_ids:raise controls.Conflict('Supply, supplier or worksheet changed. Refresh and inspect the changed review before authorizing')
        lines=[];blocking=[];total=Decimal(0);expected_total=Decimal(0);expected_unknown=0;groups=[]
        known_rows={r['id'] for r in selected}
        if set(valuations)-known_rows:raise controls.InvalidPolicy('Valuation includes an unselected product')
        for row in selected:
            packs=row['packs'];size=row['pack_size'];offer=row['offers'].get(row['supplier_id']) or {};expected=offer.get('expected_unit_cost');kind='REQUEST' if profiles[row['supplier_id']]['confirmation_enabled'] else 'PO'
            count=sum(p or 0 for p in packs);packs_known=all(p is not None for p in packs);complete=packs_known and (size is not None or count==0)
            if not complete:blocking.append(row['name']+': resolve all four pack quantities and positive-order pack size explicitly')
            units=[p*size if p is not None and size is not None else 0 if p==0 else None for p in packs];line_units=sum(v or 0 for v in units)
            treatment=value_treatment(valuations[row['id']],expected) if row['id'] in valuations else None
            if kind=='PO' and count>0:
                if treatment is None:blocking.append(row['name']+': explicit owner-approved committed valuation required')
                else:total+=Decimal(treatment['unit_value'])*line_units
                if line_units>2147483647:blocking.append(row['name']+': individual units exceed the legacy accounting quantity capacity')
            if count and size:
                if expected is None:expected_unknown+=1
                else:expected_total+=Decimal(expected)*line_units
            overlap=supply['overlap'][row['variation_id']]
            if count>0 and overlap and not incremental_reason.strip():blocking.append(row['name']+': review overlapping supply and give an explicit additional-purchase reason')
            current_offer=controls.live(current_heads.get(('OFFER',row['variation_id']+':'+row['supplier_id'])))
            warnings=list(row['warnings'])
            if current_offer['pack_size']!=size:warnings.append('Current supplier pack differs from the pinned worksheet pack; review this order explicitly')
            if current_offer['expected_unit_cost']!=expected and (current_offer['expected_unit_cost'] is None or expected is None or Decimal(current_offer['expected_unit_cost'])!=Decimal(expected)):warnings.append('Current expected cost differs from the pinned offer; no cost was automatically replaced')
            lines.append(dict(row_id=row['id'],variation_id=row['variation_id'],square_variation_id=supply['reference_mapping'][row['variation_id']],supplier_id=row['supplier_id'],kind=kind,name=row['name'],variation=row['variation'],sku=row['sku'],vendor_sku=offer.get('vendor_sku'),pack_size=size,packs=packs,total_packs=count if packs_known else None,units=units,total_units=line_units if complete else None,quantities_complete=complete,known_subtotal_packs=count,known_subtotal_units=line_units,expected_unit_cost=expected,expected_cost_provenance=offer.get('cost_provenance'),valuation=treatment,policy=row['policy'],warnings=warnings,incoming=supply['incoming'][row['variation_id']],overlap=overlap,advice_units=row['advice_units'],offer_version=offer.get('offer_version')))
        for sid in supplier_ids:
            own=[r for r in lines if r['supplier_id']==sid];positive=[r for r in own if (r['total_packs'] or 0)>0]
            if not positive:blocking.append(profiles[sid]['name']+': no positive purchasing quantities')
            groups.append(dict(supplier_id=sid,profile=profiles[sid],kind='REQUEST' if profiles[sid]['confirmation_enabled'] else 'PO',positive_lines=len(positive),minimum_warning=('Supplier minimum '+str(profiles[sid]['order_minimum'])+' is descriptive: internal valuations are not vendor quotations. Confirm terms explicitly; quantities were not clamped.') if profiles[sid]['order_minimum'] is not None else None))
        if currency is None:blocking.append('Select the committed valuation currency explicitly')
        if any(not public_header.get(k,'').strip() for k in ('purchaser','ship_to')):blocking.append('Review the public purchaser and shipping header explicitly')
        if total>Decimal('999999999999.99'):blocking.append('Committed financial total exceeds the existing accounting capacity')
        committed_complete=all(l['quantities_complete'] and (l['kind']=='REQUEST' or l['total_packs']==0 or l['valuation'] is not None) for l in lines)
        expected_complete=all(l['quantities_complete'] and (l['total_packs']==0 or l['expected_unit_cost'] is not None) for l in lines)
        payload=primitive(dict(session_id=session_id,worksheet_version=expected_version,generation_id=state['session']['generation_id'],generation_as_of=generation_as_of,supplier_ids=supplier_ids,stores=state['session']['stores'],lines=lines,groups=groups,selection_digest=selection_digest(selected),supply=supply,supply_digest=supply_digest,configuration_digest=configuration_digest,currency=currency,incremental_reason=incremental_reason.strip(),public_header=public_header,committed_total=str(total.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)) if committed_complete else None,committed_known_subtotal=str(total),committed_total_complete=committed_complete,expected_total=str(expected_total) if expected_complete else None,expected_known_subtotal=str(expected_total),expected_total_complete=expected_complete,unknown_expected_cost_lines=expected_unknown,unresolved_quantity_lines=sum(not l['quantities_complete'] for l in lines),blocking=blocking,reviewed_at=datetime.now(timezone.utc),commitments_created=0))
    with engine.begin() as c:
        owner(c,actor_id)
        c.execute(text('INSERT INTO purchasing_v4.submission_review(id,owner_id,session_id,worksheet_version,request_digest,payload) VALUES (:id,:owner,:sid,:version,:digest,CAST(:payload AS jsonb))'),dict(id=review_id,owner=actor_id,sid=session_id,version=expected_version,digest=request_digest,payload=worksheets.js(payload)))
    return dict(id=review_id,**payload)


def reserve_id(c,table):
    # Existing canonical sequence, never a separate V4 number pool or a rewind.
    sequence=c.execute(text('SELECT pg_get_serial_sequence(:table,\'id\')'),dict(table='public.'+table)).scalar_one()
    for _ in range(100000):
        value=c.execute(text('SELECT nextval(CAST(:s AS regclass))'),{'s':sequence}).scalar_one()
        if not c.execute(text('SELECT EXISTS(SELECT 1 FROM public.'+table+' WHERE id=:id)'),{'id':value}).scalar_one():return value
    raise controls.InvalidPolicy('Canonical sequence needs explicit reconciliation')


def bridge_supplier(c,supplier_id,profile,namespace):
    found=c.execute(text('SELECT legacy_vendor_id FROM purchasing_v4.supplier_financial_bridge WHERE supplier_id=:id'),{'id':supplier_id}).scalar_one_or_none()
    if found:return found
    references=c.execute(text('SELECT legacy_vendor_id,square_vendor_id FROM purchasing_v4.supplier_reference WHERE source_namespace=:ns AND supplier_id=:id'),dict(ns=namespace,id=supplier_id)).mappings().all()
    if len(references)>1:raise controls.InvalidPolicy('Ambiguous financial supplier identity')
    if references:vid=references[0]['legacy_vendor_id'];origin='SQUARE' if references[0]['square_vendor_id'] else 'LOCAL'
    else:
        vid=reserve_id(c,'vendors');origin='LOCAL'
        c.execute(text('INSERT INTO public.vendors(id,square_vendor_id,name,active) VALUES (:id,NULL,:name,true)'),dict(id=vid,name=profile['name']))
    c.execute(text('INSERT INTO purchasing_v4.supplier_financial_bridge(supplier_id,legacy_vendor_id,origin,provenance) VALUES (:sid,:vid,:origin,CAST(:p AS jsonb))'),dict(sid=supplier_id,vid=vid,origin=origin,p=worksheets.js(dict(source_namespace=namespace,authority='Explicit V4 supplier identity; no Square identity fabricated'))))
    return vid


def _authorize(c,*,actor_id,command_id,review_id,acknowledged,source_namespace):
    owner(c,actor_id);cid=controls.uuid(command_id);rid=controls.uuid(review_id)
    if acknowledged is not True:raise controls.InvalidPolicy('Explicit final owner authorization and warning acknowledgment required')
    request_digest=digest(dict(owner=actor_id,review_id=rid,acknowledged=True))
    if c.execute(text('SHOW transaction_isolation')).scalar_one()!='read committed':raise controls.InvalidPolicy('Authorization requires a fresh read-committed transaction')
    c.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':SUPPLY_LOCK})
    old=c.execute(text('SELECT * FROM purchasing_v4.authorization_command WHERE id=:id'),{'id':cid}).mappings().one_or_none()
    if old:
        if old['owner_id']!=actor_id or old['request_digest']!=request_digest:raise controls.Conflict('Authorization command identity reused for another decision')
        return dict(old['result'],replayed=True)
    review=c.execute(text('SELECT * FROM purchasing_v4.submission_review WHERE id=:id AND owner_id=:owner'),dict(id=rid,owner=actor_id)).mappings().one_or_none()
    if not review:raise controls.InvalidPolicy('Unknown owner review')
    previous=c.execute(text('SELECT * FROM purchasing_v4.purchase_authorization WHERE review_id=:id'),{'id':rid}).mappings().one_or_none()
    if previous:
        c.execute(text('INSERT INTO purchasing_v4.authorization_command(id,owner_id,request_digest,authorization_id,result) VALUES (:id,:owner,:hash,:aid,CAST(:result AS jsonb))'),dict(id=cid,owner=actor_id,hash=request_digest,aid=previous['id'],result=worksheets.js(previous['result'])))
        return dict(previous['result'],replayed=True)
    p=review['payload']
    if p['blocking']:raise controls.InvalidPolicy('Review is not ready: '+'; '.join(p['blocking'][:10]))
    session=c.execute(text('SELECT version,state FROM purchasing_v4.worksheet WHERE id=:id FOR UPDATE'),{'id':p['session_id']}).mappings().one()
    if session['version']!=p['worksheet_version'] or (session['state']!='EDITABLE' and not p.get('quotation')):raise controls.Conflict('Worksheet revision changed after review; nothing authorized')
    c.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
    c.execute(text('SELECT id FROM public.stores WHERE active FOR SHARE')).all()
    c.execute(text('SELECT square_variation_id FROM public.ordering_catalog_identity WHERE square_variation_id=ANY(:refs) FOR KEY SHARE'),dict(refs=list(p['supply']['reference_mapping'].values()))).all()
    if p.get('quotation'):
        from . import quotations
        supply=quotations.validate_authorization(c,p,actor_id,source_namespace)
    else:
        current=worksheets.snapshot(c,p['session_id'],actor_id);selected=[r for r in current['rows'] if not r['removed'] and r['supplier_id'] in p['supplier_ids'] and not r.get('sealed')]
        profiles,configuration_digest,current_heads=current_configuration(c,selected)
        supply,supply_digest=supply_snapshot(c,selected,p['stores'],source_namespace,since=p['generation_as_of'])
        if configuration_digest!=p['configuration_digest'] or supply_digest!=p['supply_digest']:raise controls.Conflict('Incoming supply, pending requests or supplier configuration changed after review; refresh and review again')
        if selection_digest(selected)!=p['selection_digest']:raise controls.Conflict('Exact reviewed product quantities, pack or supplier decisions changed')
        if {r['id'] for r in selected}!={r['row_id'] for r in p['lines']}:raise controls.Conflict('Reviewed product selection changed')
    aid=str(uuid5(UUID(rid),'owner-authorization'));children=[]
    c.execute(text('SET LOCAL ROLE erupted_v4_po_writer'))
    try:
        for group in p['groups']:
            sid=group['supplier_id'];child=str(uuid5(UUID(aid),sid));lines=[r for r in p['lines'] if r['supplier_id']==sid and r['total_packs']>0];po_id=reserve_id(c,'purchase_orders') if group['kind']=='PO' else None
            snapshot=dict(review_id=rid,worksheet_id=p['session_id'],worksheet_version=p['worksheet_version'],generation_id=p['generation_id'],configuration_digest=p['configuration_digest'],header=p['public_header'],currency=p['currency'],supplier=group['profile'],stores=p['stores'],lines=lines,authorized_by=actor_id,authorized_at=datetime.now(timezone.utc).isoformat(),supply_digest=p['supply_digest'],incremental_reason=p['incremental_reason'],valuation_semantics='OWNER_APPROVED_COMMITTED_UNIT_VALUATION; expected, quote, invoice, payment and inventory remain separate')
            if p.get('quotation'):snapshot['quotation']=p['quotation']
            c.execute(text('INSERT INTO purchasing_v4.authorized_vendor(id,authorization_id,supplier_id,kind,purchase_order_id,snapshot) VALUES (:id,:aid,:sid,:kind,:po,CAST(:snapshot AS jsonb))'),dict(id=child,aid=aid,sid=sid,kind=group['kind'],po=po_id,snapshot=worksheets.js(snapshot)))
            if po_id is not None:
                vendor=bridge_supplier(c,sid,group['profile'],source_namespace)
                c.execute(text("SELECT set_config('purchasing_v4.vendor_authorization',:id,true)"),{'id':child})
                c.execute(text('''INSERT INTO public.purchase_orders(id,vendor_id,status,reorder_weeks,stock_up_weeks,history_lookback_days,creation_idempotency_key,created_by_principal_id,submitted_by_principal_id,ordered_at,submitted_at)
                  VALUES (:id,:vendor,'IN_TRANSIT',NULL,NULL,NULL,:key,:owner,:owner,now(),now())'''),dict(id=po_id,vendor=vendor,key='v4:'+child,owner=actor_id))
                for line in lines:
                    public_line=reserve_id(c,'purchase_order_lines');square=supply['reference_mapping'][line['variation_id']]
                    c.execute(text('''INSERT INTO public.purchase_order_lines(id,purchase_order_id,variation_id,sku,item_name,variation_name,unit_cost,ordered_qty,suggested_qty,received_qty_total,in_transit_qty,par_source,removed)
                    VALUES (:id,:po,:vid,:sku,:name,:variation,:cost,:qty,0,0,:qty,'MANUAL',false)'''),dict(id=public_line,po=po_id,vid=square,sku=line['sku'],name=line['name'],variation=line['variation'],cost=line['valuation']['unit_value'],qty=line['total_units']))
                    c.execute(text('INSERT INTO purchasing_v4.financial_line_bridge(purchase_order_line_id,vendor_authorization_id,row_id,approved_unit_value) VALUES (:id,:child,:row,:value)'),dict(id=public_line,child=child,row=line['row_id'],value=line['valuation']['unit_value']))
                    for store,units in zip(p['stores'],line['units']):
                        allocation=reserve_id(c,'purchase_order_store_allocations')
                        c.execute(text('''INSERT INTO public.purchase_order_store_allocations(id,purchase_order_line_id,store_id,expected_qty,allocated_qty,store_received_qty,variance_qty) VALUES (:id,:line,:store,:qty,:qty,0,0)'''),dict(id=allocation,line=public_line,store=store['id'],qty=units))
            if po_id is not None and c.execute(text("SELECT to_regclass('purchasing_v4.receiving_order')")).scalar_one():
                from .receiving import capture_order
                capture_order(c,po_id)
            if p.get('quotation'):
                quotations.resolve(c,p,child,po_id,actor_id,cid)
            else:
                for line in lines:
                    for store,units in zip(p['stores'],line['units']):
                        intent=str(uuid5(UUID(p['session_id']),line['row_id']+'/store:'+str(store['id'])))
                        c.execute(text('''INSERT INTO purchasing_v4.purchasing_intent(id,session_id,row_id,store_id,vendor_authorization_id,units,reviewed_supply_digest,incremental_reason,prior_supply)
                        VALUES (:id,:session,:row,:store,:child,:units,:digest,:reason,CAST(:prior AS jsonb))'''),dict(id=intent,session=p['session_id'],row=line['row_id'],store=store['id'],child=child,units=units,digest=p['supply_digest'],reason=p['incremental_reason'] or None,prior=worksheets.js(line['overlap'])))
                        target='intent_consumption' if group['kind']=='PO' else 'request_reservation'
                        c.execute(text('INSERT INTO purchasing_v4.'+target+'(intent_id,vendor_authorization_id) VALUES (:id,:child)'),dict(id=intent,child=child))
                    c.execute(text('INSERT INTO purchasing_v4.worksheet_seal(session_id,row_id,vendor_authorization_id) VALUES (:s,:r,:child)'),dict(s=p['session_id'],r=line['row_id'],child=child))
            from .vendor_documents import vendor_projection
            public=vendor_projection(child,po_id,group['kind'],snapshot)
            public=dict(document=public,delivery=dict(auto_enabled=group['profile']['automatic_po_email'] if group['kind']=='PO' else group['profile']['automatic_spreadsheet_email'],recipient=group['profile'].get('email'),format='XLSX' if group['profile']['preferred_po_format']=='SPREADSHEET' else 'PDF'))
            doc_command=str(uuid5(UUID(child),'documents:v1'))
            c.execute(text('INSERT INTO purchasing_v4.integration_command(id,vendor_authorization_id,kind,payload,payload_digest) VALUES (:id,:child,\'GENERATE_DOCUMENTS\',CAST(:payload AS jsonb),:digest)'),dict(id=doc_command,child=child,payload=worksheets.js(public),digest=digest(public)))
            c.execute(text("INSERT INTO purchasing_v4.integration_state(command_id,state) VALUES (:id,'PENDING')"),{'id':doc_command})
            children.append(dict(id=child,supplier_id=sid,supplier_name=group['profile']['name'],kind=group['kind'],purchase_order_id=po_id,document_command_id=doc_command,lines=len(lines),status='PO authorized; vendor files pending; delivery is separate' if po_id else 'Request recorded; vendor handoff pending; no commitment'))
        result=dict(authorization_id=aid,review_id=rid,session_id=p['session_id'],worksheet_version=p['worksheet_version']+1,children=children,commitments_created=sum(g['kind']=='PO' for g in children),requests_created=sum(g['kind']=='REQUEST' for g in children),replayed=False)
        if p.get('quotation'):result['source_request_id']=p['quotation']['request_id']
        c.execute(text('INSERT INTO purchasing_v4.purchase_authorization(id,review_id,owner_id,request_digest,result) VALUES (:id,:review,:owner,:hash,CAST(:result AS jsonb))'),dict(id=aid,review=rid,owner=actor_id,hash=request_digest,result=worksheets.js(result)))
        c.execute(text('INSERT INTO purchasing_v4.authorization_command(id,owner_id,request_digest,authorization_id,result) VALUES (:id,:owner,:hash,:aid,CAST(:result AS jsonb))'),dict(id=cid,owner=actor_id,hash=request_digest,aid=aid,result=worksheets.js(result)))
        c.execute(text('UPDATE purchasing_v4.worksheet SET version=version+1,updated_at=now() WHERE id=:id'),{'id':p['session_id']})
        return result
    finally:
        if sys.exc_info()[0] is None:c.execute(text('RESET ROLE'))


def authorize(c,**kwargs):
    with c.begin_nested():return _authorize(c,**kwargs)
