from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, get_current_principal, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.routers.v2 import V2Page, _visible_navigation
from app.security.csrf import verify_csrf
from app.services.v2_daily_store_log_service import (
    FEATURE_KEY,
    DailyLogConflict,
    DailyLogInput,
    DailyLogValidationError,
    HistoryFilters,
    get_daily_log_detail,
    get_own_receipt,
    issue_action_token,
    issue_submission_token,
    list_daily_logs,
    perform_management_action,
    portal_today,
    submit_daily_log,
)
from app.v2.feature_exposure import require_v2_feature
from app.v2.current_store import CurrentStore, current_store_for_request
from app.v2.results import ActionResult, ResultKind, SaveOutcome
from app.v2.statuses import status_context
from app.v2.store_scope import ScopeMode, list_authorized_stores, resolve_request_store_scope, resolve_store_scope


logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v2/store-operations/daily-logs', tags=['v2-daily-store-logs'])
feature_access = require_v2_feature(FEATURE_KEY)
store_access = require_capability('store.access', Role.STORE)
management_access = require_capability('management.access', Role.ADMIN, Role.MANAGER, Role.LEAD)


def _page(label: str, description: str, route_path: str) -> V2Page:
    return V2Page(
        slug='store-operations/daily-logs',
        label=label,
        description=description,
        route_path=route_path,
        badge='Owner Preview',
        active_prefix='/v2/store-operations/daily-logs',
    )


def _locked_scope(label: str, *, caption: str) -> dict:
    return {
        'stores': [],
        'selected_store_ids': [],
        'all_stores_selected': False,
        'store_scope_label': label,
        'scope_locked': True,
        'scope_caption': caption,
    }


def _scope_context(scope, authorized_stores) -> dict:
    if scope.mode == ScopeMode.ALL:
        label = 'All Stores'
    elif len(scope.store_names) == 1:
        label = scope.store_names[0]
    else:
        label = f'{len(scope.store_names)} stores'
    return {
        'stores': [{'id': store.id, 'name': store.name} for store in authorized_stores],
        'selected_store_ids': list(scope.store_ids),
        'all_stores_selected': scope.mode == ScopeMode.ALL,
        'store_scope_label': label,
        'scope_locked': False,
        'scope_caption': 'Stores',
    }


def _base_context(
    request: Request,
    principal: Principal,
    page: V2Page,
    scope_context: dict,
) -> dict:
    return {
        'request': request,
        'principal': principal,
        'page': page,
        'navigation': _visible_navigation(request),
        **scope_context,
    }


def _raw_form_values(form) -> dict[str, str]:
    return {
        'general_summary': str(form.get('general_summary', '')).strip(),
        'customer_incidents': str(form.get('customer_incidents', '')).strip(),
        'inventory_concerns': str(form.get('inventory_concerns', '')).strip(),
        'facility_equipment_issues': str(form.get('facility_equipment_issues', '')).strip(),
        'staffing_coverage_notes': str(form.get('staffing_coverage_notes', '')).strip(),
        'follow_up_items': str(form.get('follow_up_items', '')).strip(),
        'no_issues_reported': '1' if form.get('no_issues_reported') else '',
        'follow_up_required': '1' if form.get('follow_up_required') else '',
    }


def _parse_input(values: dict[str, str]) -> tuple[DailyLogInput | None, dict[str, str]]:
    return (
        DailyLogInput(
            general_summary=values['general_summary'],
            customer_incidents=values['customer_incidents'],
            inventory_concerns=values['inventory_concerns'],
            facility_equipment_issues=values['facility_equipment_issues'],
            staffing_coverage_notes=values['staffing_coverage_notes'],
            follow_up_items=values['follow_up_items'],
            no_issues_reported=values['no_issues_reported'] == '1',
            follow_up_required=values['follow_up_required'] == '1',
        ),
        {},
    )


def _current_store_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    return RedirectResponse(
        f'/v2/current-store?return_to={quote(target, safe="")}',
        status_code=303,
    )


def _natural_date(value: date, *, today: date | None = None) -> str:
    display = value.strftime('%B %d, %Y').replace(' 0', ' ')
    return f'Today — {display}' if value == (today or portal_today()) else display


def _render_form(
    request: Request,
    db: Session,
    principal: Principal,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    result: ActionResult | None = None,
    submission_token: str | None = None,
    conflict_own_record_id: int | None = None,
    current_store: CurrentStore,
    status_code: int = 200,
):
    submitted_raw = request.query_params.get('submitted', '')
    success = None
    if submitted_raw.isdigit():
        success = get_own_receipt(db, record_id=int(submitted_raw), principal_id=principal.id)
        if success:
            success['duplicate'] = request.query_params.get('duplicate') == '1'
    context = _base_context(
        request,
        principal,
        _page(
            'Daily Store Log',
            'Record the important operational facts for the store where you are working.',
            '/v2/store-operations/daily-logs',
        ),
        _locked_scope(current_store.name, caption='Current Store'),
    )
    context.update(
        {
            'values': values or {},
            'errors': errors or {},
            'result': result,
            'submission_token': submission_token or issue_submission_token(principal_id=principal.id),
            'success': success,
            'conflict_own_record_id': conflict_own_record_id,
            'current_store': current_store,
            'show_current_store_context': True,
            'business_date': portal_today(),
            'business_date_label': _natural_date(portal_today()),
        }
    )
    return request.app.state.templates.TemplateResponse(
        'v2/daily_store_log_form.html',
        context,
        status_code=status_code,
    )


@router.get('')
def submission_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(store_access),
    db: Session = Depends(get_db),
):
    current_store = current_store_for_request(request, db)
    if current_store is None:
        return _current_store_redirect(request)
    return _render_form(request, db, principal, current_store=current_store)


@router.post('')
async def submit_form(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(store_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    current_store = current_store_for_request(request, db)
    if current_store is None:
        return _current_store_redirect(request)
    form = await request.form()
    token = str(form.get('submission_token', '')).strip()
    values = _raw_form_values(form)
    parsed, errors = _parse_input(values)
    if errors or parsed is None:
        result = ActionResult(
            kind=ResultKind.VALIDATION_ERROR,
            message='Check the highlighted fields. Nothing was saved.',
            save_outcome=SaveOutcome.NOTHING_SAVED,
            field_errors=errors,
        )
        return _render_form(
            request,
            db,
            principal,
            current_store=current_store,
            values=values,
            errors=errors,
            result=result,
            submission_token=token,
            status_code=422,
        )
    try:
        outcome = submit_daily_log(
            db,
            principal=principal,
            submission_token=token,
            current_store_id=current_store.id,
            values=parsed,
            ip=get_client_ip(request),
            now=datetime.now(tz=timezone.utc),
        )
        db.commit()
    except DailyLogValidationError as exc:
        db.rollback()
        result = ActionResult(
            kind=ResultKind.VALIDATION_ERROR,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
            field_errors=exc.field_errors,
        )
        return _render_form(
            request,
            db,
            principal,
            current_store=current_store,
            values=values,
            errors=exc.field_errors,
            result=result,
            submission_token=token,
            status_code=422,
        )
    except DailyLogConflict as exc:
        db.rollback()
        errors = {'submission': str(exc)}
        result = ActionResult(
            kind=ResultKind.CONFLICT,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
            field_errors=errors,
            safe_retry=False,
        )
        return _render_form(
            request,
            db,
            principal,
            current_store=current_store,
            values=values,
            errors=errors,
            result=result,
            submission_token=token,
            conflict_own_record_id=exc.own_record_id,
            status_code=409,
        )
    except ValueError as exc:
        db.rollback()
        errors = {'submission': str(exc)}
        result = ActionResult(
            kind=ResultKind.VALIDATION_ERROR,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
            field_errors=errors,
        )
        return _render_form(
            request,
            db,
            principal,
            current_store=current_store,
            values=values,
            errors=errors,
            result=result,
            submission_token=issue_submission_token(principal_id=principal.id),
            status_code=422,
        )
    except (SQLAlchemyError, RuntimeError) as exc:
        db.rollback()
        correlation_id = str(uuid.uuid4())
        logger.error(
            'Daily Store Log persistence failed correlation_id=%s error_type=%s',
            correlation_id,
            type(exc).__name__,
        )
        result = ActionResult(
            kind=ResultKind.SERVER_FAILURE,
            message='The Daily Store Log could not be saved. Try again using the reference below.',
            save_outcome=SaveOutcome.NOTHING_SAVED,
            correlation_id=correlation_id,
            safe_retry=True,
        )
        return _render_form(
            request,
            db,
            principal,
            current_store=current_store,
            values=values,
            errors={'submission': result.message},
            result=result,
            submission_token=token,
            status_code=500,
        )
    return RedirectResponse(
        f'/v2/store-operations/daily-logs?submitted={outcome.record_id}&duplicate={int(outcome.duplicate)}',
        status_code=303,
    )


def _history_values(request: Request) -> tuple[dict[str, str], dict[str, str], dict]:
    raw = {
        'from': request.query_params.get('from', '').strip(),
        'to': request.query_params.get('to', '').strip(),
        'actor': request.query_params.get('actor', '').strip(),
        'status': request.query_params.get('status', '').strip().upper(),
        'follow_up': request.query_params.get('follow_up', '').strip().lower(),
        'q': request.query_params.get('q', '').strip(),
        'page': request.query_params.get('page', '1').strip(),
    }
    errors: dict[str, str] = {}
    try:
        from_date = date.fromisoformat(raw['from']) if raw['from'] else None
    except ValueError:
        from_date = None
        errors['from'] = 'Enter a valid From date.'
    try:
        to_date = date.fromisoformat(raw['to']) if raw['to'] else None
    except ValueError:
        to_date = None
        errors['to'] = 'Enter a valid To date.'
    if from_date and to_date and from_date > to_date:
        errors['to'] = 'To date must be on or after From date.'
    if raw['status'] not in {'', 'SUBMITTED', 'ACKNOWLEDGED', 'RESOLVED'}:
        errors['status'] = 'Choose a supported lifecycle status.'
    if raw['follow_up'] not in {'', 'yes', 'no'}:
        errors['follow_up'] = 'Choose Any, Yes, or No.'
    page = int(raw['page']) if raw['page'].isdigit() and int(raw['page']) > 0 else 1
    return (
        raw,
        errors,
        {
            'from_date': from_date,
            'to_date': to_date,
            'follow_up_required': None if not raw['follow_up'] else raw['follow_up'] == 'yes',
            'page': page,
        },
    )


@router.get('/history')
def history_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    authorized_stores = list_authorized_stores(db, principal)
    scope = resolve_request_store_scope(request, db, principal)
    raw, errors, parsed = _history_values(request)
    rows, total = ([], 0)
    if not errors:
        rows, total = list_daily_logs(
            db,
            filters=HistoryFilters(
                store_ids=scope.store_ids,
                from_date=parsed['from_date'],
                to_date=parsed['to_date'],
                actor=raw['actor'],
                lifecycle_status=raw['status'],
                follow_up_required=parsed['follow_up_required'],
                search=raw['q'],
                page=parsed['page'],
            ),
        )
    context = _base_context(
        request,
        principal,
        _page(
            'Daily Store Log History',
            'Review Daily Store Logs across your authorized store scope.',
            '/v2/store-operations/daily-logs/history',
        ),
        _scope_context(scope, authorized_stores),
    )
    context.update(
        {
            'rows': rows,
            'total': total,
            'filters': raw,
            'errors': errors,
            'page_number': parsed['page'],
            'page_size': 50,
        }
    )
    return request.app.state.templates.TemplateResponse(
        'v2/daily_store_log_history.html',
        context,
        status_code=422 if errors else 200,
    )


def _detail_access(
    request: Request,
    db: Session,
    principal: Principal,
    detail: dict,
) -> tuple[bool, bool]:
    flags = getattr(request.state, 'permission_flags', {}) or {}
    owner_allowed = bool(flags.get('store.access')) and detail['submitted_by_principal_id'] == principal.id
    management_allowed = False
    if flags.get('management.access'):
        authorized = list_authorized_stores(db, principal)
        try:
            scope = resolve_store_scope(
                principal=principal,
                authorized_stores=authorized,
                request_all=True,
            )
        except HTTPException:
            scope = None
        management_allowed = scope is not None and detail['store_id'] in scope.store_ids
    return owner_allowed, management_allowed


def _render_detail(
    request: Request,
    db: Session,
    principal: Principal,
    detail: dict,
    *,
    can_manage: bool,
    result: ActionResult | None = None,
    status_code: int = 200,
    action_token_overrides: dict[str, str] | None = None,
):
    token_overrides = action_token_overrides or {}
    context = _base_context(
        request,
        principal,
        _page(
            f'Daily Store Log #{detail["id"]}',
            'Submitted report and management action history.',
            f'/v2/store-operations/daily-logs/{detail["id"]}',
        ),
        _locked_scope(detail['store_name'], caption='Record store'),
    )
    context.update(
        {
            'detail': detail,
            'can_manage': can_manage,
            'result': result,
            'status_view': status_context(detail['lifecycle_status']),
            'acknowledge_token': token_overrides.get('ACKNOWLEDGED')
            or issue_action_token(
                principal_id=principal.id,
                record_id=detail['id'],
                action_type='ACKNOWLEDGED',
            )
            if can_manage
            else '',
            'follow_up_token': token_overrides.get('MARKED_FOLLOW_UP')
            or issue_action_token(
                principal_id=principal.id,
                record_id=detail['id'],
                action_type='MARKED_FOLLOW_UP',
            )
            if can_manage
            else '',
            'resolve_token': token_overrides.get('RESOLVED')
            or issue_action_token(
                principal_id=principal.id,
                record_id=detail['id'],
                action_type='RESOLVED',
            )
            if can_manage
            else '',
        }
    )
    if not can_manage:
        context['current_store'] = current_store_for_request(request, db)
        context['show_current_store_context'] = True
    return request.app.state.templates.TemplateResponse(
        'v2/daily_store_log_detail.html',
        context,
        status_code=status_code,
    )


@router.get('/{record_id}')
def detail_page(
    record_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
):
    detail = get_daily_log_detail(db, record_id=record_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    owner_allowed, management_allowed = _detail_access(request, db, principal, detail)
    if not owner_allowed and not management_allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return _render_detail(
        request,
        db,
        principal,
        detail,
        can_manage=management_allowed,
    )


def _management_store_ids(db: Session, principal: Principal) -> tuple[int, ...]:
    authorized = list_authorized_stores(db, principal)
    scope = resolve_store_scope(
        principal=principal,
        authorized_stores=authorized,
        request_all=True,
    )
    return scope.store_ids


@router.post('/{record_id}/{action_slug}')
async def management_action(
    record_id: int,
    action_slug: str,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    action_by_slug = {
        'acknowledge': 'ACKNOWLEDGED',
        'follow-up': 'MARKED_FOLLOW_UP',
        'resolve': 'RESOLVED',
    }
    action_type = action_by_slug.get(action_slug)
    if action_type is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    detail = get_daily_log_detail(db, record_id=record_id)
    if detail is None or detail['store_id'] not in _management_store_ids(db, principal):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    form = await request.form()
    token = str(form.get('action_token', '')).strip()
    note = str(form.get('response_note', '')).strip()
    try:
        outcome = perform_management_action(
            db,
            principal=principal,
            record_id=record_id,
            action_type=action_type,
            action_token=token,
            response_note=note,
            authorized_store_ids=_management_store_ids(db, principal),
            ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, PermissionError) as exc:
        db.rollback()
        refreshed = get_daily_log_detail(db, record_id=record_id)
        if refreshed is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
        result = ActionResult(
            kind=ResultKind.CONFLICT if isinstance(exc, ValueError) else ResultKind.AUTHORIZATION_FAILURE,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
        )
        return _render_detail(
            request,
            db,
            principal,
            refreshed,
            can_manage=True,
            result=result,
            status_code=409 if isinstance(exc, ValueError) else 403,
        )
    except (SQLAlchemyError, RuntimeError) as exc:
        db.rollback()
        correlation_id = str(uuid.uuid4())
        logger.error(
            'Daily Store Log action failed correlation_id=%s error_type=%s',
            correlation_id,
            type(exc).__name__,
        )
        refreshed = get_daily_log_detail(db, record_id=record_id)
        if refreshed is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
        result = ActionResult(
            kind=ResultKind.SERVER_FAILURE,
            message='The management action could not be saved. Retry with the reference below.',
            save_outcome=SaveOutcome.NOTHING_SAVED,
            correlation_id=correlation_id,
            safe_retry=True,
        )
        response = _render_detail(
            request,
            db,
            principal,
            refreshed,
            can_manage=True,
            result=result,
            status_code=500,
            action_token_overrides={action_type: token},
        )
        return response
    return RedirectResponse(
        f'/v2/store-operations/daily-logs/{record_id}?action={action_slug}&duplicate={int(outcome.duplicate)}',
        status_code=303,
    )
