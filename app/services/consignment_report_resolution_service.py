"""Report snapshots and owner cost decisions; no inventory or financial writers."""
import copy
import hashlib
import json
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation
from uuid import uuid4
from sqlalchemy import select
from app.models import (ConsignmentReportingDraft,ConsignmentReportingPreference,
    VendorSkuConfig,Principal,AuditLog)

class Conflict(ValueError):
    pass


def _actor(db,actor_id):
    actor=db.get(Principal,actor_id)
    role=getattr(actor.role,'value',actor.role) if actor else None
    if not actor or not actor.active or actor.store_id is not None or role not in ('ADMIN','MANAGER'):
        raise PermissionError('Authorized organization administrator required.')


def _stamp(mapping):
    from app.services.consignment_reporting_service import configuration_stamp
    return configuration_stamp(mapping)


def _audit(db,actor_id,action,draft,before,after,ip=None):
    db.add(AuditLog(actor_principal_id=actor_id,action=action,ip=ip,
        meta={'domain':'CONSIGNMENT_REPORTING','report_id':draft.id,'vendor_id':draft.vendor_id,
              'before':before,'after':after}))


def create(db, *, report, actor_id, ip=None):
    _actor(db,actor_id)
    from app.services.consignment_reporting_service import configured_products
    _,_,current_products=configured_products(db,report['vendor'].id)
    current_versions={str(p['mapping_id']):[p['configuration_stamp'],p['assignment']] for p in current_products}
    if report.get('configuration_versions')!=current_versions:
        raise Conflict('Vendor costs or assignments changed while this report was loading. Generate it again.')
    snapshot=json.loads(json.dumps({k:v for k,v in report.items() if k!='vendor'},default=str))
    snapshot['vendor_name']=report['vendor'].name
    # Keep source problems distinct from resolvable unit costs.
    snapshot['source_issues']=[i for i in report['issues'] if not i['reason'].startswith('Configured vendor unit cost')]
    mappings=list(db.scalars(select(VendorSkuConfig).where(VendorSkuConfig.vendor_id==report['vendor'].id)))
    prefs={p.mapping_id:p for p in db.scalars(select(ConsignmentReportingPreference).where(
        ConsignmentReportingPreference.mapping_id.in_([m.id for m in mappings])))}
    snapshot['configuration']={str(m.id):{'stamp':_stamp(m),'preference_revision':prefs[m.id].revision if m.id in prefs else 0,
        'sku':m.sku,'unit_cost':str(m.unit_cost) if m.unit_cost is not None else None} for m in mappings}
    decisions={}
    for m in mappings:
        p=prefs.get(m.id)
        if p and p.excluded:decisions[str(m.id)]={'action':'exclude','remember':False,'remembered':True}
        elif p and p.zero_cost_stamp==_stamp(m) and m.unit_cost==0:
            decisions[str(m.id)]={'action':'cost','cost':'0','confirm_zero':True,'remember':False,'remembered':True}
    draft=ConsignmentReportingDraft(id=str(uuid4()),vendor_id=report['vendor'].id,actor_id=actor_id,
        snapshot=snapshot,decisions=decisions,revision=1,status='DRAFT')
    db.add(draft);db.flush()
    _audit(db,actor_id,'CONSIGNMENT_REPORT_CREATED',draft,{}, {'source_orders':report['orders_checked'],'period':[str(report['start_date']),str(report['end_date'])]},ip)
    return draft


def normalize(draft,decisions):
    if not isinstance(decisions,dict):raise ValueError('Expected SKU decisions.')
    result={}
    for key,raw in decisions.items():
        if key not in draft.snapshot['configuration'] or not isinstance(raw,dict):raise ValueError('SKU is not part of this vendor report configuration.')
        action=raw.get('action')
        if action not in ('cost','exclude','unresolved','include'):raise ValueError('Choose Enter Cost, Ignore SKU, or Leave Unresolved.')
        if not isinstance(raw.get('remember',False),bool):raise ValueError('Invalid remember selection.')
        value={'action':action,'remember':raw.get('remember',False)}
        if raw.get('remembered') is True:value['remembered']=True
        if action=='cost':
            try:cost=Decimal(str(raw.get('cost','')))
            except (InvalidOperation,ValueError):raise ValueError('Enter a nonnegative unit cost with at most four decimal places.')
            if not cost.is_finite() or cost<0 or cost>Decimal('9999999999.9999') or cost!=cost.quantize(Decimal('.0001')):
                raise ValueError('Enter a nonnegative unit cost with at most four decimal places.')
            if cost==0 and raw.get('confirm_zero') is not True:raise ValueError('Explicitly confirm an intentional $0.00 cost.')
            value.update(cost=str(cost),confirm_zero=cost==0)
        result[key]=value
    return result


def calculate(draft,decisions=None):
    choices=normalize(draft, draft.decisions if decisions is None else decisions)
    rows=[];excluded=[];problems=[];grouped={};source=draft.snapshot
    for raw in source['rows']:
        row=copy.deepcopy(raw)
        if row.get('variation') and row['variation'].casefold() not in row['name'].casefold():row['name']+=' — '+row['variation']
        key=str(row['mapping_id']);choice=choices.get(key,{});action=choice.get('action','include')
        configured=Decimal(row['unit_cost']) if row['unit_cost'] is not None else None
        cost=Decimal(choice['cost']) if action=='cost' else configured
        row['configured_cost']=row['unit_cost'];row['cost_source']='Manual report override' if action=='cost' else 'Configured vendor cost'
        row['intentional_zero']=action=='cost' and cost==0 and choice.get('confirm_zero') is True
        row['unit_cost']=str(cost) if cost is not None else None
        net=Decimal(row['net_units'])
        if action=='exclude':
            row['cogs']=None;row['exclusion']='Remembered exclusion' if choice.get('remembered') else 'Owner excluded';excluded.append(row)
        else:
            resolved=action!='unresolved' and cost is not None and (cost>0 or row['intentional_zero'])
            row['cogs']=str(net*cost) if resolved else None
            if not resolved:problems.append({'sku':row['sku'],'mapping_id':row['mapping_id'],'reason':'Vendor cost requires a decision'})
            rows.append(row)
        if configured is None or configured<=0 or key in choices:
            g=grouped.setdefault(key,{'mapping_id':key,'sku':row['sku'],'name':row['name'],'configured_cost':row['configured_cost'],
                'stores':[],'decision':choice or {'action':'unresolved','remember':False}})
            g['stores'].append({'name':row['store_name'],'sold':row['sold'],'returned':row['returned'],'net_units':row['net_units']})
    # Remembered exclusions are reversible even in periods with no sales.
    for key,choice in choices.items():
        if key not in grouped and choice.get('remembered'):
            config=source['configuration'][key]
            grouped[key]={'mapping_id':key,'sku':config['sku'],'name':config['sku'],'configured_cost':config['unit_cost'],
                'stores':[],'decision':choice}
    def total(items,field):return str(sum((Decimal(r[field]) for r in items if r[field] is not None),Decimal(0)))
    stores=[]
    for store in source['stores']:
        lines=[r for r in rows if r['store_id']==store['store_id']]
        stores.append({**store,'sold':total(lines,'sold'),'returned':total(lines,'returned'),'net_units':total(lines,'net_units'),
            'known_cogs':total(lines,'cogs'),'cogs':total(lines,'cogs') if all(r['cogs'] is not None for r in lines) else None})
    complete=not problems and not source['source_issues']
    return {**source,'rows':rows,'excluded':excluded,'resolutions':list(grouped.values()),'decisions':choices,'stores':stores,
        'sold':total(rows,'sold'),'returned':total(rows,'returned'),'net_units':total(rows,'net_units'),
        'excluded_net_units':total(excluded,'net_units'),'known_cogs':total(rows,'cogs'),
        'cogs':total(rows,'cogs') if complete else None,'complete':complete,'issues':source['source_issues']+problems,
        'id':draft.id,'revision':draft.revision,'status':draft.status}


def save(db, *, draft_id, actor_id, revision, decisions, request_key, finalize=False, ip=None):
    _actor(db,actor_id)
    if not isinstance(request_key,str) or not 8<=len(request_key)<=64:raise ValueError('A valid save request identity is required.')
    draft=db.scalar(select(ConsignmentReportingDraft).where(ConsignmentReportingDraft.id==draft_id).with_for_update().execution_options(populate_existing=True))
    if not draft:raise LookupError('Report not found.')
    choices=normalize(draft,decisions)
    digest=hashlib.sha256(json.dumps([choices,bool(finalize)],sort_keys=True).encode()).hexdigest()
    if draft.last_request==request_key:
        if draft.last_digest!=digest:raise Conflict('Save identity was reused with different decisions.')
        return draft
    if draft.status!='DRAFT':raise Conflict('Finalized reports are immutable. Generate a new report to make new decisions.')
    if type(revision) is not int or draft.revision!=revision:raise Conflict('This report changed in another window. Reload before saving; your selections have not been applied.')
    view=calculate(draft,choices)
    if finalize and not view['complete']:raise ValueError('Resolve or exclude affected SKUs before finalizing. You can save an unresolved draft.')
    before=copy.deepcopy(draft.decisions);configuration=copy.deepcopy(draft.snapshot['configuration'])
    for key in sorted(choices,key=int):
        choice=choices[key]
        if not choice['remember']:continue
        mapping=db.scalar(select(VendorSkuConfig).where(VendorSkuConfig.id==int(key)).with_for_update().execution_options(populate_existing=True))
        pref=db.scalar(select(ConsignmentReportingPreference).where(ConsignmentReportingPreference.mapping_id==int(key)).with_for_update().execution_options(populate_existing=True))
        prior=configuration[key]
        if not mapping or mapping.vendor_id!=draft.vendor_id or not mapping.active or not mapping.is_default_vendor or _stamp(mapping)!=prior['stamp'] or (pref.revision if pref else 0)!=prior['preference_revision']:
            raise Conflict('The SKU cost, assignment or remembered decision changed. Generate a fresh report before changing future settings.')
        previous={'unit_cost':str(mapping.unit_cost) if mapping.unit_cost is not None else None,'excluded':pref.excluded if pref else False,'zero_confirmed':bool(pref and pref.zero_cost_stamp==_stamp(mapping))}
        if pref is None:
            pref=ConsignmentReportingPreference(mapping_id=mapping.id,excluded=False,revision=1,updated_by=actor_id);db.add(pref)
        else:pref.revision+=1
        pref.excluded=choice['action']=='exclude';pref.updated_by=actor_id;pref.updated_at=datetime.now(timezone.utc)
        if choice['action']=='cost':
            mapping.unit_cost=Decimal(choice['cost']);mapping.updated_by_principal_id=actor_id;db.flush();db.refresh(mapping)
            pref.zero_cost_stamp=_stamp(mapping) if mapping.unit_cost==0 else None
        elif choice['action']=='unresolved':pref.zero_cost_stamp=None
        configuration[key]={'stamp':_stamp(mapping),'preference_revision':pref.revision,'sku':mapping.sku,'unit_cost':str(mapping.unit_cost) if mapping.unit_cost is not None else None}
        _audit(db,actor_id,'CONSIGNMENT_FUTURE_SETTING',draft,{'mapping_id':key,**previous},
            {'mapping_id':key,'unit_cost':configuration[key]['unit_cost'],'excluded':pref.excluded,'zero_confirmed':bool(pref.zero_cost_stamp)},ip)
        choice['remember']=False;choice['remembered']=True
    draft.snapshot={**draft.snapshot,'configuration':configuration};draft.decisions=choices;draft.revision+=1
    draft.last_request=request_key;draft.last_digest=digest
    if finalize:draft.status='FINALIZED';draft.finalized_at=datetime.now(timezone.utc)
    _audit(db,actor_id,'CONSIGNMENT_REPORT_FINALIZED' if finalize else 'CONSIGNMENT_REPORT_DECISIONS',draft,before,
        {'decisions':choices,'net_units':view['net_units'],'cogs':view['cogs'],'excluded_net_units':view['excluded_net_units']},ip)
    db.flush();return draft


def export_csv(view):
    import csv,io
    out=io.StringIO();w=csv.writer(out)
    def safe(value):
        value=str(value if value is not None else '')
        return "'"+value if value.lstrip(' \t\r\n').startswith(('=','+','-','@')) else value
    w.writerow(['Report ID',view['id'],'Status',view['status'],'Financial total','COMPLETE' if view['complete'] else 'INCOMPLETE'])
    w.writerow(['Vendor',safe(view['vendor_name']),'Start inclusive',view['start_date'],'End inclusive',view['end_date']])
    w.writerow(['Disposition','SKU','Product','Store','Sold','Returned','Net units','Configured cost','Applied cost','COGS','Cost source','Intentional zero'])
    for disposition,rows in [('INCLUDED',view['rows']),('EXCLUDED',view['excluded'])]:
        for r in rows:w.writerow([disposition,safe(r['sku']),safe(r['name']),safe(r['store_name']),r['sold'],r['returned'],r['net_units'],r['configured_cost'] if r['configured_cost'] is not None else 'UNKNOWN',r['unit_cost'] if r['unit_cost'] is not None else 'UNKNOWN',r['cogs'] if r['cogs'] is not None else ('EXCLUDED' if disposition=='EXCLUDED' else 'UNRESOLVED'),r['cost_source'],r['intentional_zero']])
    w.writerow(['TOTAL INCLUDED','','','',view['sold'],view['returned'],view['net_units'],'','',view['cogs'] if view['complete'] else 'INCOMPLETE'])
    w.writerow(['TOTAL EXCLUDED','','','','','',view['excluded_net_units']])
    if view.get('amount_only_refunds'):w.writerow(['WARNING','',str(view['amount_only_refunds'])+' checked orders have amount-only refunds; no returned units were invented.'])
    for issue in view['issues']:w.writerow(['WARNING',safe(issue.get('sku')),safe(issue['reason'])])
    return out.getvalue()
