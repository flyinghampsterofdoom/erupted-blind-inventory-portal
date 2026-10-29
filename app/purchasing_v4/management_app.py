"""Explicit loopback-only management app. Never mounted by the production app."""
import argparse,json,secrets,time
from collections import defaultdict
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI,Request,Depends,HTTPException
from fastapi.responses import HTMLResponse,JSONResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from sqlalchemy import create_engine,select,text
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from app.security.csrf import install_csrf_cookie_middleware,verify_csrf
from app.security.headers import install_security_headers
from app.security.passwords import verify_password
from app.security.sessions import load_session_from_token,create_web_session
from app.models import Principal as PrincipalModel
from .isolation import require_isolated_url,require_isolated,readonly_snapshot
from . import controls
from .management_read import list_catalog,list_manual,list_legacy_manual,supplier_suggestions
from .policy_projection import calculate_history_check,save_history_check,use_automatic
from .control_descriptions import PARAMETERS
from .calculation_serialization import primitive

COOKIE='v4_isolated_session'
STATIC=Path(__file__).with_name('management_static')


def create_app(isolated_url,*,source_namespace):
    require_isolated_url(isolated_url)
    engine=create_engine(isolated_url,pool_size=4,max_overflow=2)
    with engine.connect() as c:
        require_isolated(c)
        if c.execute(text('SELECT version_num FROM purchasing_v4.alembic_version')).scalar_one()not in ('p3_0003','p4_0001','p4_0002','p4_0003','p5_0001','p6_0001','p7_0008','p8_0001','p8_0002','p8_0003','p8_0004','p8_0005','p8_0006','p8_0007','p9_0001','d1_0001'):raise RuntimeError('Run explicit isolated controls migration first')
    app=FastAPI(title='Purchasing V4 owner management — isolated',docs_url=None,redoc_url=None)
    app.state.engine=engine;app.state.source_namespace=source_namespace
    install_csrf_cookie_middleware(app);install_security_headers(app)
    app.mount('/assets',StaticFiles(directory=STATIC),name='v4-management-assets')
    attempts={}

    @app.exception_handler(HTTPException)
    async def http_error(request,exc):
        if exc.status_code==401 and request.url.path in ('/','/receiving'):return RedirectResponse('/login',status_code=303)
        return JSONResponse(dict(detail=exc.detail),status_code=exc.status_code,headers=exc.headers)

    @app.exception_handler(controls.Conflict)
    async def conflict(request,exc):return JSONResponse(dict(detail=str(exc),saved=False,conflict=True),status_code=409)
    @app.exception_handler(KeyError)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse(dict(detail=str(exc),saved=False),status_code=422)
    @app.exception_handler(PermissionError)
    async def forbidden(request,exc):return JSONResponse(dict(detail=str(exc)),status_code=403)
    @app.exception_handler(DBAPIError)
    async def database_error(request,exc):return JSONResponse(dict(detail='Database rejected the change. Nothing was saved; refresh the record and retry.',saved=False),status_code=409)

    def actor(request:Request):
        with Session(engine) as db:
            loaded=load_session_from_token(db,request.cookies.get(COOKIE),renew=False)
        if not loaded:raise HTTPException(401,'Sign in to this isolated application')
        principal=loaded[1]
        if not principal.active or principal.role.value!='ADMIN':raise HTTPException(403,'Owner access required')
        return principal.id

    app.state.actor=actor

    @app.get('/login',response_class=HTMLResponse)
    def login_page():return '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Isolated owner sign in</title><link rel="stylesheet" href="/assets/management.css"><main><h1>Owner sign in</h1><p>Purchasing V4 · isolated development</p><form id="login"><label>Username<input name="username" autocomplete="username" required></label><label>Password<input name="password" type="password" autocomplete="current-password" required></label><button>Sign in</button></form><p id="error" role="alert"></p><script src="/assets/login.js"></script></main></html>'''.replace('Owner sign in','Purchasing & receiving sign in' if getattr(app.state,'receiving_enabled',False) else 'Owner sign in')

    @app.post('/login',dependencies=[Depends(verify_csrf)])
    async def login(request:Request):
        body=await request.json();username=body.get('username','');password=body.get('password','')
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>255 or len(password)>1024:raise HTTPException(401,'Invalid sign in')
        # Match existing password/session checks, with bounded throttling and no login-triggered Square refresh.
        now=time.monotonic();key=(request.client.host,username.casefold())
        if len(attempts)>1000:
            for k in list(attempts):
                if attempts[k][0]<now-300:attempts.pop(k,None)
            if len(attempts)>1000:raise HTTPException(429,'Please retry later')
        start,count=attempts.get(key,(now,0))
        if now-start>300:start,count=now,0
        if count>=8:raise HTTPException(429,'Please retry later')
        attempts[key]=(start,count+1)
        with Session(engine) as db:
            p=db.execute(select(PrincipalModel).where(PrincipalModel.username==username)).scalar_one_or_none()
            if not p or not p.active or not verify_password(password,p.password_hash):raise HTTPException(401,'Invalid sign in')
            token=create_web_session(db,p.id,ip=request.client.host,user_agent=request.headers.get('user-agent'))
            loaded=load_session_from_token(db,token,renew=False)
            if not loaded or not loaded[1].active:raise HTTPException(403,'Active account required')
            receiving_only=loaded[1].role.value!='ADMIN'
            if receiving_only:
                allowed=getattr(app.state,'receiving_login_allowed',None)
                if not allowed or not allowed(p.id):raise HTTPException(403,'Explicit receiving access required')
            db.commit()
        attempts.pop(key,None)
        response=JSONResponse(dict(signed_in=True,next='/receiving' if receiving_only else '/'));response.set_cookie(COOKIE,token,httponly=True,samesite='strict',secure=False,max_age=3600,path='/')
        return response

    @app.post('/logout',dependencies=[Depends(verify_csrf)])
    def logout(request:Request,owner_id=Depends(actor)):
        from app.security.sessions import revoke_web_session
        with Session(engine) as db:revoke_web_session(db,request.cookies.get(COOKIE));db.commit()
        response=JSONResponse(dict(signed_out=True));response.delete_cookie(COOKIE);return response

    @app.get('/',response_class=HTMLResponse)
    def home(owner_id=Depends(actor)):return (STATIC/'index.html').read_text()

    @app.get('/api/context')
    def context(owner_id=Depends(actor)):
        with engine.connect() as c:
            stores=[dict(r) for r in c.execute(text('SELECT id,name FROM public.stores WHERE active ORDER BY id')).mappings()]
            categories=c.execute(text('SELECT DISTINCT jsonb_array_elements_text(categories) AS id FROM purchasing_v4.management_catalog ORDER BY id')).scalars().all()
        return dict(email_enabled=getattr(app.state,'email_enabled',False) and owner_id==getattr(app.state,'authorization_owner_id',None),receiving_enabled=getattr(app.state,'receiving_enabled',False),quotation_enabled=getattr(app.state,'quotation_enabled',False) and owner_id==getattr(app.state,'authorization_owner_id',None),stores=stores,categories=categories,parameters=PARAMETERS,recommendations_active=False,owner_id=owner_id,authorization_enabled=getattr(app.state,'authorization_enabled',False),submission_enabled=getattr(app.state,'submission_enabled',False) and owner_id==getattr(app.state,'authorization_owner_id',None))

    @app.get('/api/catalog')
    def catalog(search:str='',after:str='',limit:int=50,owner_id=Depends(actor)):
        with engine.connect() as c:return primitive(list_catalog(c,search=search,after=after,limit=limit))

    @app.get('/api/manual')
    def manual(search:str='',after:str='',store_id:int|None=None,state:str|None=None,limit:int=50,owner_id=Depends(actor)):
        with engine.connect() as c:return primitive(list_manual(c,search=search,after=after,store_id=store_id,state=state,limit=limit))

    @app.get('/api/legacy-manual')
    def legacy(search:str='',after:int=0,limit:int=50,owner_id=Depends(actor)):
        with engine.connect() as c:return primitive(list_legacy_manual(c,search=search,after=after,limit=limit,source_namespace=source_namespace))

    @app.get('/api/records')
    def records(kind:str,after:str='',limit:int=50,owner_id=Depends(actor)):
        if kind not in controls.KINDS or not 1<=limit<=100:raise HTTPException(422,'Bounded control list required')
        with engine.connect() as c:
            rows=c.execute(text('SELECT * FROM purchasing_v4.control_head WHERE kind=:kind AND record_key>:after ORDER BY record_key LIMIT :limit'),dict(kind=kind,after=after,limit=limit+1)).mappings().all()
            page=rows[:limit]
        return primitive(dict(rows=[dict(r) for r in page],next_cursor=page[-1]['record_key'] if len(rows)>limit else None))

    @app.get('/api/record')
    def record(kind:str,key:str,owner_id=Depends(actor)):
        if kind not in controls.KINDS:raise HTTPException(422,'Unknown control')
        with engine.connect() as c:return primitive(controls.heads(c,{(kind,key)}).get((kind,key)))

    @app.get('/api/suppliers')
    def suppliers(search:str='',after:str='',limit:int=50,owner_id=Depends(actor)):
        if not 1<=limit<=100 or len(search)>200:raise HTTPException(422,'Bounded supplier search required')
        with engine.connect() as c:
            rows=c.execute(text('''SELECT s.id::text,s.origin,h.version,h.payload,coalesce(h.payload->>'name',v.name,'Unconfigured internal supplier') AS name
             FROM purchasing_v4.authority_supplier s LEFT JOIN purchasing_v4.control_head h ON h.kind='SUPPLIER' AND h.record_key=s.id::text
             LEFT JOIN purchasing_v4.supplier_reference r ON r.supplier_id=s.foundation_supplier_id AND r.source_namespace=:ns
             LEFT JOIN public.vendors v ON v.id=r.legacy_vendor_id WHERE (:after='' OR s.id>CAST(NULLIF(:after,'') AS uuid))
             AND (:search='' OR coalesce(h.payload->>'name',v.name,'') ILIKE :like) ORDER BY s.id LIMIT :limit'''),
             dict(ns=source_namespace,after=after,search=search,like='%'+search+'%',limit=limit+1)).mappings().all()
            page=rows[:limit]
        return primitive(dict(rows=[dict(r) for r in page],next_cursor=page[-1]['id'] if len(rows)>limit else None))

    @app.get('/api/supplier-suggestions')
    def suggestions(after:str='',limit:int=50,owner_id=Depends(actor)):
        with engine.connect() as c:return primitive(supplier_suggestions(c,source_namespace=source_namespace,after=after,limit=limit))

    @app.get('/api/reviews')
    def reviews(category:str='',after:str='',limit:int=50,owner_id=Depends(actor)):
        if not 1<=limit<=100:raise HTTPException(422,'Bounded evidence page required')
        with engine.connect() as c:
            rows=c.execute(text('''SELECT e.*,h.version,h.payload AS review FROM purchasing_v4.review_evidence e LEFT JOIN purchasing_v4.control_head h
             ON h.kind='REVIEW' AND h.record_key=e.id WHERE e.id>:after AND (:category='' OR e.category=:category) ORDER BY e.id LIMIT :limit'''),dict(after=after,category=category,limit=limit+1)).mappings().all();page=rows[:limit]
        return primitive(dict(rows=[dict(r) for r in page],next_cursor=page[-1]['id'] if len(rows)>limit else None))

    @app.get('/api/audit')
    def audit(after_time:str|None=None,after_id:str|None=None,limit:int=50,owner_id=Depends(actor)):
        if not 1<=limit<=100:raise HTTPException(422,'Bounded audit page required')
        instant=datetime.fromisoformat(after_time) if after_time else datetime.max.replace(tzinfo=timezone.utc)
        identifier=controls.uuid(after_id) if after_id else 'ffffffff-ffff-ffff-ffff-ffffffffffff'
        with engine.connect() as c:rows=c.execute(text('''SELECT c.*,p.username FROM purchasing_v4.control_command c JOIN public.principals p ON p.id=c.actor_id
         WHERE (c.created_at,c.id)<(:instant,:id) ORDER BY c.created_at DESC,c.id DESC LIMIT :limit'''),dict(instant=instant,id=identifier,limit=limit+1)).mappings().all()
        page=[dict(r) for r in rows[:limit]]
        for item in page:
            item['result']=dict(item['result'],records=[{k:v for k,v in r.items() if k!='payload'} for r in item['result']['records']])
        return primitive(dict(rows=page,next_cursor=dict(after_time=page[-1]['created_at'],after_id=str(page[-1]['id'])) if len(rows)>limit else None))

    @app.get('/api/command-revisions')
    def revisions(command_id:str,owner_id=Depends(actor)):
        with engine.connect() as c:
            rows=c.execute(text('SELECT * FROM purchasing_v4.control_revision WHERE command_id=:id ORDER BY kind,record_key LIMIT 100'),{'id':controls.uuid(command_id)}).mappings().all()
        return primitive(dict(rows=[dict(r) for r in rows]))

    def write(operation,**values):
        with engine.begin() as c:return primitive(operation(c,**values))

    async def body(request):
        raw=await request.body()
        if len(raw)>131072:raise HTTPException(413,'Command too large')
        try:value=json.loads(raw)
        except ValueError:raise HTTPException(422,'Invalid command')
        if not isinstance(value,dict):raise HTTPException(422,'Object command required')
        return value

    @app.post('/api/commands',dependencies=[Depends(verify_csrf)])
    async def command(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        return await run_in_threadpool(write,controls.apply,actor_id=owner_id,command_id=value['command_id'],reason=value['reason'],changes=value['changes'])

    @app.post('/api/suppliers',dependencies=[Depends(verify_csrf)])
    async def new_supplier(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        return await run_in_threadpool(write,controls.create_supplier,actor_id=owner_id,command_id=value['command_id'],reason=value['reason'],supplier_id=value['supplier_id'],profile=value['profile'])

    @app.post('/api/restore',dependencies=[Depends(verify_csrf)])
    async def restore(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        return await run_in_threadpool(write,controls.restore,actor_id=owner_id,command_id=value['command_id'],reason=value['reason'],target_command=value['target_command'],expected=value['expected'])

    @app.post('/api/adopt-legacy-manual',dependencies=[Depends(verify_csrf)])
    async def adopt_legacy(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        return await run_in_threadpool(write,controls.adopt_legacy_manual,actor_id=owner_id,command_id=value['command_id'],reason=value['reason'],selections=value['selections'],source_namespace=source_namespace)

    @app.post('/api/check-history',dependencies=[Depends(verify_csrf)])
    async def check(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        as_of=datetime.fromisoformat(value['as_of']) if value.get('as_of') else datetime.now(timezone.utc)
        # Explicit study freshness input, never an installed automatic generation default.
        age=value.get('max_age_seconds')
        if age is None:
            with engine.connect() as c:
                saved=controls.live(controls.heads(c,{('GLOBAL','global')}).get(('GLOBAL','global')))
                age=saved.get('max_source_age_seconds') if saved else None
        if type(age) is not int or age<0:raise HTTPException(422,'Explicit source freshness seconds required')
        def perform_check():
            with readonly_snapshot(engine) as c:result=calculate_history_check(c,actor_id=owner_id,manual_keys=value['manual_keys'],as_of=as_of,max_age=timedelta(seconds=age),source_namespace=source_namespace)
            with engine.begin() as c:save_history_check(c,result)
            return result
        return await run_in_threadpool(perform_check)

    @app.post('/api/use-automatic',dependencies=[Depends(verify_csrf)])
    async def convert(request:Request,owner_id=Depends(actor)):
        value=await body(request)
        return await run_in_threadpool(write,use_automatic,actor_id=owner_id,command_id=value['command_id'],reason=value['reason'],check_id=value['check_id'],selected_keys=value['selected_keys'])
    return app


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--isolated-url',required=True);p.add_argument('--source-namespace',required=True);p.add_argument('--port',type=int,required=True)
    args=p.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.isolated_url,source_namespace=args.source_namespace),host='127.0.0.1',port=args.port)

if __name__=='__main__':main()
