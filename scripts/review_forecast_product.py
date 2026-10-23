"""Loopback-only local review shell with synthetic accounts; never used by production.

Runs the real forecasting router with its real Owner authorization. A tiny in-memory
business-auth database adapter supplies only synthetic identities. No business DB,
Square client, source refresh, collector, scheduler or write endpoint is mounted.
"""
import argparse
from types import SimpleNamespace
import secrets
import hmac
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse
from app.auth import get_current_principal
from app.db import get_db
from app.config import settings
from app.routers.forecast_product import router

class ReviewDB:
    def execute(self,*args,**kwargs):
        # Owner authorization queries the configured Owner principal ID only.
        actor=next(iter(args[0].compile().params.values()))
        return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id=actor,active=True))
    def rollback(self):pass

def create_review_app(path,password):
    app=FastAPI(title='Erupted local forecasting review')
    sessions={}
    settings.forecast_validation_enabled=True
    settings.forecast_product_projection_path=str(path)
    settings.purchasing_owner_principal_id=6
    def principal(request:Request):
        actor=sessions.get(request.cookies.get('local_forecast_review'))
        if not actor:raise HTTPException(401,'Sign in through /local-review')
        return SimpleNamespace(id=actor)
    app.dependency_overrides[get_current_principal]=principal
    app.dependency_overrides[get_db]=lambda:ReviewDB()
    app.include_router(router)
    @app.get('/local-review',response_class=HTMLResponse)
    def login():
        return '<title>Local review sign-in</title><h1>Erupted local forecasting review</h1><p>Synthetic local accounts. No connection to production.</p><form method="post"><input name="password" type="password" placeholder="Local review password" required><select name="account"><option value="6">Owner</option><option value="7">Store reviewer (denied)</option></select><button>Sign in</button></form>'
    @app.post('/local-review')
    async def signin(request:Request):
        if request.headers.get('origin') and request.headers['origin']!=str(request.base_url).rstrip('/'):raise HTTPException(403)
        f=await request.form()
        if not hmac.compare_digest(str(f.get('password','')),password):raise HTTPException(403)
        actor=int(f.get('account',0))
        if actor not in (6,7):raise HTTPException(403)
        token=secrets.token_urlsafe(32);sessions[token]=actor
        response=RedirectResponse('/management/forecasting',status_code=303)
        response.set_cookie('local_forecast_review',token,httponly=True,samesite='strict')
        return response
    return app

if __name__=='__main__':
    import uvicorn
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--projection',required=True);parser.add_argument('--password-file',required=True);parser.add_argument('--port',type=int,default=8766);args=parser.parse_args()
    from pathlib import Path
    uvicorn.run(create_review_app(args.projection,Path(args.password_file).read_text().strip()),host='127.0.0.1',port=args.port,access_log=False)
