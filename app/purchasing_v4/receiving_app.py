"""Explicit loopback-only Phase7 application. No live Square worker is installed."""
import argparse,json
from pathlib import Path
from fastapi import Request,Depends,HTTPException,BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app.security.sessions import load_session_from_token
from app.security.csrf import verify_csrf
from .quotation_app import create_app as quotation_app
from .management_app import COOKIE
from .calculation_serialization import primitive
from . import receiving as receiving,controls,worksheets


def create_app(isolated_url,*,source_namespace,owner_principal_id,authorization_enabled=True,receiving_enabled=True,inventory_adapter=None):
    app=quotation_app(isolated_url,source_namespace=source_namespace,owner_principal_id=owner_principal_id,authorization_enabled=authorization_enabled);engine=app.state.engine
    with engine.connect() as c:
        if c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()not in ('p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007'):raise RuntimeError('Explicit isolated receiving migration required')
    app.state.receiving_enabled=True;app.state.receiving_writes_enabled=receiving_enabled
    def actor(request:Request):
        with Session(engine) as db:loaded=load_session_from_token(db,request.cookies.get(COOKIE),renew=False)
        if not loaded or not loaded[1].active:raise HTTPException(401,'Sign in to receiving')
        with engine.connect() as c:receiving.capability(c,loaded[1].id,'VIEW',receiving.fixed_stores(c)[1]['id'])
        return loaded[1].id
    def writer(actor_id=Depends(actor)):
        if not app.state.receiving_writes_enabled:raise HTTPException(503,'New receiving and inventory movements are paused; original facts remain available')
        return actor_id
    def login_allowed(principal):
        try:
            with engine.connect() as c:receiving.capability(c,principal,'VIEW',receiving.fixed_stores(c)[1]['id'])
            return True
        except PermissionError:return False
    app.state.receiving_login_allowed=login_allowed
    async def body(request):
        raw=await request.body()
        if len(raw)>2000000:raise HTTPException(413,'Receiving submission exceeds its bounded limit')
        value=json.loads(raw)
        if not isinstance(value,dict):raise controls.InvalidPolicy('Object receiving command required')
        return value

    @app.get('/receiving',response_class=HTMLResponse)
    def home(request:Request,actor_id=Depends(actor)):
        static=Path(__file__).with_name('worksheet_static');assets=json.loads((static/'assets.json').read_text())
        return '<!doctype html><html lang="en"><meta charset="utf-8"><title>Receiving · isolated</title><link rel="stylesheet" href="/worksheet-assets/'+assets['css']+'"><body><main id="receiving-home"><h1>Receiving · HWY 99</h1><p>Isolated development · no real Square posting</p><div id="receiving-list"></div></main><script type="module" src="/worksheet-assets/'+assets['receiving_js']+'"></script></body></html>'

    @app.get('/api/receiving/context')
    def context(actor_id=Depends(actor)):
        with engine.connect() as c:
            return dict(actor_id=actor_id,owner=receiving.is_owner(c,actor_id),enabled=app.state.receiving_writes_enabled,central=receiving.fixed_stores(c)[1])

    @app.get('/api/receiving/orders')
    def orders(after:int=0,actor_id=Depends(actor)):
        with engine.connect() as c:
            rows=[dict(r) for r in c.execute(text('''SELECT p.id,p.status::text,v.name AS supplier,p.created_at,o.origin,
             ro.central_store_id,h.version FROM public.purchase_orders p JOIN public.vendors v ON v.id=p.vendor_id
             JOIN purchasing_v4.po_ownership o ON o.purchase_order_id=p.id
             LEFT JOIN purchasing_v4.receiving_order ro ON ro.purchase_order_id=p.id LEFT JOIN purchasing_v4.receiving_head h ON h.purchase_order_id=p.id
             WHERE (:after=0 OR p.id<:after) ORDER BY p.id DESC LIMIT 101'''),{'after':after}).mappings()]
            return primitive(dict(rows=rows[:100],next_cursor=rows[99]['id'] if len(rows)>100 else None))

    @app.post('/api/receiving/orders/{po_id}/open',dependencies=[Depends(verify_csrf)])
    async def open_order(po_id:int,actor_id=Depends(writer)):
        def execute():
            with engine.begin() as c:
                c.execute(text('SELECT pg_advisory_xact_lock(73520707,:po)'),{'po':po_id})
                if not c.execute(text('SELECT 1 FROM purchasing_v4.receiving_order WHERE purchase_order_id=:po'),{'po':po_id}).scalar_one_or_none():receiving.capability(c,actor_id,'ENTER',receiving.fixed_stores(c)[1]['id'])
                receiving.capture_order(c,po_id)
                return primitive(receiving.projection(c,po_id,actor_id=actor_id))
        return await run_in_threadpool(execute)

    @app.get('/api/receiving/orders/{po_id}')
    def order(po_id:int,actor_id=Depends(actor)):
        with engine.connect() as c:return primitive(receiving.projection(c,po_id,actor_id=actor_id))

    @app.post('/api/receiving/orders/{po_id}/commands',dependencies=[Depends(verify_csrf)])
    async def commands(po_id:int,request:Request,background_tasks:BackgroundTasks,actor_id=Depends(writer)):
        value=await body(request)
        if set(value)!={'command_id','expected_version','kind','lines','reason','confirmed'}:raise controls.InvalidPolicy('Exact reviewed receiving command fields required')
        def execute():
            with engine.begin() as c:return receiving.command(c,actor_id=actor_id,purchase_order_id=po_id,**value)
        result=await run_in_threadpool(execute)
        if inventory_adapter is not None:
            from .receiving_worker import process_order
            background_tasks.add_task(process_order,engine,po_id,inventory_adapter)
        return result

    @app.get('/api/receiving/orders/{po_id}/draft')
    def draft(po_id:int,actor_id=Depends(actor)):
        with engine.connect() as c:
            receiving.projection(c,po_id,actor_id=actor_id)
            row=c.execute(text('SELECT version,base_version,payload FROM purchasing_v4.receiving_draft WHERE purchase_order_id=:po AND actor_id=:actor'),dict(po=po_id,actor=actor_id)).mappings().one_or_none()
            return dict(row) if row else dict(version=0,base_version=None,payload=None)

    @app.post('/api/receiving/orders/{po_id}/draft',dependencies=[Depends(verify_csrf)])
    async def save_draft(po_id:int,request:Request,actor_id=Depends(writer)):
        value=await body(request)
        if set(value)!={'expected_draft_version','base_version','payload'}:raise controls.InvalidPolicy('Exact receiving draft fields required')
        with engine.begin() as c:return receiving.save_draft(c,actor_id=actor_id,purchase_order_id=po_id,**value)

    @app.get('/api/receiving/commands/{command_id}')
    def receipt(command_id:str,actor_id=Depends(actor)):
        with engine.connect() as c:
            row=c.execute(text('SELECT result,actor_id FROM purchasing_v4.receiving_event WHERE id=:id'),{'id':controls.uuid(command_id)}).mappings().one_or_none()
            if not row:raise HTTPException(404,'No committed receiving command; retain and retry the exact original identity')
            if row['actor_id']!=actor_id and not receiving.is_owner(c,actor_id):raise PermissionError('This receiving receipt belongs to another employee')
            return row['result']

    @app.post('/api/receiving/orders/{po_id}/inventory',dependencies=[Depends(verify_csrf)])
    async def reconcile(po_id:int,actor_id=Depends(writer)):
        with engine.connect() as c:
            value=receiving.projection(c,po_id,actor_id=actor_id);receiving.capability(c,actor_id,'CONFIRM',value['snapshot']['central']['id'])
        if inventory_adapter is None:raise HTTPException(503,'Live Square posting is unavailable; configure only the explicit isolated simulator')
        from .receiving_worker import process_order
        return await run_in_threadpool(lambda:process_order(engine,po_id,inventory_adapter))

    @app.post('/api/receiving/inventory/{command_id}/retry',dependencies=[Depends(verify_csrf)])
    async def retry(command_id:str,request:Request,actor_id=Depends(writer)):
        value=await body(request)
        if set(value)!={'reason','interrupted_worker_confirmed'} or type(value['interrupted_worker_confirmed']) is not bool:raise controls.InvalidPolicy('Explicit owner reconciliation and worker status required')
        with engine.connect() as c:
            from .submission import owner
            owner(c,actor_id)
        if inventory_adapter is None:raise HTTPException(503,'Live Square posting is unavailable')
        from .receiving_worker import recover_interrupted,retry_same_identity
        if value['interrupted_worker_confirmed']:
            await run_in_threadpool(lambda:recover_interrupted(engine,command_id,actor_id=actor_id,reason=value['reason']))
        return await run_in_threadpool(lambda:retry_same_identity(engine,command_id,inventory_adapter,actor_id=actor_id,reason=value['reason']))

    @app.post('/api/receiving/capabilities',dependencies=[Depends(verify_csrf)])
    async def capabilities(request:Request,actor_id=Depends(writer)):
        value=await body(request)
        if set(value)!={'command_id','principal_id','kind','enabled','reason','store_id'}:raise controls.InvalidPolicy('Exact capability decision required')
        with engine.begin() as c:return receiving.grant(c,actor_id=actor_id,**value)
    return app


def main():
    p=argparse.ArgumentParser();p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--owner-id',type=int,required=True);p.add_argument('--port',type=int,default=58449);p.add_argument('--disable-authorization',action='store_true');p.add_argument('--disable-receiving',action='store_true');p.add_argument('--simulate-square',action='store_true');a=p.parse_args()
    import uvicorn
    adapter=None
    if a.simulate_square:
        from sqlalchemy import create_engine
        from .receiving_simulator import FixtureInventoryTransport
        from .receiving_square import InventoryAdapter
        adapter=InventoryAdapter(FixtureInventoryTransport(create_engine(a.isolated_url)))
    uvicorn.run(create_app(a.isolated_url,source_namespace=a.source_namespace,owner_principal_id=a.owner_id,authorization_enabled=not a.disable_authorization,receiving_enabled=not a.disable_receiving,inventory_adapter=adapter),host='127.0.0.1',port=a.port)

if __name__=='__main__':main()
