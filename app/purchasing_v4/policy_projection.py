"""Read-only control projection and explicit owner history conversion commands."""
from dataclasses import replace
from fractions import Fraction
import json
from uuid import uuid4
from sqlalchemy import text
from . import controls
from .controls import InvalidPolicy,Conflict,live,heads,owner
from .calculation import ManualRule,ProductCoverageOverride,recommend_batch
from .calculation_config import Coverage
from .calculation_documents import exact
from .calculation_serialization import primitive,evidence_digest


def project_rows(connection,rows,*,with_versions=False):
    keys={('PRODUCT',r.variation_id) for r in rows}|{('ASSIGNMENT',r.variation_id) for r in rows}|{('MANUAL',r.variation_id+':'+r.store_id) for r in rows}
    state=heads(connection,keys)
    ids=sorted({r.variation_id for r in rows})
    frozen=connection.execute(text("SELECT * FROM purchasing_v4.legacy_manual_evidence WHERE scope_variations ?| CAST(:ids AS text[])"),{'ids':ids}).mappings().all() if ids else []
    dependencies=set()
    for (kind,key),record in state.items():
        assignment=live(record)
        if kind=='ASSIGNMENT' and assignment and assignment['supplier_id']:
            dependencies|={('SUPPLIER',assignment['supplier_id']),('OFFER',key+':'+assignment['supplier_id'])}
    state.update(heads(connection,dependencies));result=[]
    for row in rows:
        product_record=state.get(('PRODUCT',row.variation_id));product=live(product_record)
        assignment=live(state.get(('ASSIGNMENT',row.variation_id)))
        issues=set(row.identity_issues);pack=None
        if assignment and assignment['supplier_id']:
            sid=assignment['supplier_id'];supplier=live(state.get(('SUPPLIER',sid)));offer=live(state.get(('OFFER',row.variation_id+':'+sid)))
            if supplier and supplier['active'] and offer and offer['approved']:
                pack=offer['pack_size']
                if offer['expected_unit_cost'] is None:issues.add('OWNER_OFFER_EXPECTED_COST_UNKNOWN')
            else:issues.add('UNRESOLVED_OWNER_SUPPLIER_AUTHORITY')
        else:issues.add('UNRESOLVED_OWNER_DEFAULT_SUPPLIER')
        manual_record=state.get(('MANUAL',row.variation_id+':'+row.store_id));manual=live(manual_record)
        legacy_issues=row.policy_issues
        nonmanual_issues=tuple(code for code in row.policy_issues if not code.startswith(('UNRESOLVED_LEGACY_MANUAL_','INVALID_LEGACY_MANUAL_')))
        manual_value=row.manual
        baselines=[f for f in frozen if row.variation_id in f['scope_variations'] and (f['store_id'] is None or str(f['store_id'])==row.store_id)]
        if not manual and baselines:
            values={(f['observation']['manual_par_level'],f['observation']['manual_stock_up_level']) for f in baselines}
            valid=len(values)==1 and all(f['mapped_variations']==[row.variation_id] and f['store_id'] is not None for f in baselines)
            par,level=next(iter(values))
            valid=valid and type(par) is int and type(level) is int and 0<=par<level
            if valid:
                manual_value=ManualRule('frozen-legacy:'+','.join(str(f['legacy_id']) for f in baselines),par,level);legacy_issues=nonmanual_issues
            else:
                manual_value=None;legacy_issues=nonmanual_issues+('UNRESOLVED_FROZEN_LEGACY_MANUAL_POLICY',)
        if manual:
            if manual['state']=='ACTIVE':
                manual_value=ManualRule('v4-manual:'+manual_record['record_key']+':'+str(manual_record['version']),manual['par_units'],manual['level_units']);legacy_issues=nonmanual_issues
            elif manual['state']=='INACTIVE':manual_value=None;legacy_issues=('EXPLICIT_INACTIVE_MANUAL_POLICY',)
            else:manual_value=None;legacy_issues=nonmanual_issues
        coverage=None
        if product and product['coverage']:
            coverage=ProductCoverageOverride(Coverage(exact(product['coverage']['reorder_weeks']),exact(product['coverage']['target_weeks'])),'owner-product-version:'+str(product_record['version']))
        result.append(replace(row,pack_size=pack,identity_issues=tuple(sorted(issues)),manual=manual_value,policy_issues=legacy_issues,
            no_reorder=product['no_reorder'] if product else row.no_reorder,product_coverage=coverage))
    versions=[dict(kind=k,record_key=key,version=state[(k,key)]["version"] if (k,key) in state else 0) for k,key in sorted(keys|dependencies)]
    return (tuple(result),versions) if with_versions else tuple(result)


def calculate_history_check(connection,*,actor_id,manual_keys,as_of,max_age,source_namespace):
    """Owner-triggered read-only computation; no policy is modified."""
    owner(connection,actor_id)
    if not isinstance(manual_keys,list) or not 1<=len(manual_keys)<=100 or len(set(manual_keys))!=len(manual_keys):raise InvalidPolicy('Explicitly select1..100 distinct manual policies')
    config=controls.configuration(connection)
    if config is None:raise InvalidPolicy('Enter explicit automatic settings before a comparison; none are installed')
    records=heads(connection,{('MANUAL',key) for key in manual_keys})
    for key in manual_keys:
        record=records.get(('MANUAL',key));value=live(record)
        if not value or value['state'] not in ('ACTIVE','INACTIVE'):raise InvalidPolicy('History check requires explicitly selected manual policies')
    from .calculation_source import load_snapshot
    snapshot=load_snapshot(connection,config,as_of=as_of,max_age=max_age,source_namespace=source_namespace,
        selected_variations=tuple(sorted({key.split(':')[0] for key in manual_keys})))
    selected=tuple(r for r in snapshot.rows if r.variation_id+':'+r.store_id in manual_keys)
    projected,versions=project_rows(connection,selected,with_versions=True)
    if len(projected)!=len(manual_keys):raise InvalidPolicy('A selected manual policy lacks verified catalog/store evidence')
    manual_batch=recommend_batch(projected,config,as_of=as_of,input_digest=snapshot.digest)
    automatic_rows=tuple(replace(row,manual=None,policy_issues=tuple(code for code in row.policy_issues if code!='EXPLICIT_INACTIVE_MANUAL_POLICY')) for row in projected)
    automatic_batch=recommend_batch(automatic_rows,config,as_of=as_of,input_digest=snapshot.digest)
    comparison=[dict(variation_id=row.variation_id,store_id=row.store_id,manual=m,automatic=a,
        eligible_for_conversion=(a.state in ('RECOMMENDED','REVIEW_REQUIRED') and a.forecast.weekly_units is not None and not row.no_reorder),
        selected_for_conversion=False,conversion_performed=False) for row,m,a in zip(projected,manual_batch.recommendations,automatic_batch.recommendations)]
    result=[]
    for row in comparison:
        key=row['variation_id']+':'+row['store_id'];row['key']=key;row['expected_version']=records[('MANUAL',key)]['version']
        row['manual_policy']=records[('MANUAL',key)]['payload'];row['selected_for_conversion']=False
        result.append(primitive(row))
    return dict(id=str(uuid4()),actor_id=actor_id,configuration_digest=config.content_digest,input_digest=snapshot.digest,as_of=as_of.isoformat(),max_age_seconds=int(max_age.total_seconds()),rows=result,dependency_versions=versions)


def save_history_check(connection,check):
    owner(connection,check['actor_id'])
    connection.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
    config=controls.configuration(connection)
    if config is None or config.content_digest!=check['configuration_digest']:raise Conflict('Automatic configuration changed during history check')
    verify_dependencies(connection,check['dependency_versions'])
    current=heads(connection,{('MANUAL',r['key']) for r in check['rows']})
    if any(current[('MANUAL',r['key'])]['version']!=r['expected_version'] for r in check['rows']):raise Conflict('Manual policy changed during history check')
    connection.execute(text('''INSERT INTO purchasing_v4.history_check(id,actor_id,configuration_digest,input_digest,rows)
      VALUES (:id,:actor,:config,:input,CAST(:rows AS jsonb))'''),dict(id=check['id'],actor=check['actor_id'],config=check['configuration_digest'],input=check['input_digest'],rows=json.dumps(dict(as_of=check['as_of'],max_age_seconds=check['max_age_seconds'],rows=check['rows'],dependency_versions=check['dependency_versions']))))
    return check


def use_automatic(connection,*,actor_id,command_id,reason,check_id,selected_keys):
    owner(connection,actor_id)
    if not isinstance(selected_keys,list) or not selected_keys or len(selected_keys)>100 or len(set(selected_keys))!=len(selected_keys):raise InvalidPolicy('Explicit nonempty conversion selection required')
    with connection.begin_nested():
        connection.execute(text('SELECT pg_advisory_xact_lock(704210273)'))
        check=connection.execute(text('SELECT * FROM purchasing_v4.history_check WHERE id=:id'),{'id':controls.uuid(check_id)}).mappings().one_or_none()
        if not check or check['actor_id']!=actor_id:raise InvalidPolicy('Unknown owner history check')
        if check['rows'].get('format')=='manual-comparison-1':raise InvalidPolicy('Use a fresh impact preview and explicit comparison confirmation')
        eligible={r['key']:r for r in check['rows']['rows'] if r['eligible_for_conversion']}
        if not set(selected_keys)<=set(eligible):raise InvalidPolicy('Selection contains unchecked/unqualified history candidates')
        changes=[]
        for key in sorted(selected_keys):
            r=eligible[key];value=dict(r['manual_policy'],state='AUTOMATIC')
            changes.append(dict(kind='MANUAL',key=key,expected_version=r['expected_version'],value=value))
        previous=connection.execute(text('SELECT 1 FROM purchasing_v4.control_command WHERE id=:id'),{'id':controls.uuid(command_id)}).scalar_one_or_none()
        config=controls.configuration(connection)
        if not previous:verify_dependencies(connection,check['rows']['dependency_versions'])
        if not previous and (config is None or config.content_digest!=check['configuration_digest']):raise Conflict('Configuration changed; initiate a new history check')
        return controls._apply(connection,actor_id=actor_id,command_id=command_id,reason=reason,changes=changes,
                               action='USE_AUTOMATIC_ENGINE:'+check_id,conversion=True)


def verify_dependencies(connection,versions):
    current=heads(connection,{(r['kind'],r['record_key']) for r in versions})
    for row in versions:
        record=current.get((row['kind'],row['record_key']))
        if (record['version'] if record else 0)!=row['version']:
            raise Conflict('Policy or supplier dependency changed; initiate a new history check')
