"""Owner-only advisory product. Every HTTP operation reads a prepared projection."""
import threading
import sqlite3
import json
from pathlib import Path
from urllib.parse import urlencode
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from .forecast_validation import owner, templates
from app.config import settings
from app.forecast_validation.product_release import verify_file
from app.forecast_validation.product_diagnostics import PRIORITY_LABELS
from app.forecast_validation.product import ProductView, MODELS, LABELS, HORIZONS

router=APIRouter(prefix='/management/forecasting',tags=['Owner forecasting'])
templates.globals['dict']=dict
templates.filters['evidence_gaps']=lambda raw: ', '.join({'on_hand':'observed stock','incoming':'incoming commitments','pack':'supplier pack','sales_coverage':'sales completeness','demand':'supported demand','cost':'unit cost','lead_time':'supplier lead time'}.get(x,x) for x in json.loads(raw))
templates.globals['priority_labels']=PRIORITY_LABELS
_REASON_TEXT={'CONFIRMED_STOCKOUT_ADJUSTMENT':'Adjusted for stockouts under the existing purchasing rules.', 'EXPLICIT_RAW84_FALLBACK':'Too little verified exposure; uses observed 84-day sales.', 'INSUFFICIENT_VERIFIED_EXPOSURE':'Abstains: fewer than 14 verified in-stock days.', 'FULL_DAY_EXPOSURE_PROXY':'Uses verified full-day availability, not verified selling hours.', 'NO_RECENT_OBSERVED_SALES':'No sales observed in the recent window; demand may be censored.', 'DEMAND_CENSORED_OR_NEW':'Too little evidence of demand while stock was available.'}
templates.filters['forecast_reasons']=lambda reasons: ' '.join(_REASON_TEXT.get(x,x.replace('_',' ').capitalize()) for x in reasons)
templates.filters['rate_times']=lambda rate,h: float(__import__('fractions').Fraction(rate)*h)
templates.filters['quantity']=lambda x: 'Unknown' if x is None else f'{float(x):,.1f}'
templates.filters['money']=lambda x: 'Unknown' if x is None else f'${float(x):,.2f}'
templates.globals['urlencode']=urlencode

slots=threading.BoundedSemaphore(2)

def view():
    path=settings.forecast_product_projection_path
    if not path or not Path(path).is_file():raise HTTPException(503,'No prepared forecasting projection. Prepare frozen evidence offline.')
    if not slots.acquire(blocking=False):raise HTTPException(503,'Forecast readers are busy; retry shortly',headers={'Retry-After':'1'})
    v=None
    try:
        verify_file(path,settings.forecast_product_projection_sha256,required=settings.environment=='production')
        v=ProductView(path)
        yield v
    except (sqlite3.Error,ValueError,OSError):
        raise HTTPException(503,'Prepared forecast view is unavailable or failed verification; business operations are unaffected.')
    finally:
        if v:v.close()
        slots.release()

def filters(model: str='B_CURRENT',variation: str=Query('',max_length=64),store: str=Query('',max_length=20),category: str=Query('',max_length=500),vendor: str=Query('',max_length=255),q: str=Query('',max_length=120),attention: str='',horizon: int=28,page: int=Query(0,ge=0,le=10000)):
    if model not in MODELS or horizon not in HORIZONS or attention not in ('','risk','reorder','excess','slow','abstain','near_term','evidence'):raise HTTPException(422,'Invalid forecasting filter')
    return dict(model=model,variation=variation,store=store,category=category,vendor=vendor,q=q,attention=attention,horizon=horizon,page=page)

def render(name,**context):
    return HTMLResponse(templates.get_template(name).render(**context,labels=LABELS,horizons=HORIZONS),headers={'Cache-Control':'no-store','X-Forecast-Mode':'frozen-advisory'})

@router.get('',response_class=HTMLResponse)
def overview(request:Request,actor=Depends(owner),f=Depends(filters),v=Depends(view)):
    summary,rows=v.listing(**{k:x for k,x in f.items() if k!='horizon'})
    return render('forecast_product.html',meta=v.meta(),facets=v.facets(),summary=summary,rows=rows,f=f,request=request)

@router.get('/product/{store}/{variation}',response_class=HTMLResponse)
def detail(store:str,variation:str,model:str='B_CURRENT',actor=Depends(owner),v=Depends(view)):
    if model not in MODELS:raise HTTPException(422)
    row=v.detail(store,variation,model)
    if row is None:raise HTTPException(404,'Store/SKU is not supported by this snapshot')
    sales=[float(x) for x in row['detail']['facts']['sales']]; peak=max(sales+[1])
    points=' '.join(f'{i*720/83:.1f},{130-x/peak*110:.1f}' for i,x in enumerate(sales))
    return render('forecast_product_detail.html',meta=v.meta(),row=row,points=points,peak=peak,model=model)

@router.get('/purchasing-list',response_class=HTMLResponse)
def purchasing_list(actor=Depends(owner),f=Depends(filters),v=Depends(view)):
    # Attention is intentionally ignored: an abstaining store cannot disappear from a pack aggregate.
    rows=v.purchases(**f)
    known=[r for r in rows if r['quantity'] is not None and r['quantity']>0]
    total=sum(float(__import__('fractions').Fraction(r['cost'])) for r in known if r['cost'] is not None)
    return render('forecast_product_purchases.html',meta=v.meta(),rows=rows,f=f,total=total,unknown=sum(r['cost'] is None for r in known),blocked=sum(r['quantity'] is None for r in rows))
