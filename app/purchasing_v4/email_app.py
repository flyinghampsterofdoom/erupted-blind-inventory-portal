"""Isolated Phase8 application. No live provider configuration is loaded."""
import argparse,json
from fastapi import Depends,Request,HTTPException,BackgroundTasks
from fastapi.responses import Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from app.security.csrf import verify_csrf
from .receiving_app import create_app as receiving_app
from . import vendor_email as mail,email_worker as worker,submission,controls,worksheets
from .calculation_serialization import primitive


def create_app(isolated_url,*,source_namespace,owner_principal_id,authorization_enabled=True,receiving_enabled=True,inventory_adapter=None,email_enabled=True,provider=None,webhook_secret=None):
 app=receiving_app(isolated_url,source_namespace=source_namespace,owner_principal_id=owner_principal_id,authorization_enabled=authorization_enabled,receiving_enabled=receiving_enabled,inventory_adapter=inventory_adapter);engine=app.state.engine
 with engine.connect() as c:
  if c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()!='p8_0007':raise RuntimeError('Explicit isolated email migration required')
 mail_engine=worker.worker_engine(isolated_url);app.state.mail_engine=mail_engine
 import asyncio
 app.state.mail_processing_lock=asyncio.Lock()
 async def process_owned_queue():
  async with app.state.mail_processing_lock:return await run_in_threadpool(lambda:prepare_pending(engine,provider))
 app.state.email_enabled=True;app.state.email_writes_enabled=email_enabled
 @app.on_event('startup')
 async def resume_authorized_mail():
  if provider is not None and app.state.email_writes_enabled:
   import asyncio
   app.state.email_resume_task=asyncio.create_task(process_owned_queue())
 @app.on_event('shutdown')
 async def finish_owned_mail():
  task=getattr(app.state,'email_resume_task',None)
  if task is not None:await task
  mail_engine.dispose()
 def owner(actor_id=Depends(app.state.actor)):
  with engine.connect() as c:submission.owner(c,actor_id)
  return actor_id
 def writer(actor_id=Depends(owner)):
  if not app.state.email_writes_enabled:raise HTTPException(503,'Automated email is paused; original evidence and manual fallback remain available')
  return actor_id
 @app.middleware('http')
 async def schedule(request,call_next):
  response=await call_next(request)
  # Durable intents are queued within existing owner transactions. Only integration
  # work is scheduled after those transactions; ordinary grid edits never enqueue it.
  if provider is not None and app.state.email_writes_enabled and request.method=='POST' and response.status_code<300 and (request.url.path=='/api/purchase-authorizations' or request.url.path.endswith(('/prepare','/decisions','/documents'))):
   from starlette.background import BackgroundTask
   previous=response.background
   async def work():
    if previous:await previous()
    await process_owned_queue()
   response.background=BackgroundTask(work)
  return response
 @app.get('/api/purchasing-email')
 def history(actor_id=Depends(owner)):
  with engine.connect() as c:return primitive(dict(mail.attention(c,actor_id),enabled=app.state.email_writes_enabled,provider_configured=provider is not None))
 @app.post('/api/purchasing-email/deliveries/{did}/retry',dependencies=[Depends(verify_csrf)])
 async def retry(did:str,request:Request,background:BackgroundTasks,actor_id=Depends(writer)):
  value=await bounded(request)
  if set(value)!={'reason','interrupted_worker_confirmed'} or type(value['interrupted_worker_confirmed']) is not bool:raise controls.InvalidPolicy('Explicit owner recovery reason and interrupted-worker confirmation required')
  if not value['reason'].strip():raise controls.InvalidPolicy('Explain the reviewed delivery recovery')
  with engine.connect() as c:
   row=mail.delivery(c,did)
   if row['owner_id']!=actor_id:raise PermissionError('Delivery belongs to another purchasing owner')
  if provider is None:raise HTTPException(503,'No isolated provider configured; manual delivery remains available')
  background.add_task(worker.send,mail_engine,did,provider,retry=True,actor_id=actor_id,reason=value['reason'],interrupted=value['interrupted_worker_confirmed'])
  return dict(queued=True,delivery_id=did)
 @app.post('/api/purchasing-email/deliveries/{did}/manual',dependencies=[Depends(verify_csrf)])
 async def manual(did:str,request:Request,actor_id=Depends(owner)):
  value=await bounded(request)
  if set(value)!={'command_id','reason','interrupted_worker_confirmed'} or not isinstance(value['reason'],str) or not value['reason'].strip() or type(value['interrupted_worker_confirmed']) is not bool:raise controls.InvalidPolicy('Explicit owner manual-handoff record required')
  cid=controls.uuid(value['command_id'])
  with engine.begin() as c:
   prior=c.execute(text('SELECT owner_id,subject,reason FROM purchasing_v4.mail_resolution WHERE id=:id'),{'id':cid}).mappings().one_or_none()
   if prior:
    if dict(prior)!=dict(owner_id=actor_id,subject=did,reason=value['reason']):raise controls.Conflict('Manual handoff identity already records a different decision')
    return dict(recorded=True,replayed=True)
   c.execute(text('SELECT 1 FROM purchasing_v4.mail_state WHERE delivery_id=:id FOR UPDATE'),{'id':controls.uuid(did)});row=mail.delivery(c,did)
   if row['owner_id']!=actor_id:raise PermissionError('Delivery belongs to another owner')
   if row['state']=='SENDING' and not value['interrupted_worker_confirmed']:raise controls.Conflict('Confirm the previous worker has stopped before recording manual fallback')
   c.execute(text('INSERT INTO purchasing_v4.mail_resolution(id,owner_id,subject,reason) VALUES (:id,:owner,:subject,:reason)'),dict(id=cid,owner=actor_id,subject=did,reason=value['reason']))
   if row['state']!='DELIVERED':mail.record_attempt(c,row,'OWNER_MANUAL_HANDOFF','HELD',dict(detail='Owner reports manual handoff; automatic delivery is held. Original provider state: '+row['state'],owner_id=actor_id,reason=value['reason'],manual_command_id=cid))
  return dict(recorded=True,replayed=False)
 @app.post('/api/purchasing-email/deliveries/{did}/reconcile',dependencies=[Depends(verify_csrf)])
 async def reconcile(did:str,request:Request,background:BackgroundTasks,actor_id=Depends(writer)):
  value=await bounded(request)
  if set(value)!={'reason','provider_id'} or not isinstance(value['reason'],str) or not value['reason'].strip():raise controls.InvalidPolicy('Explicit owner reason and provider message reference required')
  controls.uuid(value['provider_id'])
  if provider is None:raise HTTPException(503,'No isolated provider configured')
  with engine.connect() as c:
   if mail.delivery(c,did)['owner_id']!=actor_id:raise PermissionError('Delivery belongs to another owner')
  background.add_task(worker.reconcile,mail_engine,did,provider,provider_id=value['provider_id'],actor_id=actor_id,reason=value['reason']);return dict(queued=True,delivery_id=did)
 @app.post('/api/purchasing-email/events/{event_id}/retry',dependencies=[Depends(verify_csrf)])
 async def retry_event(event_id:str,request:Request,background:BackgroundTasks,actor_id=Depends(writer)):
  value=await bounded(request)
  if set(value)!={'reason','interrupted_worker_confirmed'} or not value['reason'].strip() or type(value['interrupted_worker_confirmed']) is not bool:raise controls.InvalidPolicy('Explicit owner recovery decision required')
  if provider is None:raise HTTPException(503,'No isolated provider configured')
  with engine.begin() as c:
   state=c.execute(text('SELECT state FROM purchasing_v4.mail_event_state WHERE event_id=:id FOR UPDATE'),{'id':event_id}).scalar_one()
   if state=='PROCESSING':
    if not value['interrupted_worker_confirmed']:raise controls.Conflict('Confirm prior worker has stopped before recovering its original event')
    c.execute(text("UPDATE purchasing_v4.mail_event_state SET state='FAILED',version=version+1,updated_at=now() WHERE event_id=:id"),{'id':event_id})
   c.execute(text('INSERT INTO purchasing_v4.mail_resolution(id,owner_id,subject,reason) VALUES (:id,:owner,:subject,:reason)'),dict(id=str(__import__('uuid').uuid4()),owner=actor_id,subject=event_id,reason=value['reason']))
  background.add_task(worker.process_event,mail_engine,event_id,provider);return dict(queued=True,event_id=event_id)
 @app.get('/api/purchasing-email/attachments/{aid}')
 def attachment(aid:str,actor_id=Depends(owner)):
  with engine.connect() as c:r=c.execute(text('SELECT content,filename FROM purchasing_v4.mail_attachment WHERE id=:id'),{'id':controls.uuid(aid)}).mappings().one_or_none()
  if not r or r['content'] is None:raise HTTPException(404,'Standalone attachment unavailable; original message remains retained')
  return Response(bytes(r['content']),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="retained-vendor-attachment.bin"','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
 @app.get('/api/purchasing-email/messages/{pid}/original')
 def original(pid:str,actor_id=Depends(owner)):
  with engine.connect() as c:raw=c.execute(text('SELECT original FROM purchasing_v4.mail_message WHERE provider_id=:id'),{'id':pid}).scalar_one_or_none()
  if raw is None:raise HTTPException(404,'Original message unavailable')
  return Response(bytes(raw),media_type='application/octet-stream',headers={'Content-Disposition':'attachment; filename="vendor-message.eml"','Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
 @app.post('/api/purchasing-email/resend/events')
 async def webhook(request:Request,background:BackgroundTasks):
  if not app.state.email_writes_enabled or not webhook_secret or provider is None:raise HTTPException(503,'Inbound integration is not activated')
  raw=b''
  async for chunk in request.stream():
   raw+=chunk
   if len(raw)>1024*1024:raise HTTPException(413,'Event exceeds its bound')
  try:
   with engine.begin() as c:result=mail.accept_event(c,raw,request.headers,webhook_secret)
  except PermissionError:raise HTTPException(401,'Invalid authenticated provider event') from None
  background.add_task(worker.process_event,mail_engine,result['event_id'],provider);return result
 return app

async def bounded(request):
 raw=await request.body()
 if len(raw)>10000:raise HTTPException(413,'Decision exceeds its bound')
 try:value=json.loads(raw)
 except ValueError:raise controls.InvalidPolicy('Object decision required') from None
 if not isinstance(value,dict):raise controls.InvalidPolicy('Object decision required')
 return value

def prepare_pending(engine,provider):
 from . import outbox
 results=[]
 # Bounded draining of already-authorized integration work, with no purchasing
 # calculations or new commitments. One bad document does not suppress others.
 for _ in range(50):
  with engine.connect() as c:commands=list(c.execute(text("SELECT x.id::text FROM purchasing_v4.integration_command x JOIN purchasing_v4.integration_state s ON s.command_id=x.id JOIN purchasing_v4.mail_delivery d ON d.vendor_authorization_id=x.vendor_authorization_id WHERE x.kind='GENERATE_DOCUMENTS' AND s.state='PENDING' AND d.revision_id IS NULL LIMIT 20")).scalars())
  if commands:
   e=outbox.worker_engine(engine.url)
   try:
    for cid in commands:
     try:outbox.generate_documents(e,cid)
     except Exception:pass  # The durable document state/evidence remains authoritative.
   finally:e.dispose()
  e=worker.worker_engine(engine.url)
  try:result=worker.process_pending(e,provider)
  finally:e.dispose()
  results.append(result)
  if not result['events'] and (not result['deliveries'] or all(x.get('state')=='NOT_SENT' for x in result['deliveries'])):break
 return results


def main():
 p=argparse.ArgumentParser();p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--owner-id',type=int,required=True);p.add_argument('--port',type=int,default=58453);p.add_argument('--simulate-email',action='store_true');p.add_argument('--disable-email',action='store_true');p.add_argument('--disable-authorization',action='store_true');p.add_argument('--disable-receiving',action='store_true');a=p.parse_args()
 provider=None;secret=None
 if a.simulate_email:
  from sqlalchemy import create_engine
  from .email_simulator import FixtureResend
  provider=FixtureResend(create_engine(a.isolated_url));secret='whsec_'+__import__('base64').b64encode(b'isolated-fixture-webhook-key-only').decode()
 import uvicorn
 uvicorn.run(create_app(a.isolated_url,source_namespace=a.source_namespace,owner_principal_id=a.owner_id,provider=provider,webhook_secret=secret,email_enabled=not a.disable_email,authorization_enabled=not a.disable_authorization,receiving_enabled=not a.disable_receiving),host='127.0.0.1',port=a.port)

if __name__=='__main__':main()
