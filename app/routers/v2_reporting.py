from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from secrets import token_urlsafe
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import PurchaseOrder, Store, Vendor
from app.security.csrf import verify_csrf
from app.services.v2_daily_store_log_service import portal_today
from app.services.v2_replenishment_report_service import (
    build_replenishment_preview,
    create_replenishment_purchase_order,
    run_replenishment_report,
)
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
        'exclude_over_four_weeks': True,
        'exclude_no_recent_sales': True,
        'manual_exclusions': [],
        'po_vendor_id': '',
        'po_mode': 'replace_sales',
        'target_weeks': '4',
        'po_finalize_key': '',
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
        'exclude_over_four_weeks': str(form.get('exclude_over_four_weeks') or '') == '1',
        'exclude_no_recent_sales': str(form.get('exclude_no_recent_sales') or '') == '1',
        'manual_exclusions': list(dict.fromkeys(
            str(value) for value in form.getlist('manual_exclusion') if str(value)
        )),
        'po_vendor_id': str(form.get('po_vendor_id') or '').strip(),
        'po_mode': str(form.get('po_mode') or 'replace_sales').strip(),
        'target_weeks': str(form.get('target_weeks') or '4').strip(),
        'po_finalize_key': str(form.get('po_finalize_key') or '').strip(),
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
        'vendor': config.get('vendor') or 'All vendors',
        'lifecycle': (config.get('lifecycle') or 'All lifecycle states').replace('_', ' ').title(),
        'date': (
            f"{config.get('start_date')} through {config.get('end_date')}"
            if config.get('report_type') in {'sales_analysis', 'replenishment'}
            else 'Current valuation only'
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
    if (
        result is not None
        and getattr(result, 'report_type', '') != 'replenishment'
        and pagination is None
    ):
        result, pagination = _paginate_result(
            result, page=1, page_size=active_config['page_size'],
        )
    stores, selected = _authorized_store_context(db, active_config)
    vendor_rows = db.execute(
        select(Vendor.id, Vendor.name)
        .where(Vendor.active.is_(True))
        .order_by(Vendor.name)
    ).all()
    vendors = [str(row.name) for row in vendor_rows]
    if 'Unknown / Unassigned' not in vendors:
        vendors.append('Unknown / Unassigned')
    return {
        'request': request, 'principal': principal, 'page': Page(), 'navigation': build_navigation(request),
        'stores': stores, 'selected_store_ids': selected, 'all_stores_selected': not selected,
        'store_scope_label': 'All Stores' if not selected else f'{len(selected)} selected', 'scope_locked': True,
        'config': active_config, 'result': result, 'pagination': pagination,
        'page_sizes': PAGE_SIZES, 'criteria': _criteria(active_config, stores),
        'saved_views': list_saved_views(db, principal_id=principal.id), 'selected_view_id': selected_view_id,
        'vendors': vendors,
        'replenishment_vendors': [
            {'id': int(row.id), 'name': str(row.name)} for row in vendor_rows
        ],
        'error': error, 'message': message,
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
        elif config['report_type'] == 'stock_value':
            result = run_stock_value(
                db, store_ids=config['store_ids'], include_terms=config['include_terms'],
                exclude_terms=config['exclude_terms'], match_mode=config['match_mode'],
                grouping=config['grouping'], vendor=config['vendor'], lifecycle=config['lifecycle'], sort=config['sort'],
            )
        else:
            start, end = _dates(config)
            result = run_replenishment_report(
                db, start_date=start, end_date=end, store_ids=config['store_ids'],
                exclude_over_four_weeks=config['exclude_over_four_weeks'],
                exclude_no_recent_sales=config['exclude_no_recent_sales'],
                manual_exclusions=config['manual_exclusions'], as_of=portal_today(),
            )
        pagination = None
        if config['report_type'] != 'replenishment':
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


def _replenishment_from_form(db: Session, form):
    config = _form_config(form)
    if config['report_type'] != 'replenishment':
        raise ValueError('Run the Replenishment / Replacement PO report first.')
    stores, _ = _authorized_store_context(db, config)
    start, end = _dates(config)
    result = run_replenishment_report(
        db, start_date=start, end_date=end, store_ids=config['store_ids'],
        exclude_over_four_weeks=config['exclude_over_four_weeks'],
        exclude_no_recent_sales=config['exclude_no_recent_sales'],
        manual_exclusions=config['manual_exclusions'], as_of=portal_today(),
    )
    try:
        vendor_id = int(config['po_vendor_id'])
        target_weeks = Decimal(config['target_weeks'])
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise ValueError('Choose a valid vendor and target weeks.') from exc
    quantities: dict[str, int] = {}
    for key, value in form.multi_items():
        if not str(key).startswith('final_qty::'):
            continue
        try:
            quantities[str(key)[11:]] = int(str(value))
        except ValueError as exc:
            raise ValueError('Final quantities must be whole numbers.') from exc
    preview = build_replenishment_preview(
        result, vendor_id=vendor_id, mode=config['po_mode'], target_weeks=target_weeks,
        final_quantities=quantities or None,
        preview_exclusions=form.getlist('preview_exclusion'),
    )
    return config, stores, result, preview


@router.post('/replenishment/preview')
async def replenishment_preview_route(
    request: Request, principal: Principal = Depends(reporting_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        config, stores, result, preview = _replenishment_from_form(db, form)
        context = _context(request, principal, db, config=config, result=result)
        context['criteria'] = _criteria(config, stores)
        context['po_preview'] = preview
        context['po_finalize_key'] = token_urlsafe(32)
        return request.app.state.templates.TemplateResponse(
            'v2/reporting/workbench.html', context
        )
    except (ValueError, RuntimeError) as exc:
        return request.app.state.templates.TemplateResponse(
            'v2/reporting/workbench.html',
            _context(request, principal, db, config=locals().get('config'), error=str(exc)),
            status_code=422,
        )


@router.post('/replenishment/finalize')
async def replenishment_finalize_route(
    request: Request, principal: Principal = Depends(reporting_access),
    _csrf: None = Depends(verify_csrf), db: Session = Depends(get_db),
):
    form = await request.form()
    try:
        submitted_key = str(form.get('po_finalize_key') or '').strip()
        existing = db.scalar(select(PurchaseOrder).where(
            PurchaseOrder.creation_idempotency_key == submitted_key
        )) if submitted_key else None
        if existing is not None:
            if (
                int(existing.created_by_principal_id) != int(principal.id)
                or str(existing.vendor_id) != str(form.get('po_vendor_id') or '')
            ):
                raise ValueError(
                    'This finalization token is already associated with another order.'
                )
            return RedirectResponse(
                f'/management/ordering-tool/orders/{existing.id}'
                '?created_from=replenishment&duplicate=1',
                status_code=303,
            )
        config, _stores, _result, preview = _replenishment_from_form(db, form)
        order, created = create_replenishment_purchase_order(
            db, preview=preview, created_by_principal_id=principal.id,
            idempotency_key=config['po_finalize_key'],
            selected_store_ids=config['store_ids'],
        )
        if created:
            write_v2_audit_event(db, event=V2AuditEvent(
                actor_principal_id=principal.id,
                action='REPLENISHMENT_PO_FINALIZED', domain='REPORTING',
                entity_type='purchase_order', entity_id=order.id,
                after={
                    'vendor_id': order.vendor_id, 'status': 'DRAFT',
                    'line_count': sum(line.final_qty > 0 for line in preview.lines),
                },
            ), ip=get_client_ip(request))
        db.commit()
    except IntegrityError:
        db.rollback()
        duplicate = db.scalar(select(PurchaseOrder).where(
            PurchaseOrder.creation_idempotency_key
            == locals().get('config', {}).get('po_finalize_key', '')
        ))
        if (
            duplicate is not None
            and int(duplicate.created_by_principal_id) == int(principal.id)
            and str(duplicate.vendor_id)
            == str(locals().get('config', {}).get('po_vendor_id', ''))
        ):
            return RedirectResponse(
                f'/management/ordering-tool/orders/{duplicate.id}'
                '?created_from=replenishment&duplicate=1',
                status_code=303,
            )
        raise
    except (ValueError, RuntimeError) as exc:
        db.rollback()
        context = _context(
            request, principal, db, config=locals().get('config'),
            result=locals().get('_result'), error=str(exc),
        )
        if locals().get('preview') is not None:
            context['po_preview'] = preview
            context['po_finalize_key'] = config.get('po_finalize_key', '')
        return request.app.state.templates.TemplateResponse(
            'v2/reporting/workbench.html', context, status_code=422,
        )
    duplicate_suffix = '' if created else '&duplicate=1'
    return RedirectResponse(
        f'/management/ordering-tool/orders/{order.id}'
        f'?created_from=replenishment{duplicate_suffix}',
        status_code=303,
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
