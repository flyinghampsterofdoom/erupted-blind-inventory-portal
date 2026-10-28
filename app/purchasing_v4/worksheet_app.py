"""Isolated desktop worksheet HTTP boundary. Selective submission preview only."""
import argparse,json
from pathlib import Path
from uuid import UUID,uuid5
from datetime import datetime,timedelta,timezone
from fastapi import Depends,Request,HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from .management_app import create_app as management_app,STATIC as MANAGEMENT_STATIC
from . import worksheets,controls
from .calculation_serialization import primitive
from app.security.csrf import verify_csrf
STATIC=Path(__file__).with_name('worksheet_static')


def create_app(isolated_url,*,source_namespace):
    app=management_app(isolated_url,source_namespace=source_namespace);engine=app.state.engine;actor=app.state.actor
    with engine.connect() as c:
        if c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()!='p4_0003':raise RuntimeError('Explicit worksheet migration required')
    app.mount('/worksheet-assets',StaticFiles(directory=STATIC),name='isolated-worksheet-assets')
    def home(owner_id=Depends(actor)):
        assets=json.loads((STATIC/'assets.json').read_text());return HTMLResponse((STATIC/'index.html').read_text().replace('{{JS}}',assets['js']).replace('{{CSS}}',assets['css']))
    for route in app.routes:
        if getattr(route,'path',None)=='/' and 'GET' in getattr(route,'methods',[]):app.routes.remove(route);break
    app.add_api_route('/',home,methods=['GET'])

    @app.get('/management',response_class=HTMLResponse)
    def management(owner_id=Depends(actor)):return (MANAGEMENT_STATIC/'index.html').read_text()

    async def body(request):
        raw=await request.body()
        if len(raw)>8000000:raise HTTPException(413,'Worksheet command exceeds limit')
        result=json.loads(raw)
        if not isinstance(result,dict):raise HTTPException(422,'Object command required')
        return result

    @app.get('/api/worksheets')
    def drafts(owner_id=Depends(actor),after:str=''):
        stamp,after_id=after.rsplit('/',1) if after else (datetime.max.replace(tzinfo=timezone.utc).isoformat(),'00000000-0000-0000-0000-000000000000')
        controls.uuid(after_id);datetime.fromisoformat(stamp)
        with engine.connect() as c:
            rows=c.execute(text("SELECT id::text,title,version,state,updated_at FROM purchasing_v4.worksheet WHERE owner_id=:owner AND state='EDITABLE' AND (:after='' OR updated_at<CAST(:stamp AS timestamptz) OR (updated_at=CAST(:stamp AS timestamptz) AND id>CAST(:after_id AS uuid))) ORDER BY updated_at DESC,id LIMIT 101"),dict(owner=owner_id,after=after,stamp=stamp,after_id=after_id)).mappings().all()
        return primitive(dict(rows=[dict(r) for r in rows[:100]],next_cursor=rows[99]['updated_at'].isoformat()+'/'+rows[99]['id'] if len(rows)>100 else None))

    @app.get('/api/worksheets/{session_id}/inventory')
    def inventory(session_id:str,max_age_seconds:int|None=None,owner_id=Depends(actor)):
        from .worksheet_inventory import inventory_view
        from .isolation import readonly_snapshot
        if max_age_seconds is not None and not 0<=max_age_seconds<=315360000:raise controls.InvalidPolicy('Enter an explicit inventory age in seconds, at most ten years')
        with readonly_snapshot(engine) as c:return inventory_view(c,session_id=session_id,actor_id=owner_id,source_namespace=source_namespace,as_of=datetime.now(timezone.utc),max_age=timedelta(seconds=max_age_seconds) if max_age_seconds is not None else None)

    @app.get('/api/worksheets/{session_id}')
    def read(session_id:str,owner_id=Depends(actor)):
        with engine.connect() as c:return worksheets.snapshot(c,session_id,owner_id)

    @app.post('/api/worksheets',dependencies=[Depends(verify_csrf)])
    async def fresh(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        def perform():
            # Validate lifecycle identity before any calculation on a retry.
            with engine.connect() as c:
                replay=worksheets.owner_receipt(c,owner_id,value['command_id'],'START_FRESH',value)
                if replay:return worksheets.snapshot(c,replay['session_id'],owner_id)
            rows=[];generation=None
            if value.get('calculate'):
                age=value.get('max_age_seconds')
                if type(age) is not int or age<0:raise controls.InvalidPolicy('Explicit evidence age required')
                rows,generation=worksheets.prepare_generation(engine,actor_id=owner_id,source_namespace=source_namespace,as_of=datetime.fromisoformat(value['as_of']) if value.get('as_of') else datetime.now(timezone.utc),max_age=timedelta(seconds=age))
            with engine.begin() as c:
                state=worksheets.create(c,actor_id=owner_id,command_id=value['command_id'],title=value.get('title','Purchasing worksheet'),rows=rows,generation=generation)
                worksheets.record_owner_receipt(c,owner_id,value['command_id'],'START_FRESH',value,dict(session_id=state['session']['id']))
                return state
        return await run_in_threadpool(perform)

    @app.post('/api/worksheets/{session_id}/commands',dependencies=[Depends(verify_csrf)])
    async def command(session_id:str,request:Request,owner_id=Depends(actor)):
        value=await body(request)
        def write():
            with engine.begin() as c:return worksheets.patch(c,actor_id=owner_id,session_id=session_id,command_id=value['command_id'],expected_version=value['expected_version'],changes=value['changes'],defaults=value.get('defaults'),discard=value.get('discard',False))
        return await run_in_threadpool(write)

    @app.post('/api/worksheets/{session_id}/add',dependencies=[Depends(verify_csrf)])
    async def add(session_id:str,request:Request,owner_id=Depends(actor)):
        value=await body(request)
        def write():
            with engine.begin() as c:
                vid=controls.uuid(value['variation_id'])
                worksheets.snapshot(c,session_id,owner_id)
                receipt=c.execute(text('SELECT actor_id,before_version,result FROM purchasing_v4.worksheet_command WHERE session_id=:sid AND id=:id'),dict(sid=controls.uuid(session_id),id=controls.uuid(value['command_id']))).mappings().one_or_none()
                if receipt:
                    added=receipt['result'].get('added',[])
                    if receipt['actor_id']!=owner_id or receipt['before_version']!=value['expected_version'] or len(added)!=1 or added[0]['variation_id']!=vid:raise controls.Conflict('Add command identity reused')
                    return dict(receipt['result'],replayed=True)
                rows=worksheets.catalog_rows(c,[vid],source_namespace)
                if len(rows)!=1:raise controls.InvalidPolicy('Verified catalog identity required')
                row=rows[0];row.update(id=str(uuid5(UUID(controls.uuid(value['command_id'])),vid)),packs=[0]*4,touched=[True]*4,policy=['Manual entry']*4)
                return worksheets.patch(c,actor_id=owner_id,session_id=session_id,command_id=value['command_id'],expected_version=value['expected_version'],changes=[],add=[row])
        return await run_in_threadpool(write)

    @app.post('/api/worksheets/{session_id}/submission-preview',dependencies=[Depends(verify_csrf)])
    async def submission(session_id:str,request:Request,owner_id=Depends(actor)):
        value=await body(request)
        with engine.connect() as c:state=worksheets.snapshot(c,session_id,owner_id)
        if state['session']['version']!=value['expected_version']:raise controls.Conflict('Save and review the current worksheet first')
        selected=value['supplier_ids']
        if not isinstance(selected,list) or not selected or len(selected)!=len(set(selected)):raise controls.InvalidPolicy('Select vendors explicitly')
        result=[]
        for sid in selected:
            controls.uuid(sid);rows=[r for r in state['rows'] if r['supplier_id']==sid and not r['removed']]
            if not rows:raise controls.InvalidPolicy('Unknown selected vendor group')
            offer=rows[0]['offers'][sid];total=worksheets.totals(rows)
            result.append(dict(supplier_id=sid,name=offer['name'],workflow=offer['workflow'],totals=total,requires_review=bool(total['unknown_cost_rows'] or total['unresolved_cells'] or any(r['pack_size'] is None for r in rows))))
        return dict(preview_only=True,submitted=False,authorized=False,commitments_created=0,vendors=result)

    @app.post('/api/worksheets/discard-selected',dependencies=[Depends(verify_csrf)])
    async def discard_selected(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        def write():
            with engine.begin() as c:return worksheets.discard_selected(c,actor_id=owner_id,command_id=value['command_id'],selections=value['selections'])
        return await run_in_threadpool(write)

    @app.get('/api/worksheet-preferences')
    def preferences(owner_id=Depends(actor)):
        with engine.connect() as c:row=c.execute(text("SELECT version,value FROM purchasing_v4.worksheet_preference WHERE owner_id=:owner AND workspace='desktop'"),dict(owner=owner_id)).mappings().one_or_none()
        return dict(row) if row else dict(version=0,value={})

    @app.post('/api/worksheet-preferences',dependencies=[Depends(verify_csrf)])
    async def save_preferences(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        if not isinstance(value.get('value'),dict) or len(json.dumps(value['value']))>1000000:raise controls.InvalidPolicy('Bounded display preferences required')
        with engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(:owner,704214)'),{'owner':owner_id})
            current=c.execute(text("SELECT version FROM purchasing_v4.worksheet_preference WHERE owner_id=:owner AND workspace='desktop'"),dict(owner=owner_id)).scalar_one_or_none() or 0
            if current!=value['expected_version']:raise controls.Conflict('Display preferences changed in another window; local preferences retained')
            c.execute(text("INSERT INTO purchasing_v4.worksheet_preference(owner_id,workspace,version,value) VALUES (:owner,'desktop',:version,CAST(:value AS jsonb)) ON CONFLICT(owner_id,workspace) DO UPDATE SET version=EXCLUDED.version,value=EXCLUDED.value"),dict(owner=owner_id,version=current+1,value=json.dumps(value['value'])))
        return dict(version=current+1)
    return app


def main():
    p=argparse.ArgumentParser();p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--port',type=int,default=58441);args=p.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.isolated_url,source_namespace=args.source_namespace),host='127.0.0.1',port=args.port)

if __name__=='__main__':main()
