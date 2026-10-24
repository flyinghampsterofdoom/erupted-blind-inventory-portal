"""Owner-only bounded projections; raw evidence is an explicit streamed export."""
from pathlib import Path
from fractions import Fraction
import threading

from sqlalchemy.exc import SQLAlchemyError
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.auth import get_current_principal
from app.db import get_db
from app.config import settings
from app.services.purchase_order_admin_service import authorize_purchasing_owner
from app.forecast_validation.storage import Store
from app.forecast_validation.contracts import MODELS, EvidenceError, canonical
from app.forecast_validation.operations import latest_alerts

router = APIRouter(prefix='/management/forecast-validation', tags=['Owner forecast evaluation'])
templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / 'templates'), autoescape=select_autoescape())

def fmt(value):
    return 'Unknown' if value is None else f'{float(Fraction(str(value))):,.2f}'

templates.filters['fv_number'] = fmt
templates.filters['fv_model'] = lambda value: dict(zip(MODELS, ('Raw 84-Day Sales', 'Current Production Algorithm',
                                                              'Conservative Adjustment', 'Verified Availability'))).get(value, value)
raw_slots = threading.BoundedSemaphore(1)
read_slots = threading.BoundedSemaphore(2)


def owner(db=Depends(get_db), principal=Depends(get_current_principal)):
    if not settings.forecast_validation_enabled:
        raise HTTPException(404)
    try:
        authorize_purchasing_owner(db, principal.id)
    except PermissionError:
        raise HTTPException(403, 'Verified Owner access required')
    # Release the business authorization transaction before evidence I/O.
    db.rollback()
    return principal


def evidence_store():
    return Store(settings.forecast_validation_database_url, business_url=settings.database_url,
                 production=settings.environment == 'production')


def raw_reader(actor=Depends(owner)):
    if not raw_slots.acquire(blocking=False):
        raise HTTPException(503, 'An evidence export is already active; retry after it completes', headers={'Retry-After': '5'})
    store = None
    acquired = False
    try:
        acquired = read_slots.acquire(timeout=5)
        if not acquired:
            raise HTTPException(503, 'Forecast readers are busy; retry shortly', headers={'Retry-After': '5'})
        store = evidence_store()
        yield store
    finally:
        if store:
            store.engine.dispose()
        if acquired:
            read_slots.release()
        raw_slots.release()


@router.get('', response_class=HTMLResponse)
def dashboard(request: Request, run_id: str | None = Query(None, max_length=64), sku: str | None = Query(None, max_length=255),
              evaluation_id: str | None = Query(None, max_length=64), runs_page: int = Query(0, ge=0, le=10000),
              sku_page: int = Query(0, ge=0, le=10000), revisions_page: int = Query(0, ge=0, le=10000),
              trends_page: int = Query(0, ge=0, le=10000),
              actor=Depends(owner)):
    if not settings.forecast_validation_database_url:
        return HTMLResponse('<h1>Forecast validation</h1><p>Shadow evidence storage is not configured. Collection is disabled.</p>',
                            headers={'Cache-Control': 'no-store'})
    if not read_slots.acquire(timeout=5):
        raise HTTPException(503, 'Forecast readers are busy; retry shortly', headers={'Retry-After': '5'})
    store = None
    try:
        store = evidence_store()
        runs = store.list_runs(limit=20, page=runs_page)
        run = store.get_run_meta(run_id or runs[0]['id']) if runs or run_id else None
        if run_id and run is None:
            raise HTTPException(404, 'Forecast not found')
        summary = store.get_run_summary(run['id']) if run else None
        selected = []
        revisions = []
        if run:
            revisions = store.list_evaluation_meta(run['id'], limit=10, page=revisions_page)
            if evaluation_id:
                item = store.get_evaluation_meta(evaluation_id)
                if item is None or item['run_id'] != run['id']:
                    raise HTTPException(404, 'Evaluation is not part of this origin')
                import json
                item['summary'] = json.loads(item['summary'])
                selected = [item]
            else:
                for horizon in (28, 56):
                    selected.extend(store.list_evaluation_meta(run['id'], horizon=horizon, limit=1))
        performance = [{**metric, 'horizon': item['horizon'], 'revision': item['revision']}
                       for item in selected for metric in item['summary']['metrics']]
        investigations = store.sku_rows(run['id'], sku, limit=8, page=sku_page) if run and sku else []
        for item in investigations:
            key = item['input']['variation_id'] + ':' + str(item['input']['store_id'])
            item['outcomes'] = [
                {'horizon': value['horizon'], 'revision': value['revision'],
                 'detail': store.get_view('evaluation', value['id'], 'evaluation-row', key)}
                for value in selected]
        body = templates.get_template('forecast_validation.html').render(
            runs=runs, run=run, summary=summary, performance=performance, evaluations=selected,
            revisions=revisions, investigations=investigations, sku=sku or '',
            trends=store.trends(limit=20, page=trends_page), failures=store.failures(), alerts=latest_alerts(store),
            models=MODELS, runs_page=runs_page, sku_page=sku_page, revisions_page=revisions_page, trends_page=trends_page)
        return HTMLResponse(body, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex'})
    except EvidenceError as exc:
        raise HTTPException(503, str(exc))
    except SQLAlchemyError:
        raise HTTPException(503, 'Shadow evidence is unavailable; purchasing is unaffected')
    finally:
        if store:
            store.engine.dispose()
        read_slots.release()


def _record_json(store, kind, meta):
    # Only small metadata is serialized here. The logical payload is verified and
    # streamed in <=64KiB pieces, never decoded by the HTTP path.
    yield canonical({k: v for k, v in meta.items() if k != 'payload'})[:-1].encode()
    yield b',"payload":'
    yield from store.stream_record(kind, meta['id'])
    yield b'}'


@router.get('/evidence/{run_id}')
def evidence(run_id: str, page: int = Query(0, ge=0, le=10000), store=Depends(raw_reader)):
    meta = store.get_run_meta(run_id)
    if not meta:
        raise HTTPException(404, 'Forecast not found')
    evaluations = store.list_evaluation_meta(run_id, limit=2, page=page)
    def stream():
        yield b'{"run":'
        yield from _record_json(store, 'run', meta)
        yield b',"evaluations":['
        for index, value in enumerate(evaluations):
            if index:
                yield b','
            # Preserve the historical raw-record summary representation.
            raw_meta = store.get_evaluation_meta(value['id'])
            yield from _record_json(store, 'evaluation', raw_meta)
        yield b'],"pagination":' + canonical({'page': page, 'limit': 2,
                  'next_page': page + 1 if len(evaluations) == 2 else None}).encode() + b'}'
    return StreamingResponse(stream(), media_type='application/json',
                             headers={'Cache-Control': 'no-store', 'X-Evidence-SHA256': meta['sha256'],
                                      'X-Robots-Tag': 'noindex'})


@router.get('/evaluation-evidence/{evaluation_id}')
def evaluation_evidence(evaluation_id: str, store=Depends(raw_reader)):
    meta = store.get_evaluation_meta(evaluation_id)
    if not meta:
        raise HTTPException(404, 'Evaluation not found')
    return StreamingResponse(_record_json(store, 'evaluation', meta), media_type='application/json',
                             headers={'Cache-Control': 'no-store', 'X-Evidence-SHA256': meta['sha256'],
                                      'X-Robots-Tag': 'noindex'})


@router.get('/runtime')
def runtime_observation(request: Request):
    key = settings.forecast_validation_monitor_public_key
    if not key:
        raise HTTPException(404)
    from app.forecast_validation.runtime_probe import authorize_probe, measure
    try:
        authorize_probe(key, request.headers.get('X-FV-Timestamp'), request.headers.get('X-FV-Signature'))
    except EvidenceError:
        raise HTTPException(403, 'Verified runtime observer required')
    if not read_slots.acquire(blocking=False):
        raise HTTPException(503, 'Runtime observer busy')
    try:
        return JSONResponse(measure(), headers={'Cache-Control':'no-store','X-Robots-Tag':'noindex'})
    except (EvidenceError, OSError, ValueError):
        raise HTTPException(503, 'Runtime telemetry unavailable')
    finally:
        read_slots.release()
