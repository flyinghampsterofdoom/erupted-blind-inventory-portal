"""Explicit owner supplier setup; pinned worksheet prices, no external effects."""
from uuid import UUID,uuid5
from copy import deepcopy
from sqlalchemy import text
from . import controls,worksheets
from .identity import digest
from .isolation import require_isolated


def row_for_owner(c,actor_id,session_id,row_id):
    controls.owner(c,actor_id)
    row=c.execute(text('SELECT r.id::text,r.variation_id::text,r.original,r.working FROM purchasing_v4.worksheet_row r JOIN purchasing_v4.worksheet w ON w.id=r.session_id WHERE w.id=:sid AND w.owner_id=:owner AND r.id=:rid'),dict(sid=controls.uuid(session_id),owner=actor_id,rid=controls.uuid(row_id))).mappings().one_or_none()
    if not row or row['working']['removed']:raise controls.InvalidPolicy('Select an active row in your worksheet')
    return row


def options(c,*,actor_id,session_id,row_id):
    row=row_for_owner(c,actor_id,session_id,row_id);vid=row['variation_id']
    profiles=c.execute(text("SELECT record_key,version,payload FROM purchasing_v4.control_head WHERE kind='SUPPLIER' AND payload->>'active'='true' ORDER BY lower(payload->>'name'),record_key LIMIT 501")).mappings().all()
    if len(profiles)>500:raise controls.InvalidPolicy('Supplier catalog exceeds the inline limit; use Supplier management')
    offers=controls.heads(c,{('OFFER',vid+':'+p['record_key']) for p in profiles})
    assignment=controls.heads(c,{('ASSIGNMENT',vid)}).get(('ASSIGNMENT',vid))
    return dict(variation_id=vid,assignment_version=assignment['version'] if assignment else 0,suppliers=[dict(id=p['record_key'],name=p['payload']['name'],profile_version=p['version'],use_vendor_sku=p['payload']['use_vendor_sku'],offer_version=offers[('OFFER',vid+':'+p['record_key'])]['version'] if ('OFFER',vid+':'+p['record_key']) in offers else 0,offer=controls.live(offers.get(('OFFER',vid+':'+p['record_key'])))) for p in profiles])


def resolve(c,*,actor_id,session_id,command_id,expected_version,row_id,supplier_id,expected_offer_version,expected_supplier_version,expected_assignment_version,scope,value):
    controls.owner(c,actor_id);require_isolated(c)
    sid=controls.uuid(session_id);cid=controls.uuid(command_id);rid=controls.uuid(row_id);supplier=controls.uuid(supplier_id)
    if scope not in ('order','default'):raise controls.InvalidPolicy('Choose this order or an explicit default supplier change')
    for n in (expected_version,expected_offer_version,expected_supplier_version,expected_assignment_version):
        if type(n) is not int or n<0:raise controls.InvalidPolicy('Explicit current versions required')
    request=dict(session_id=sid,expected_version=expected_version,row_id=rid,supplier_id=supplier,expected_offer_version=expected_offer_version,expected_supplier_version=expected_supplier_version,expected_assignment_version=expected_assignment_version,scope=scope,value=value)
    with c.begin_nested():
        session=c.execute(text('SELECT version,state FROM purchasing_v4.worksheet WHERE id=:sid AND owner_id=:owner FOR UPDATE'),dict(sid=sid,owner=actor_id)).mappings().one_or_none()
        if not session:raise controls.InvalidPolicy('Unknown owner worksheet')
        replay=worksheets.owner_receipt(c,actor_id,cid,'SUPPLIER_RESOLUTION',request)
        if replay:return dict(replay,replayed=True)
        if session['state']!='EDITABLE' or session['version']!=expected_version:raise controls.Conflict('Worksheet changed; review the current row before saving supplier setup')
        row=row_for_owner(c,actor_id,sid,rid)
        if c.scalar(text("SELECT to_regclass('purchasing_v4.worksheet_seal')")) and c.scalar(text('SELECT EXISTS(SELECT 1 FROM purchasing_v4.worksheet_seal WHERE session_id=:sid AND row_id=:rid)'),dict(sid=sid,rid=rid)):raise controls.Conflict('Submitted rows are sealed; no supplier setup changed')
        c.execute(text("SET LOCAL lock_timeout='5s'"));c.execute(text("SET LOCAL statement_timeout='15s'"));c.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
        vid=row['variation_id'];key=vid+':'+supplier
        heads=controls.heads(c,{('SUPPLIER',supplier),('OFFER',key),('ASSIGNMENT',vid)})
        profile_head=heads.get(('SUPPLIER',supplier));profile=controls.live(profile_head)
        offer_head=heads.get(('OFFER',key));current=controls.live(offer_head)
        assignment=heads.get(('ASSIGNMENT',vid))
        if not profile or not profile['active']:raise controls.InvalidPolicy('Choose an existing active supplier')
        if profile_head['version']!=expected_supplier_version or (offer_head['version'] if offer_head else 0)!=expected_offer_version:raise controls.Conflict('Supplier or offer changed; reopen supplier setup to review the current values')
        if scope=='default' and (assignment['version'] if assignment else 0)!=expected_assignment_version:raise controls.Conflict('Default supplier changed; review it before replacing it')
        mutations=[]
        if value is None:
            if not current or not current['approved']:raise controls.InvalidPolicy('An approved offer or explicit supplier setup fields are required')
            offer=current;offer_version=offer_head['version']
        else:
            if not isinstance(value,dict) or set(value)!={'pack_size','expected_unit_cost','vendor_sku','approve'} or type(value['approve']) is not bool:raise controls.InvalidPolicy('Explicit pack, cost, SKU and approval fields required')
            if current and current['approved']:raise controls.Conflict('An approved offer already exists; use its current values instead of repricing it here')
            if value['approve'] and (value['pack_size'] is None or value['expected_unit_cost'] is None):raise controls.InvalidPolicy('Confirm a known pack size and unit cost before approving calculated purchasing')
            if value['approve'] and profile['use_vendor_sku'] and not value['vendor_sku']:raise controls.InvalidPolicy('This supplier requires its product SKU before approval')
            offer=dict(variation_id=vid,supplier_id=supplier,approved=value['approve'],pack_size=value['pack_size'],expected_unit_cost=value['expected_unit_cost'],vendor_sku=value['vendor_sku'],cost_provenance='Owner entered expected unit cost in worksheet supplier setup' if value['expected_unit_cost'] is not None else 'Owner saved worksheet supplier link; expected unit cost remains unknown')
            controls.validate('OFFER',key,offer)
            offer_version=expected_offer_version+1
            mutations.append(dict(kind='OFFER',key=key,expected_version=expected_offer_version,value=offer))
        complete=worksheets.complete_supplier_offer(offer) and (not profile['use_vendor_sku'] or bool(offer['vendor_sku']))
        if scope=='default':
            if not complete:raise controls.InvalidPolicy('Complete and approve supplier setup before choosing it as the future default')
            mutations.append(dict(kind='ASSIGNMENT',key=vid,expected_version=expected_assignment_version,value=dict(variation_id=vid,supplier_id=supplier)))
        if mutations:controls.apply(c,actor_id=actor_id,command_id=str(uuid5(UUID(cid),'supplier-controls')),reason='Owner saved supplier setup from worksheet '+sid+' row '+rid,changes=mutations)
        pin=dict(offer,_worksheet_link=True,name=profile['name'],workflow='Spreadsheet confirmation' if profile['confirmation_enabled'] else 'Direct PO',profile_version=profile_head['version'],offer_version=offer_version,use_vendor_sku=profile['use_vendor_sku'])
        before=worksheets.editable(row['working']);working=dict(deepcopy(before),supplier_id=supplier,pack_size=offer['pack_size'])
        for i in range(4):
            if working['touched'][i]:continue
            if 'GLOBAL_NO_REORDER' in row['original'].get('warnings',[]):working['packs'][i]=0
            elif not complete:working['packs'][i]=None
            else:working['packs'][i]=worksheets.final_pack_conversion(worksheets.Fraction(row['original']['advice_units'][i]),offer['pack_size']) if row['original']['advice_units'][i] is not None else None
        # Keep metadata outside the five editable fields. Ordinary quantity edits
        # retain it, while every explicit new pin is recorded in immutable receipts.
        metadata=dict(row['working'].get('_supplier_offers',{}))
        if supplier not in metadata and len(metadata)>=100:raise controls.InvalidPolicy('This row has reached its saved supplier offer limit')
        metadata[supplier]=pin
        effective=dict(row['original'],offers={**row['original']['offers'],**metadata})
        worksheets.validate_row(dict(effective,**working),prior=row['working'],pinned=effective)
        c.execute(text('UPDATE purchasing_v4.worksheet_row SET working=CAST(:working AS jsonb) WHERE session_id=:sid AND id=:rid'),dict(working=worksheets.js(dict(working,_supplier_offers=metadata)),sid=sid,rid=rid))
        version=expected_version+1;c.execute(text('UPDATE purchasing_v4.worksheet SET version=:version,updated_at=now() WHERE id=:sid'),dict(version=version,sid=sid))
        result=dict(command_id=cid,version=version,row_id=rid,supplier_id=supplier,setup_complete=complete,default_changed=scope=='default',pinned_offer=pin,inverse=[dict(id=rid,value=before)],replayed=False)
        c.execute(text('INSERT INTO purchasing_v4.worksheet_command(session_id,id,actor_id,request_digest,before_version,after_version,changes,result) VALUES (:sid,:cid,:actor,:digest,:before,:after,CAST(:changes AS jsonb),CAST(:result AS jsonb))'),dict(sid=sid,cid=cid,actor=actor_id,digest=digest(request),before=expected_version,after=version,changes=worksheets.js(dict(rows=[dict(id=rid,value=working)],supplier_offer=pin)),result=worksheets.js(result)))
        worksheets.record_owner_receipt(c,actor_id,cid,'SUPPLIER_RESOLUTION',request,result)
        return result
