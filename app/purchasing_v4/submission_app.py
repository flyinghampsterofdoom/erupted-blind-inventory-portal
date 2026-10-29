"""Phase5 standalone owner application. Never mounted into production routing."""
import argparse,hashlib,json
from pathlib import Path
from fastapi import Depends,Request,HTTPException
from fastapi.responses import Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from app.security.csrf import verify_csrf
from .worksheet_app import create_app as worksheet_app
from . import submission,outbox,controls,worksheets


def create_app(isolated_url,*,source_namespace,owner_principal_id,authorization_enabled=True):
    app=worksheet_app(isolated_url,source_namespace=source_namespace);engine=app.state.engine;actor=app.state.actor
    with engine.connect() as c:
        if c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()not in ('p5_0001','p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007','p9_0001'):raise RuntimeError('Explicit isolated commitment migration required')
        submission.owner(c,owner_principal_id)
    worker=outbox.worker_engine(engine.url);app.state.worker_engine=worker
    app.state.authorization_enabled=authorization_enabled;app.state.submission_enabled=True;app.state.authorization_owner_id=owner_principal_id
    def authorized_actor(owner_id=Depends(actor)):
        with engine.connect() as c:submission.owner(c,owner_id)
        return owner_id
    def can_authorize(owner_id=Depends(authorized_actor)):
        if not app.state.authorization_enabled:raise HTTPException(503,'New purchasing authorization is paused; existing documents and commitments remain preserved')
        return owner_id

    async def body(request):
        raw=await request.body()
        if len(raw)>8000000:raise HTTPException(413,'Review exceeds the qualified request bound')
        value=json.loads(raw)
        if not isinstance(value,dict):raise controls.InvalidPolicy('Object command required')
        return value

    @app.post('/api/submission-reviews',dependencies=[Depends(verify_csrf)])
    async def review(request:Request,owner_id=Depends(can_authorize)):
        value=await body(request)
        allowed={'review_id','session_id','expected_version','supplier_ids','currency','valuations','incremental_reason','public_header','previous_review_id'}
        if set(value)-allowed or not {'review_id','session_id','expected_version','supplier_ids'}<=set(value):raise controls.InvalidPolicy('Unknown review fields')
        return await run_in_threadpool(lambda:submission.prepare_review(engine,actor_id=owner_id,source_namespace=source_namespace,**value))

    @app.post('/api/purchase-authorizations',dependencies=[Depends(verify_csrf)])
    async def authorize(request:Request,owner_id=Depends(can_authorize)):
        value=await body(request)
        if set(value)!={'command_id','review_id','acknowledged'}:raise controls.InvalidPolicy('Exact reviewed authorization identity and acknowledgment required')
        def execute():
            with engine.begin() as c:return submission.authorize(c,actor_id=owner_id,source_namespace=source_namespace,**value)
        return await run_in_threadpool(execute)

    @app.get('/api/authorization-commands/{command_id}')
    def command(command_id:str,owner_id=Depends(authorized_actor)):
        with engine.connect() as c:
            result=c.execute(text('SELECT result FROM purchasing_v4.authorization_command WHERE id=:id AND owner_id=:owner'),dict(id=controls.uuid(command_id),owner=owner_id)).scalar_one_or_none()
            if result is None:raise HTTPException(404,'No committed receipt recorded for this command; retry only the same immutable authorization')
            return result

    @app.get('/api/purchase-authorizations')
    def authorizations(owner_id=Depends(authorized_actor)):
        with engine.connect() as c:return [dict(r) for r in c.execute(text('SELECT id::text,result,created_at FROM purchasing_v4.purchase_authorization WHERE owner_id=:owner ORDER BY created_at DESC LIMIT 100'),{'owner':owner_id}).mappings()]

    def child(c,child_id,owner_id):
        value=c.execute(text('SELECT v.* FROM purchasing_v4.authorized_vendor v JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id WHERE v.id=:id AND a.owner_id=:owner'),dict(id=controls.uuid(child_id),owner=owner_id)).mappings().one_or_none()
        if not value:raise HTTPException(404,'Unknown owner-authorized vendor order/request')
        return value

    @app.get('/api/vendor-authorizations/{child_id}')
    def vendor(child_id:str,owner_id=Depends(authorized_actor)):
        with engine.connect() as c:
            value=dict(child(c,child_id,owner_id));commands=[dict(r) for r in c.execute(text('SELECT x.id::text,x.kind,s.state,s.evidence FROM purchasing_v4.integration_command x JOIN purchasing_v4.integration_state s ON s.command_id=x.id WHERE x.vendor_authorization_id=:id ORDER BY x.kind'),{'id':child_id}).mappings()]
            files=[dict(r) for r in c.execute(text('SELECT id::text,format,sha256 FROM purchasing_v4.vendor_artifact WHERE vendor_authorization_id=:id ORDER BY format'),{'id':child_id}).mappings()]
            return dict(id=child_id,kind=value['kind'],purchase_order_id=value['purchase_order_id'],snapshot=value['snapshot'],commands=commands,artifacts=files)

    @app.post('/api/vendor-authorizations/{child_id}/documents',dependencies=[Depends(verify_csrf)])
    async def documents(child_id:str,owner_id=Depends(authorized_actor)):
        with engine.connect() as c:
            child(c,child_id,owner_id);command=c.execute(text("SELECT id::text FROM purchasing_v4.integration_command WHERE vendor_authorization_id=:id AND kind='GENERATE_DOCUMENTS'"),{'id':child_id}).scalar_one()
        return await run_in_threadpool(lambda:outbox.generate_documents(worker,command))

    @app.get('/api/vendor-artifacts/{artifact_id}')
    def artifact(artifact_id:str,owner_id=Depends(authorized_actor)):
        with engine.connect() as c:
            row=c.execute(text('''SELECT f.* FROM purchasing_v4.vendor_artifact f JOIN purchasing_v4.authorized_vendor v ON v.id=f.vendor_authorization_id JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id WHERE f.id=:id AND a.owner_id=:owner'''),dict(id=controls.uuid(artifact_id),owner=owner_id)).mappings().one_or_none()
            if not row:raise HTTPException(404,'Original vendor document unavailable')
            content=bytes(row['content'])
            if hashlib.sha256(content).hexdigest()!=row['sha256']:raise HTTPException(503,'Original document integrity failed; restore and reconcile the original, never regenerate from current data')
            format=row['format'].lower();filename=('purchase-order-' if row['public_projection']['kind']=='PO' else 'purchasing-request-')+row['public_projection']['number'].replace('PO #','').replace('Request ','')+'.'+format
            return Response(content,media_type='application/pdf' if format=='pdf' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="'+filename+'"','Cache-Control':'private, no-store','X-Content-SHA256':row['sha256']})
    return app


def main():
    p=argparse.ArgumentParser();p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--owner-id',type=int,required=True);p.add_argument('--disable-authorization',action='store_true');p.add_argument('--port',type=int,default=58443);a=p.parse_args()
    import uvicorn
    uvicorn.run(create_app(a.isolated_url,source_namespace=a.source_namespace,owner_principal_id=a.owner_id,authorization_enabled=not a.disable_authorization),host='127.0.0.1',port=a.port)

if __name__=='__main__':main()
