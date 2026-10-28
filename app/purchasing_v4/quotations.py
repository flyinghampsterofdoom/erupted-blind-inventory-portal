"""Immutable quotation evidence and explicit owner request lifecycle.

Final authorization is deliberately delegated to submission.authorize, the
accepted Phase5 transaction, canonical PO bridge, consumption and outbox writer.
"""
from datetime import datetime,timezone
from uuid import uuid4,UUID,uuid5
from copy import deepcopy
from decimal import Decimal
import hashlib,hmac
from sqlalchemy import text
from . import controls,worksheets,submission
from . import quotation_workbooks as protocol
from .quotation_finance import calculate,CONTRACT,DESCRIPTION
from .identity import digest
from .calculation_serialization import primitive
from .isolation import readonly_snapshot


def request(c,request_id,actor_id):
 submission.owner(c,actor_id);rid=controls.uuid(request_id)
 row=c.execute(text("SELECT v.*,r.payload AS original_review FROM purchasing_v4.authorized_vendor v JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id JOIN purchasing_v4.submission_review r ON r.id=a.review_id WHERE v.id=:id AND a.owner_id=:owner AND v.kind='REQUEST'"),dict(id=rid,owner=actor_id)).mappings().one_or_none()
 if not row:raise controls.InvalidPolicy('Choose a retained confirmation request owned by the authenticated purchasing owner')
 return row


def head(c,request_id,*,lock=False):
 value=c.execute(text('SELECT request_id::text,version,revision_id::text,selected_response_id::text,state FROM purchasing_v4.quotation_head WHERE request_id=:id'+(' FOR UPDATE' if lock else '')),{'id':request_id}).mappings().one_or_none()
 if not value:raise controls.InvalidPolicy('Prepare a versioned quotation workbook for this request first')
 return dict(value)


def revision(c,revision_id):
 row=c.execute(text('SELECT * FROM purchasing_v4.quotation_revision WHERE id=:id'),{'id':controls.uuid(revision_id)}).mappings().one_or_none()
 if not row:raise controls.InvalidPolicy('Unknown immutable quotation revision')
 return row


def receipt(c,actor_id,command_id,kind,payload):
 value=c.execute(text('SELECT * FROM purchasing_v4.quotation_event WHERE command_id=:id'),{'id':controls.uuid(command_id)}).mappings().one_or_none()
 if value:
  if value['owner_id']!=actor_id or value['kind']!=kind or value['request_digest']!=digest(payload):raise controls.Conflict('Quotation command identity reused for another owner decision')
  return value['result']


def event(c,actor_id,command_id,request_id,kind,payload,reason,result):
 c.execute(text('INSERT INTO purchasing_v4.quotation_event(command_id,request_id,owner_id,kind,request_digest,reason,result) VALUES (:id,:request,:owner,:kind,:hash,:reason,CAST(:result AS jsonb))'),dict(id=command_id,request=request_id,owner=actor_id,kind=kind,hash=digest(payload),reason=reason,result=worksheets.js(result)))


def new_revision(c,request_id,actor_id,number,snapshot):
 rid=str(uuid4());public=protocol.projection(request_id,rid,number,snapshot);key=bytes(c.execute(text('SELECT secret FROM purchasing_v4.quotation_key')).scalar_one());signature=protocol.signature(public,key);content=protocol.template_bytes(public,signature)
 c.execute(text('INSERT INTO purchasing_v4.quotation_revision(id,request_id,number,owner_id,snapshot,public_projection,signature,template,sha256) VALUES (:id,:request,:n,:owner,CAST(:snapshot AS jsonb),CAST(:public AS jsonb),:sig,:content,:hash)'),dict(id=rid,request=request_id,n=number,owner=actor_id,snapshot=worksheets.js(snapshot),public=worksheets.js(public),sig=signature,content=content,hash=hashlib.sha256(content).hexdigest()))
 return rid


def prepare(c,*,actor_id,command_id,request_id):
 original=request(c,request_id,actor_id);payload=dict(request_id=request_id);replay=receipt(c,actor_id,command_id,'PREPARE',payload)
 if replay:return dict(replay,replayed=True)
 c.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':submission.SUPPLY_LOCK})
 replay=receipt(c,actor_id,command_id,'PREPARE',payload)
 if replay:return dict(replay,replayed=True)
 existing=c.execute(text('SELECT 1 FROM purchasing_v4.quotation_head WHERE request_id=:id'),{'id':request_id}).scalar_one_or_none()
 if existing:return head(c,request_id)
 if c.execute(text('SELECT EXISTS(SELECT 1 FROM purchasing_v4.purchasing_intent i JOIN purchasing_v4.intent_consumption x ON x.intent_id=i.id WHERE i.vendor_authorization_id=:id)'),{'id':request_id}).scalar_one():raise controls.Conflict('Request intents were already consumed')
 snapshot=deepcopy(original['snapshot']);snapshot.update(supplier_id=str(original['supplier_id']),generation_as_of=original['original_review']['generation_as_of'])
 rid=new_revision(c,request_id,actor_id,1,snapshot);result=dict(request_id=request_id,version=0,revision_id=rid,selected_response_id=None,state='OPEN')
 event(c,actor_id,command_id,request_id,'PREPARE',payload,'Owner prepares a manual vendor quotation; no commitment',result)
 c.execute(text("INSERT INTO purchasing_v4.quotation_head(request_id,revision_id,state) VALUES (:id,:revision,'OPEN')"),dict(id=request_id,revision=rid));return result


def transition(c,*,actor_id,command_id,request_id,expected_version,kind,reason,response_id=None,changes=None):
 request(c,request_id,actor_id)
 if type(expected_version) is not int or expected_version<0:raise controls.InvalidPolicy('Explicit nonnegative request version required')
 if kind not in ('SELECT_RESPONSE','REISSUE','CANCEL') or not isinstance(reason,str) or not reason.strip() or len(reason)>2000:raise controls.InvalidPolicy('Explicit lifecycle action and owner reason required')
 payload=dict(request_id=request_id,expected_version=expected_version,kind=kind,reason=reason,response_id=response_id,changes=changes);replay=receipt(c,actor_id,command_id,kind,payload)
 if replay:return dict(replay,replayed=True)
 c.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':submission.SUPPLY_LOCK})
 replay=receipt(c,actor_id,command_id,kind,payload)
 if replay:return dict(replay,replayed=True)
 current=head(c,request_id,lock=True)
 if current['version']!=expected_version or current['state']!='OPEN':raise controls.Conflict('Request or selected response changed; inspect the current request before continuing')
 result=dict(current,version=current['version']+1)
 if kind=='SELECT_RESPONSE':
  row=c.execute(text('SELECT request_id::text,revision_id::text FROM purchasing_v4.quotation_response WHERE id=:id'),{'id':controls.uuid(response_id)}).mappings().one_or_none()
  if not row or row['request_id']!=request_id or row['revision_id']!=current['revision_id']:raise controls.Conflict('An outdated or different-vendor response cannot become the active response')
  result['selected_response_id']=response_id
 elif kind=='CANCEL':result.update(state='CANCELED',selected_response_id=None)
 else:
  old=revision(c,current['revision_id']);snapshot=deepcopy(old['snapshot']);choices=changes
  if not isinstance(choices,dict) or set(choices)!={l['row_id'] for l in snapshot['lines']}:raise controls.InvalidPolicy('A reissue requires an explicit reviewed pack configuration and four store quantities for every original line')
  for l in snapshot['lines']:
   v=choices[l['row_id']]
   if not isinstance(v,dict) or set(v)!={'pack_size','packs'} or type(v['pack_size']) is not int or not 1<=v['pack_size']<=1000000 or not isinstance(v['packs'],list) or len(v['packs'])!=4 or any(type(q) is not int or not 0<=q<=1000000 for q in v['packs']):raise controls.InvalidPolicy('Reissued packs must be explicit whole quantities')
   l.update(pack_size=v['pack_size'],packs=v['packs'],units=[q*v['pack_size'] for q in v['packs']],total_packs=sum(v['packs']),total_units=sum(v['packs'])*v['pack_size'])
   if l['total_units']>2147483647:raise controls.InvalidPolicy('Reissued units exceed accounting capacity')
  if not any(l['total_packs'] for l in snapshot['lines']):raise controls.InvalidPolicy('Cancel an all-zero request explicitly')
  result.update(revision_id=new_revision(c,request_id,actor_id,old['number']+1,snapshot),selected_response_id=None)
 event(c,actor_id,command_id,request_id,kind,payload,reason.strip(),result)
 c.execute(text('UPDATE purchasing_v4.quotation_head SET version=:version,revision_id=:revision_id,selected_response_id=:selected_response_id,state=:state,updated_at=now() WHERE request_id=:request_id'),result);return result


def intake(engine,*,actor_id,request_id,filename,content):
 if not isinstance(filename,str) or not filename.lower().endswith('.xlsx') or len(filename)>240:raise controls.InvalidPolicy('Choose a named .xlsx quotation workbook')
 if not isinstance(content,bytes) or len(content)>protocol.MAX_FILE:raise controls.InvalidPolicy('Upload at most 10 MiB; larger files are rejected before retention')
 file_hash=hashlib.sha256(content).hexdigest()
 with readonly_snapshot(engine) as c:
  request(c,request_id,actor_id);current=head(c,request_id)
  prior=c.execute(text('SELECT id::text,response_id::text,validation FROM purchasing_v4.quotation_upload WHERE request_id=:id AND sha256=:hash'),dict(id=request_id,hash=file_hash)).mappings().one_or_none()
  if prior:return dict(prior,duplicate_upload=True)
  # Resolve immutable revision from the uploaded manifest only after bounded ZIP/XML validation.
  try:
   protocol.bounded_package(content)
   book=protocol.load_workbook(protocol.BytesIO(content),data_only=False,read_only=True,keep_links=False)
   try:
    if 'Protocol' not in book or book['Protocol'].max_row>8 or book['Protocol'].max_column>2:raise protocol.InvalidWorkbook('Missing or oversized versioned quotation manifest')
    fields={row[0]:row[1] for row in book['Protocol'].iter_rows(max_row=8,max_col=2,values_only=True)}
    immutable=revision(c,fields.get('revision_id'));public=immutable['public_projection']
    if str(immutable['request_id'])!=request_id:raise protocol.InvalidWorkbook('Workbook belongs to another purchasing request/vendor')
   finally:book.close()
   key=bytes(c.execute(text('SELECT secret FROM purchasing_v4.quotation_key')).scalar_one())
   if not hmac.compare_digest(protocol.signature(public,key),immutable['signature']):raise protocol.InvalidWorkbook('Stored request signature failed integrity verification')
   normalized=protocol.parse(content,public,immutable['signature']);validation=dict(valid=True,errors=[],issues=normalized['issues'],revision_id=public['revision_id'],stale=public['revision_id']!=current['revision_id'] or current['state']!='OPEN')
  except (protocol.InvalidWorkbook,controls.InvalidPolicy,ValueError,TypeError,KeyError) as e:
   normalized=None;validation=dict(valid=False,errors=e.errors if isinstance(e,protocol.InvalidWorkbook) else [str(e)],issues=[],stale=False)
 with engine.begin() as c:
  request(c,request_id,actor_id);persisted_head=head(c,request_id,lock=True)
  if normalized is not None:validation['stale']=normalized['revision_id']!=persisted_head['revision_id'] or persisted_head['state']!='OPEN'
  prior=c.execute(text('SELECT id::text,response_id::text,validation FROM purchasing_v4.quotation_upload WHERE request_id=:id AND sha256=:hash'),dict(id=request_id,hash=file_hash)).mappings().one_or_none()
  if prior:return dict(prior,duplicate_upload=True)
  response_id=None
  if normalized is not None:
   semantic=digest(normalized);response_id=str(uuid5(UUID(normalized['revision_id']),semantic))
   c.execute(text('INSERT INTO purchasing_v4.quotation_response(id,request_id,revision_id,semantic_digest,payload) VALUES (:id,:request,:revision,:hash,CAST(:payload AS jsonb)) ON CONFLICT(revision_id,semantic_digest) DO NOTHING'),dict(id=response_id,request=request_id,revision=normalized['revision_id'],hash=semantic,payload=worksheets.js(normalized)))
  upload=str(uuid4());c.execute(text('INSERT INTO purchasing_v4.quotation_upload(id,request_id,owner_id,sha256,content,filename,validation,response_id) VALUES (:id,:request,:owner,:hash,:content,:filename,CAST(:validation AS jsonb),:response)'),dict(id=upload,request=request_id,owner=actor_id,hash=file_hash,content=content,filename=filename,validation=worksheets.js(validation),response=response_id))
  return dict(id=upload,response_id=response_id,validation=validation,duplicate_upload=False)


def details(c,*,actor_id,request_id):
 original=request(c,request_id,actor_id);current=head(c,request_id);rev=revision(c,current['revision_id'])
 responses=[dict(r) for r in c.execute(text("SELECT id::text,revision_id::text,CASE WHEN id=CAST(:selected AS uuid) THEN payload ELSE jsonb_build_object('issues','[]'::jsonb) END AS payload,jsonb_array_length(payload->'issues') AS issue_count,created_at FROM purchasing_v4.quotation_response WHERE request_id=:id ORDER BY (id=CAST(:selected AS uuid)) DESC NULLS LAST,created_at DESC,id LIMIT 100"),{'id':request_id,'selected':current['selected_response_id']}).mappings()]
 uploads=[dict(r) for r in c.execute(text('SELECT id::text,response_id::text,filename,sha256,validation,created_at FROM purchasing_v4.quotation_upload WHERE request_id=:id ORDER BY created_at DESC,id LIMIT 100'),{'id':request_id}).mappings()]
 revisions=[dict(r) for r in c.execute(text('SELECT id::text,number,sha256,created_at FROM purchasing_v4.quotation_revision WHERE request_id=:id ORDER BY number'),{'id':request_id}).mappings()]
 history=[dict(r) for r in c.execute(text('SELECT command_id::text,owner_id,kind,reason,result,created_at FROM purchasing_v4.quotation_event WHERE request_id=:id ORDER BY created_at'),{'id':request_id}).mappings()]
 review_summaries=[dict(r) for r in c.execute(text("SELECT id::text,created_at,payload->'blocking' AS blocking,payload->>'committed_total' AS committed_total,payload->'quotation'->>'response_id' AS response_id,payload->'quotation'->>'revision_number' AS request_revision FROM purchasing_v4.submission_review WHERE owner_id=:owner AND payload->'quotation'->>'request_id'=:request ORDER BY created_at DESC LIMIT 100"),dict(owner=actor_id,request=request_id)).mappings()]
 committed=c.execute(text('SELECT a.result FROM purchasing_v4.quotation_commitment b JOIN purchasing_v4.authorized_vendor v ON v.id=b.vendor_authorization_id JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id WHERE b.request_id=:id'),{'id':request_id}).scalar_one_or_none()
 return primitive(dict(committed_result=committed,owner_reviews=review_summaries,head=current,revision_number=rev['number'],snapshot=rev['snapshot'],responses=responses,uploads=uploads,revisions=revisions,history=history,financial_contract=CONTRACT,financial_description=DESCRIPTION))


def prepare_final_review(engine,*,actor_id,review_id,request_id,expected_version,response_id,choices,components,contract,unit_adapter_approved,release_remainder,incremental_reason,source_namespace):
 rid=controls.uuid(review_id)
 if type(expected_version) is not int or expected_version<0:raise controls.InvalidPolicy('Explicit nonnegative request version required')
 if not isinstance(incremental_reason,str) or len(incremental_reason)>2000:raise controls.InvalidPolicy('Invalid additional-purchase reason')
 args=dict(request_id=request_id,expected_version=expected_version,response_id=response_id,choices=choices,components=components,contract=contract,unit_adapter_approved=unit_adapter_approved,release_remainder=release_remainder,incremental_reason=incremental_reason);request_digest=digest(args)
 with readonly_snapshot(engine) as c:
  request(c,request_id,actor_id);current=head(c,request_id)
  existing=c.execute(text('SELECT owner_id,request_digest,payload FROM purchasing_v4.submission_review WHERE id=:id'),{'id':rid}).mappings().one_or_none()
  if existing:
   if existing['owner_id']!=actor_id or existing['request_digest']!=request_digest:raise controls.Conflict('Review identity reused for different quotation decisions')
   return dict(id=rid,**existing['payload'])
  if current['version']!=expected_version or current['state']!='OPEN' or current['selected_response_id']!=response_id:raise controls.Conflict('Select and review the exact current response/request version explicitly')
  rev=revision(c,current['revision_id']);snapshot=rev['snapshot'];response=c.execute(text('SELECT payload FROM purchasing_v4.quotation_response WHERE id=:id AND revision_id=:revision'),dict(id=controls.uuid(response_id),revision=current['revision_id'])).scalar_one_or_none()
  if response is None:raise controls.Conflict('Selected response does not match current request revision')
  lines,financial,blocking=calculate(snapshot,response,choices,components,contract=contract,unit_adapter_approved=unit_adapter_approved,release_remainder=release_remainder)
  rows=[dict(l,id=l['row_id'],supplier_id=snapshot['supplier_id']) for l in snapshot['lines']];profiles,config,heads=submission.current_configuration(c,rows);supply,supply_digest=submission.supply_snapshot(c,rows,snapshot['stores'],source_namespace,since=snapshot['generation_as_of'])
  other_overlap=[v for overlaps in supply['overlap'].values() for v in overlaps if v.get('reference')!='request:'+request_id]
  if other_overlap and not incremental_reason.strip():blocking.append('Review other current incoming/pending supply and explain the separately authorized additional purchase')
  warnings=[]
  for line in snapshot['lines']:
   offer=controls.live(heads.get(('OFFER',line['variation_id']+':'+snapshot['supplier_id'])))
   if offer['pack_size']!=line['pack_size']:warnings.append(line['name']+': current supplier pack differs; the exact request pack remains pinned')
   if offer['expected_unit_cost']!=line['expected_unit_cost'] and (offer['expected_unit_cost'] is None or line['expected_unit_cost'] is None or Decimal(offer['expected_unit_cost'])!=Decimal(line['expected_unit_cost'])):warnings.append(line['name']+': current expected cost differs; original request expected cost remains preserved')
  ws=worksheets.snapshot(c,snapshot['worksheet_id'],actor_id)
  quote=dict(request_id=request_id,revision_id=current['revision_id'],revision_number=rev['number'],head_version=expected_version,response_id=response_id,response_digest=digest(response),financial=financial,owner_decisions=choices,component_decisions=components,request_snapshot_digest=digest(snapshot),release_remainder=release_remainder)
  payload=primitive(dict(session_id=snapshot['worksheet_id'],worksheet_version=ws['session']['version'],generation_id=snapshot['generation_id'],generation_as_of=snapshot['generation_as_of'],supplier_ids=[snapshot['supplier_id']],stores=snapshot['stores'],lines=lines,groups=[dict(supplier_id=snapshot['supplier_id'],profile=profiles[snapshot['supplier_id']],kind='PO')],supply=supply,supply_digest=supply_digest,configuration_digest=config,currency='USD',public_header=snapshot['header'],incremental_reason=incremental_reason.strip(),blocking=blocking,configuration_warnings=warnings,committed_total=financial['total'],quotation=quote,reviewed_at=datetime.now(timezone.utc)))
 with engine.begin() as c:
  submission.owner(c,actor_id);c.execute(text('INSERT INTO purchasing_v4.submission_review(id,owner_id,session_id,worksheet_version,request_digest,payload) VALUES (:id,:owner,:session,:version,:hash,CAST(:payload AS jsonb)) ON CONFLICT(id) DO NOTHING'),dict(id=rid,owner=actor_id,session=payload['session_id'],version=payload['worksheet_version'],hash=request_digest,payload=worksheets.js(payload)))
  stored=c.execute(text('SELECT owner_id,request_digest,payload FROM purchasing_v4.submission_review WHERE id=:id'),{'id':rid}).mappings().one()
  if stored['owner_id']!=actor_id or stored['request_digest']!=request_digest:raise controls.Conflict('Review identity reused for different owner decisions')
  payload=stored['payload']
 return dict(id=rid,**payload)


def validate_authorization(c,p,actor_id,namespace):
 q=p['quotation'];request(c,q['request_id'],actor_id);current=head(c,q['request_id'],lock=True)
 if current['state']!='OPEN' or current['version']!=q['head_version'] or current['revision_id']!=q['revision_id'] or current['selected_response_id']!=q['response_id']:raise controls.Conflict('Quotation was selected, reissued, canceled or authorized after review')
 rev=revision(c,q['revision_id']);response=c.execute(text('SELECT payload FROM purchasing_v4.quotation_response WHERE id=:id AND revision_id=:revision'),dict(id=q['response_id'],revision=q['revision_id'])).scalar_one()
 if digest(response)!=q['response_digest'] or digest(rev['snapshot'])!=q['request_snapshot_digest']:raise controls.Conflict('Exact immutable quotation evidence changed')
 snapshot=rev['snapshot'];lines,financial,blocking=calculate(snapshot,response,q['owner_decisions'],q['component_decisions'],contract=q['financial']['contract'],unit_adapter_approved=q['financial']['unit_adapter_approved'],release_remainder=q['release_remainder'])
 if blocking or financial!=q['financial'] or lines!=p['lines']:raise controls.Conflict('Approved quantities, prices or financial treatment no longer match the exact review')
 rows=[dict(l,id=l['row_id'],supplier_id=snapshot['supplier_id']) for l in snapshot['lines']];profiles,config,_=submission.current_configuration(c,rows);supply,supply_digest=submission.supply_snapshot(c,rows,p['stores'],namespace,since=p['generation_as_of'])
 if config!=p['configuration_digest'] or supply_digest!=p['supply_digest']:raise controls.Conflict('Incoming supply, pending requests or supplier configuration changed after quotation review')
 if c.execute(text('SELECT EXISTS(SELECT 1 FROM purchasing_v4.purchasing_intent i JOIN purchasing_v4.intent_consumption x ON x.intent_id=i.id WHERE i.vendor_authorization_id=:id)'),{'id':q['request_id']}).scalar_one():raise controls.Conflict('Original request intent has already been resolved into a commitment')
 return supply


def resolve(c,p,child,po_id,actor_id,command_id):
 q=p['quotation'];financial=q['financial'];c.execute(text('INSERT INTO purchasing_v4.quotation_commitment(request_id,vendor_authorization_id) VALUES (:request,:child)'),dict(request=q['request_id'],child=child));lines={l['row_id']:l for l in p['lines']};stores={s['id']:i for i,s in enumerate(p['stores'])}
 requested={l['row_id']:l for l in revision(c,q['revision_id'])['snapshot']['lines']}
 intents=c.execute(text('SELECT id::text,row_id::text,store_id,units FROM purchasing_v4.purchasing_intent WHERE vendor_authorization_id=:id ORDER BY id'),{'id':q['request_id']}).mappings().all()
 if len(intents)!=len(lines)*4:raise controls.Conflict('Original request intent scope is incomplete')
 for intent in intents:
  line=lines[intent['row_id']];index=stores[intent['store_id']];approved=line['units'][index];requested_units=requested[intent['row_id']]['units'][index]
  c.execute(text('INSERT INTO purchasing_v4.intent_consumption(intent_id,vendor_authorization_id) VALUES (:id,:child)'),dict(id=intent['id'],child=child))
  evidence=dict(review_id=p['review_id'] if 'review_id' in p else None,request_revision=q['revision_id'],response_id=q['response_id'],quantity_reason=line['quantity_reason'],excess_reason=line['excess_reason'],original_intent_units=intent['units'],requested_revision_units=requested_units,released_units=max(0,requested_units-approved),additional_units=max(0,approved-requested_units))
  c.execute(text('INSERT INTO purchasing_v4.quotation_intent_resolution(intent_id,vendor_authorization_id,source_request_id,approved_units,requested_units,evidence) VALUES (:id,:child,:request,:approved,:requested,CAST(:e AS jsonb))'),dict(id=intent['id'],child=child,request=q['request_id'],approved=approved,requested=requested_units,e=worksheets.js(evidence)))
 c.execute(text('INSERT INTO purchasing_v4.quotation_financial(purchase_order_id,vendor_authorization_id,merchandise,freight,tax,discount,total,evidence) VALUES (:po,:child,:merchandise,:freight,:tax,:discount,:total,CAST(:e AS jsonb))'),dict(po=po_id,child=child,**{k:financial[k] for k in ['merchandise','freight','tax','discount','total']},e=worksheets.js(financial)))
 current=head(c,q['request_id']);result=dict(current,version=current['version']+1,state='COMMITTED')
 event(c,actor_id,command_id,q['request_id'],'AUTHORIZE',dict(review=q['response_id'],authorization=child),'Owner final authorization; unfilled remainder released explicitly',result)
 c.execute(text('UPDATE purchasing_v4.quotation_head SET version=:version,state=:state,updated_at=now() WHERE request_id=:request_id'),result)
