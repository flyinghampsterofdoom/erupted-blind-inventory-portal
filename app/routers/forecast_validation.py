"""Owner-only reads of frozen shadow evidence. No purchasing integration on page loads."""
from pathlib import Path
from fractions import Fraction
from sqlalchemy.exc import SQLAlchemyError
from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import HTMLResponse,JSONResponse
from jinja2 import Environment,FileSystemLoader,select_autoescape
from app.auth import get_current_principal
from app.db import get_db
from app.config import settings
from app.services.purchase_order_admin_service import authorize_purchasing_owner
from app.forecast_validation.storage import Store
from app.forecast_validation.contracts import MODELS,EvidenceError

router=APIRouter(prefix='/management/forecast-validation',tags=['Owner forecast evaluation'])
templates=Environment(loader=FileSystemLoader(Path(__file__).parents[1]/'templates'),autoescape=select_autoescape())
def fmt(value):return 'Unknown' if value is None else f'{float(Fraction(str(value))):,.2f}'
templates.filters['fv_number']=fmt
templates.filters['fv_model']=lambda value:dict(zip(MODELS,('Raw 84-Day Sales','Current Production Algorithm','Conservative Adjustment','Verified Availability'))).get(value,value)

def owner(db=Depends(get_db),principal=Depends(get_current_principal)):
    if not settings.forecast_validation_enabled:raise HTTPException(404)
    try:authorize_purchasing_owner(db,principal.id)
    except PermissionError:raise HTTPException(403,'Verified Owner access required')
    return principal

def evidence_store():return Store(settings.forecast_validation_database_url,business_url=settings.database_url,production=settings.environment=='production')

@router.get('',response_class=HTMLResponse)
def dashboard(request:Request,run_id:str|None=None,sku:str|None=None,actor=Depends(owner)):
    if not settings.forecast_validation_database_url:return HTMLResponse('<h1>Forecast validation</h1><p>Shadow evidence storage is not configured. Collection is disabled.</p>',headers={'Cache-Control':'no-store'})
    store=evidence_store()
    try:
        runs=store.list_runs();run=store.get_run(run_id or runs[0]['id']) if runs or run_id else None
        if run_id and run is None:raise HTTPException(404,'Forecast not found')
        evaluations=store.list_evaluations(run['id']) if run else [];payload=run['payload'] if run else None
        # Both horizons/revisions remain distinct; never pool overlapping outcomes.
        performance=[]
        for e in evaluations:
            performance.extend({**m,'horizon':e['horizon'],'revision':e['revision']} for m in e['payload']['metrics'] if m['dimension']=='class' and m['value']==m['cohort'])
        exposure=[]
        if payload:
            for model in MODELS:
                quantities=[p['models'][model] for p in payload['purchases']]
                known=[p for p in quantities if p['cost'] is not None]
                exposure.append({'model':model,'cost':sum((Fraction(p['cost']) for p in known),Fraction(0)),'missing':len(quantities)-len(known),'abstentions':sum(f['models'][model]['rate'] is None for f in payload['forecasts'])})
        investigations=[]
        if payload and sku:
            investigations=[{'input':r,'forecast':f,'outcomes':[{'horizon':e['horizon'],'revision':e['revision'],'evidence':next((o for o in e['payload']['outcome']['rows'] if o['store_id']==r['store_id'] and o['variation_id']==r['variation_id']),None)} for e in evaluations]} for r,f in zip(payload['snapshot']['rows'],payload['forecasts']) if sku in (r['sku'],r['variation_id'])]
        control=None;impact=None
        if payload:
            known=[p['current_without_stockouts']['cost'] for p in payload['purchases']]
            control=sum((Fraction(q) for q in known if q is not None),Fraction(0)) if all(q is not None for q in known) else None
        if control is not None and exposure and exposure[1]['missing']==0:impact=exposure[1]['cost']-control
        reliability={state:sum(r['verified_availability'].count(state) for r in payload['snapshot']['rows']) for state in ('IN_STOCK','STOCKOUT','UNKNOWN','PARTIAL')} if payload else {}
        html=templates.get_template('forecast_validation.html').render(runs=runs,run=run,payload=payload,evaluations=evaluations,performance=performance,exposure=exposure,control=control,impact=impact,reliability=reliability,trends=store.trends(),investigations=investigations,sku=sku or '',failures=store.failures(),models=MODELS)
        return HTMLResponse(html,headers={'Cache-Control':'no-store','X-Robots-Tag':'noindex'})
    except EvidenceError as exc:raise HTTPException(503,str(exc))
    except SQLAlchemyError:raise HTTPException(503,'Shadow evidence is unavailable; purchasing is unaffected')
    finally:store.engine.dispose()

@router.get('/evidence/{run_id}')
def evidence(run_id:str,actor=Depends(owner)):
    store=evidence_store()
    try:
        run=store.get_run(run_id)
        if run is None:raise HTTPException(404)
        return JSONResponse({'run':run,'evaluations':store.list_evaluations(run_id)},headers={'Cache-Control':'no-store'})
    finally:store.engine.dispose()
