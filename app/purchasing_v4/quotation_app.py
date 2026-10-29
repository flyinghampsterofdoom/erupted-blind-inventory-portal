"""Isolated manual quotation interface. Reuses the Phase5 authorization endpoint."""
import argparse,hashlib,json
from fastapi import Depends,Request,HTTPException
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy import text
from app.security.csrf import verify_csrf
from .submission_app import create_app as submission_app
from . import quotations,submission,controls
from .quotation_workbooks import MAX_FILE
from .calculation_serialization import primitive


def create_app(isolated_url,*,source_namespace,owner_principal_id,authorization_enabled=True):
 app=submission_app(isolated_url,source_namespace=source_namespace,owner_principal_id=owner_principal_id,authorization_enabled=authorization_enabled);engine=app.state.engine;app.state.quotation_enabled=True
 with engine.connect() as c:
  if c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()not in ('p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007','p9_0001','d1_0001'):raise RuntimeError('Explicit isolated quotation migration required')
 def owner(actor_id=Depends(app.state.actor)):
  with engine.connect() as c:submission.owner(c,actor_id)
  return actor_id
 def writer(actor_id=Depends(owner)):
  if not app.state.authorization_enabled:raise HTTPException(503,'New quotation decisions and purchasing authorization are paused; original evidence remains available')
  return actor_id
 async def body(request):
  raw=await request.body()
  if len(raw)>8000000:raise HTTPException(413,'Quotation decision exceeds the qualified bound')
  try:value=json.loads(raw)
  except ValueError:raise controls.InvalidPolicy('Object decision required') from None
  if not isinstance(value,dict):raise controls.InvalidPolicy('Object decision required')
  return value
 def exact(value,required,optional=()):
  if not set(required)<=set(value) or set(value)-set(required)-set(optional):raise controls.InvalidPolicy('Exact quotation decision fields are required')

 @app.get('/api/quotation-requests')
 def listing(actor_id=Depends(owner)):
  with engine.connect() as c:return primitive([dict(r) for r in c.execute(text("SELECT v.id::text,v.supplier_id::text,v.snapshot->'supplier'->>'name' AS supplier_name,v.created_at,h.version,h.state,h.revision_id::text,h.selected_response_id::text FROM purchasing_v4.authorized_vendor v JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id LEFT JOIN purchasing_v4.quotation_head h ON h.request_id=v.id WHERE v.kind='REQUEST' AND a.owner_id=:owner ORDER BY v.created_at DESC LIMIT 100"),{'owner':actor_id}).mappings()])

 @app.post('/api/quotation-requests/{request_id}/prepare',dependencies=[Depends(verify_csrf)])
 async def prepare(request_id:str,request:Request,actor_id=Depends(writer)):
  value=await body(request);exact(value,{'command_id'})
  def run():
   with engine.begin() as c:return quotations.prepare(c,actor_id=actor_id,request_id=request_id,**value)
  return await run_in_threadpool(run)

 @app.get('/api/quotation-requests/{request_id}')
 def details(request_id:str,actor_id=Depends(owner)):
  with engine.connect() as c:return quotations.details(c,actor_id=actor_id,request_id=request_id)

 @app.get('/api/quotation-requests/{request_id}/workbooks/{revision_id}')
 def workbook(request_id:str,revision_id:str,actor_id=Depends(owner)):
  with engine.connect() as c:
   quotations.request(c,request_id,actor_id);r=quotations.revision(c,revision_id)
   if str(r['request_id'])!=request_id:raise HTTPException(404,'Workbook does not belong to this request')
   content=bytes(r['template'])
   if hashlib.sha256(content).hexdigest()!=r['sha256']:raise HTTPException(503,'Original quotation template failed integrity verification; restore the original')
   return Response(content,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="quotation-{request_id}-r{r["number"]}.xlsx"','Cache-Control':'private, no-store','X-Content-SHA256':r['sha256']})

 @app.post('/api/quotation-requests/{request_id}/uploads',dependencies=[Depends(verify_csrf)])
 async def upload(request_id:str,request:Request,filename:str,actor_id=Depends(writer)):
  size=0;chunks=[]
  async for chunk in request.stream():
   size+=len(chunk)
   if size>MAX_FILE:raise HTTPException(413,'Upload at most 10 MiB; no file was accepted')
   chunks.append(chunk)
  return await run_in_threadpool(lambda:quotations.intake(engine,actor_id=actor_id,request_id=request_id,filename=filename,content=b''.join(chunks)))

 @app.get('/api/quotation-requests/{request_id}/uploads/{upload_id}')
 def original_upload(request_id:str,upload_id:str,actor_id=Depends(owner)):
  with engine.connect() as c:
   quotations.request(c,request_id,actor_id);r=c.execute(text('SELECT content,sha256 FROM purchasing_v4.quotation_upload WHERE id=:id AND request_id=:request'),dict(id=controls.uuid(upload_id),request=request_id)).mappings().one_or_none()
   if not r:raise HTTPException(404,'Original uploaded evidence unavailable')
   content=bytes(r['content'])
   if hashlib.sha256(content).hexdigest()!=r['sha256']:raise HTTPException(503,'Original uploaded evidence failed integrity verification')
   return Response(content,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="quotation-evidence-{upload_id}.xlsx"','Cache-Control':'private, no-store','X-Content-SHA256':r['sha256']})

 @app.post('/api/quotation-requests/{request_id}/decisions',dependencies=[Depends(verify_csrf)])
 async def decisions(request_id:str,request:Request,actor_id=Depends(writer)):
  value=await body(request);exact(value,{'command_id','expected_version','kind','reason'},{'response_id','changes'})
  def run():
   with engine.begin() as c:return quotations.transition(c,actor_id=actor_id,request_id=request_id,**value)
  return await run_in_threadpool(run)

 @app.post('/api/quotation-requests/{request_id}/reviews',dependencies=[Depends(verify_csrf)])
 async def reviews(request_id:str,request:Request,actor_id=Depends(writer)):
  value=await body(request);exact(value,{'review_id','expected_version','response_id','choices','components','contract','unit_adapter_approved','release_remainder','incremental_reason'})
  return await run_in_threadpool(lambda:quotations.prepare_final_review(engine,actor_id=actor_id,request_id=request_id,source_namespace=source_namespace,**value))

 @app.get('/api/quotation-requests/{request_id}/reviews')
 def review_history(request_id:str,actor_id=Depends(owner)):
  with engine.connect() as c:
   quotations.request(c,request_id,actor_id)
   return primitive([dict(r) for r in c.execute(text("SELECT id::text,created_at,payload->'blocking' AS blocking,payload->>'committed_total' AS committed_total,payload->'quotation'->>'revision_number' AS request_revision FROM purchasing_v4.submission_review WHERE owner_id=:owner AND payload->'quotation'->>'request_id'=:request ORDER BY created_at DESC LIMIT 100"),dict(owner=actor_id,request=request_id)).mappings()])
 @app.get('/api/quotation-requests/{request_id}/reviews/{review_id}')
 def original_review(request_id:str,review_id:str,actor_id=Depends(owner)):
  with engine.connect() as c:
   quotations.request(c,request_id,actor_id)
   value=c.execute(text("SELECT payload FROM purchasing_v4.submission_review WHERE id=:id AND owner_id=:owner AND payload->'quotation'->>'request_id'=:request"),dict(id=controls.uuid(review_id),owner=actor_id,request=request_id)).scalar_one_or_none()
   if value is None:raise HTTPException(404,'Original owner review unavailable')
   return value

 return app


def main():
 p=argparse.ArgumentParser();p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--owner-id',type=int,required=True);p.add_argument('--disable-authorization',action='store_true');p.add_argument('--port',type=int,default=58446);a=p.parse_args()
 import uvicorn
 uvicorn.run(create_app(a.isolated_url,source_namespace=a.source_namespace,owner_principal_id=a.owner_id,authorization_enabled=not a.disable_authorization),host='127.0.0.1',port=a.port)

if __name__=='__main__':main()
