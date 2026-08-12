from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from math import ceil
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import Store, Vendor
from app.security.csrf import verify_csrf
from app.services.v2_daily_store_log_service import portal_today
from app.services.v2_reporting_workbench_service import (
    DATE_MODES,
    REPORT_TYPES,
    delete_saved_view,
    get_saved_view,
    list_saved_views,
    parse_search_terms,
    resolve_relative_dates,
    run_sales_analysis,
    run_stock_value,
    save_view,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event
from app.v2.navigation import build_navigation

router = APIRouter(prefix='/v2/reports', tags=['v2-reporting'])
reporting_access = require_capability('reports.workbench.view', Role.ADMIN, Role.MANAGER)
PAGE_SIZES = (25, 50, 150)
DEFAULT_PAGE_SIZE = 50


class Page:
    slug = 'reports'
    label = 'Reporting Workbench'
    description = 'Build transparent sales and inventory reports from one workspace.'
    badge = 'Owner Preview'


@dataclass(frozen=True)
class ReportPagination:
    page: int
    page_size: int
    total_rows: int
    total_pages: int
    first_row: int
    last_row: int
    previous_page: int | None
    next_page: int | None


def _page_size(value) -> int:
    try:
        clean = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return clean if clean in PAGE_SIZES else DEFAULT_PAGE_SIZE


def _page_number(value) -> int:
    try:
        clean = int(value)
    except (TypeError, ValueError):
        return 1
    return max(clean, 1)


def _paginate_result(result, *, page: int, page_size: int):
    total_rows = len(result.rows)
    total_pages = max(ceil(total_rows / page_size), 1)
    current_page = min(max(page, 1), total_pages)
    start = (current_page - 1) * page_size
    end = min(start + page_size, total_rows)
    pagination = ReportPagination(
        page=current_page, page_size=page_size, total_rows=total_rows,
        total_pages=total_pages, first_row=(start + 1 if total_rows else 0), last_row=end,
        previous_page=(current_page - 1 if current_page > 1 else None),
        next_page=(current_page + 1 if current_page < total_pages else None),
    )
    return replace(result, rows=result.rows[start:end]), pagination


def _default_config() -> dict:
    today = portal_today()
    return {
        'report_type': 'sales_analysis',
        'start_date': (today - timedelta(days=29)).isoformat(),
        'end_date': today.isoformat(),
        'date_mode': 'custom',
        'store_ids': [],
        'include_terms': [],
        'exclude_terms': [],
        'match_mode': 'any',
        'grouping': 'product',
        'sort': 'net_sales_desc',
        'vendor': '',
        'lifecycle': '',
        'metrics': ['units_sold', 'gross_sales', 'discounts', 'net_sales', 'cogs', 'gross_profit', 'gross_margin'],
        'page_size': DEFAULT_PAGE_SIZE,
    }


def _clean_ints(values) -> list[int]:
    output: list[int] = []
    for value in values:
        try:
            clean = int(value)
        except (TypeError, ValueError):
            continue
        if clean > 0 and clean not in output:
            output.append(clean)
    return output


def _form_config(form) -> dict:
    report_type = str(form.get('report_type') or 'sales_analysis')
    if report_type not in REPORT_TYPES:
        raise ValueError('Unknown report type.')
    date_mode = str(form.get('date_mode') or 'custom')
    if date_mode not in DATE_MODES:
        raise ValueError('Unknown date mode.')
    return {
        'report_type': report_type,
        'start_date': str(form.get('start_date') or ''),
        'end_date': str(form.get('end_date') or ''),
        'date_mode': date_mode,
        'store_ids': _clean_ints(form.getlist('store_id')),
        'include_terms': parse_search_terms(form.getlist('include_term') + [str(form.get('include_search') or '')]),
        'exclude_terms': parse_search_terms(form.getlist('exclude_term') + [str(form.get('exclude_search') or '')]),
        'match_mode': str(form.get('match_mode') or 'any'),
        'grouping': str(form.get('grouping') or ('variation' if report_type == 'stock_value' else 'product')),
        'sort': str(form.get('sort') or ('inventory_value_desc' if report_type == 'stock_value' else 'net_sales_desc')),
        'vendor': str(form.get('vendor') or '').strip(),
        'lifecycle': str(form.get('lifecycle') or '').strip(),
        'metrics': list(dict.fromkeys(str(value) for value in form.getlist('metric') if str(value))),
        'page_size': _page_size(form.get('page_size')),
    }


def _authorized_store_context(db: Session, config: dict) -> tuple[list[dict], list[int]]:
    rows = db.execute(select(Store.id, Store.name).where(Store.active.is_(True)).order_by(Store.name, Store.id)).all()
    stores = [{'id': int(row.id), 'name': str(row.name)} for row in rows]
    allowed = {row['id'] for row in stores}
    selected = [value for value in _clean_ints(config.get('store_ids', [])) if value in allowed]
    config['store_ids'] = selected
    return stores, selected


def _dates(config: dict) -> tuple[date, date]:
    relative = resolve_relative_dates(str(config.get('date_mode') or 'custom'), today=portal_today())
    if relative:
        config['start_date'], config['end_date'] = relative[0].isoformat(), relative[1].isoformat()
        return relative
    try:
        start = date.fromisoformat(str(config.get('start_date') or ''))
        end = date.fromisoformat(str(config.get('end_date') or ''))
    except ValueError as exc:
        raise ValueError('Choose a valid start and end date.') from exc
    if end < start:
        raise ValueError('End date must be on or after start date.')
    return start, end


def _criteria(config: dict, stores: list[dict]) -> dict:
    selected = set(config.get('store_ids', []))
    return {
        'include_terms': config.get('include_terms', []),
        'exclude_terms': config.get('exclude_terms', []),
        'stores': [row['name'] for row in stores if row['id'] in selected] or ['All Stores'],
        'date': (
            f"{config.get('start_date')} through {config.get('end_date')}"
            if config.get('report_type') == 'sales_analysis' else 'Current valuation only'
        ),
    }


def _context(
    request: Request, principal: Principal, db: Session, *, config: dict | None = None,
    result=None, pagination: ReportPagination | None = None, error: str = '', message: str = '',
    selected_view_id: int | None = None,
) -> dict:
    active_config = {**_default_config(), **(config or {})}
    active_config['include_terms'] = parse_search_terms(active_config.get('include_terms', []))
    active_config['exclude_terms'] = parse_search_terms(active_config.get('exclude_terms', []))
    active_config['page_size'] = _page_size(active_config.get('page_size'))
    if result is not None and pagination is None:
        result, pagination = _paginate_result(
            result, page=1, page_size=active_config['page_size'],
        )
    stores, selected = _authorized_store_context(db, active_config)
    vendors = [str(value) for value in db.scalars(select(Vendor.name).where(Vendor.active.is_(True)).order_by(Vendor.name)).all()]
    return {
        'request': request, 'principal': principal, 'page': Page(), 'navigation': build_navigation(request),
        'stores': stores, 'selected_store_ids': selected, 'all_stores_selected': not selected,
        'store_scope_label': 'All Stores' if not selected else f'{len(selected)} selected', 'scope_locked': True,
        'config': active_config, 'result': result, 'pagination': pagination,
        'page_sizes': PAGE_SIZES, 'criteria': _criteria(active_config, stores),
        'saved_views': list_saved_views(db, principal_id=principal.id), 'selected_view_id': selected_view_id,
        'vendors': vendors, 'error': error, 'message': message,
    }


@router.get('')
def reporting_page(
    request: Request, principal: Principal = Depends(reporting_access), db: Session = Depends(get_db),
):
    config = None
    selected_view_id = None
    raw_id = request.query_params.get('saved_view_id')
    if raw_id:
        try:
            selected_view_id = int(raw_id)
            view = get_saved_view(db, principal_id=principal.id, view_id=selected_view_id)
            config = {'report_type': view.report_type, **dict(view.configuration or {})}
            relative = resolve_relative_dates(str(config.get('date_mode') or 'custom'), today=portal_today())
            if relative:
                config['start_date'], config['end_date'] = relative[0].isoformat(), relative[1].isoformat()
        except (ValueError, LookupError):
            raise HTTPException(status_code=404, detail='Saved View not found.') from None
    return request.app.state.templates.TemplateResponse(
        'v2/reporting/workbench.html',
        _context(request, principal, db, config=config, selected_view_id=selected_view_id),
    )


@router.post('/run')
async def run_report_route(
    request: Request, principal: Principal = Depends(reporting_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        config = _form_config(form)
        stores, _ = _authorized_store_context(db, config)
        if config['report_type'] == 'sales_analysis':
            start, end = _dates(config)
            result = run_sales_analysis(
                db, start_date=start, end_date=end, store_ids=config['store_ids'],
                include_terms=config['include_terms'], exclude_terms=config['exclude_terms'],
                match_mode=config['match_mode'], grouping=config['grouping'], sort=config['sort'],
            )
        else:
            result = run_stock_value(
                db, store_ids=config['store_ids'], include_terms=config['include_terms'],
                exclude_terms=config['exclude_terms'], match_mode=config['match_mode'],
                grouping=config['grouping'], vendor=config['vendor'], lifecycle=config['lifecycle'], sort=config['sort'],
            )
        result, pagination = _paginate_result(
            result, page=_page_number(form.get('page')), page_size=config['page_size'],
        )
        context = _context(
            request, principal, db, config=config, result=result, pagination=pagination,
            selected_view_id=(int(form.get('saved_view_id')) if str(form.get('saved_view_id') or '').isdigit() else None),
        )
        context['criteria'] = _criteria(config, stores)
        return request.app.state.templates.TemplateResponse('v2/reporting/workbench.html', context)
    except (ValueError, RuntimeError) as exc:
        return request.app.state.templates.TemplateResponse(
            'v2/reporting/workbench.html',
            _context(request, principal, db, config=locals().get('config'), error=str(exc)),
            status_code=422,
        )


def _saved_configuration(config: dict) -> dict:
    output = dict(config)
    if output.get('date_mode') == 'choose_when_run':
        output['start_date'] = ''
        output['end_date'] = ''
    return output


@router.post('/saved-views')
async def create_saved_view_route(
    request: Request, principal: Principal = Depends(reporting_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        config = _form_config(form)
        _authorized_store_context(db, config)
        row = save_view(
            db, principal_id=principal.id, name=str(form.get('saved_view_name') or ''),
            report_type=config['report_type'], configuration=_saved_configuration(config),
        )
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='SAVED_VIEW_CREATED', domain='REPORTING',
            entity_type='reporting_saved_view', entity_id=row.id, after={'name': row.name, 'report_type': row.report_type},
        ), ip=get_client_ip(request))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return request.app.state.templates.TemplateResponse(
            'v2/reporting/workbench.html', _context(request, principal, db, config=locals().get('config'), error=str(exc)),
            status_code=422,
        )
    return RedirectResponse(f'/v2/reports?saved_view_id={row.id}&message={quote("Saved View created.")}', status_code=303)


@router.post('/saved-views/{view_id}')
async def update_saved_view_route(
    view_id: int, request: Request, principal: Principal = Depends(reporting_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        existing = get_saved_view(db, principal_id=principal.id, view_id=view_id)
        config = _form_config(form)
        _authorized_store_context(db, config)
        row = save_view(
            db, principal_id=principal.id, view_id=view_id,
            name=str(form.get('saved_view_name') or existing.name), report_type=config['report_type'],
            configuration=_saved_configuration(config),
        )
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='SAVED_VIEW_UPDATED', domain='REPORTING',
            entity_type='reporting_saved_view', entity_id=row.id, after={'name': row.name, 'report_type': row.report_type},
        ), ip=get_client_ip(request))
        db.commit()
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail='Saved View not found.') from None
    except ValueError as exc:
        db.rollback()
        return request.app.state.templates.TemplateResponse(
            'v2/reporting/workbench.html', _context(request, principal, db, config=locals().get('config'), error=str(exc)),
            status_code=422,
        )
    return RedirectResponse(f'/v2/reports?saved_view_id={row.id}&message={quote("Saved View updated.")}', status_code=303)


@router.post('/saved-views/{view_id}/delete')
async def delete_saved_view_route(
    view_id: int, request: Request, principal: Principal = Depends(reporting_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    try:
        delete_saved_view(db, principal_id=principal.id, view_id=view_id)
        write_v2_audit_event(db, event=V2AuditEvent(
            actor_principal_id=principal.id, action='SAVED_VIEW_DELETED', domain='REPORTING',
            entity_type='reporting_saved_view', entity_id=view_id,
        ), ip=get_client_ip(request))
        db.commit()
    except LookupError:
        db.rollback()
        raise HTTPException(status_code=404, detail='Saved View not found.') from None
    return RedirectResponse('/v2/reports?message=Saved%20View%20deleted.', status_code=303)
