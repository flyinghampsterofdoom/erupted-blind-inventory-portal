from __future__ import annotations

import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal, Role, require_capability
from app.db import get_db
from app.dependencies import get_client_ip
from app.routers.v2 import V2Page, _visible_navigation
from app.security.csrf import verify_csrf
from app.services.v2_exchange_return_service import (
    FEATURE_KEY,
    ExchangeReturnInput,
    HistoryFilters,
    get_exchange_return_detail,
    issue_submission_token,
    list_exchange_returns,
    submit_exchange_return,
)
from app.v2.audit import V2AuditEvent, write_v2_audit_event
from app.v2.feature_exposure import require_v2_feature
from app.v2.results import ActionResult, ResultKind, SaveOutcome
from app.v2.statuses import status_context
from app.v2.store_scope import (
    ScopeMode,
    list_authorized_stores,
    resolve_request_store_scope,
    resolve_store_scope,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix='/v2/customer-forms/exchanges-returns', tags=['v2-exchanges-returns'])
feature_access = require_v2_feature(FEATURE_KEY)
store_access = require_capability('store.access', Role.STORE)
management_access = require_capability('management.access', Role.ADMIN, Role.MANAGER, Role.LEAD)


def _page(label: str, description: str, route_path: str) -> V2Page:
    return V2Page(
        slug='customer-forms/exchanges-returns',
        label=label,
        description=description,
        route_path=route_path,
        badge='Owner Preview',
        active_prefix='/v2/customer-forms/exchanges-returns',
    )


def _scope_context(scope, authorized_stores) -> dict:
    selected_ids = list(scope.store_ids)
    if scope.mode == ScopeMode.ALL:
        label = 'All Stores'
    elif len(scope.store_names) == 1:
        label = scope.store_names[0]
    else:
        label = f'{len(scope.store_names)} stores'
    return {
        'stores': [{'id': store.id, 'name': store.name} for store in authorized_stores],
        'selected_store_ids': selected_ids,
        'all_stores_selected': scope.mode == ScopeMode.ALL,
        'store_scope_label': label,
        'scope_locked': scope.locked,
    }


def _base_context(request: Request, principal: Principal, page: V2Page, scope, authorized_stores) -> dict:
    return {
        'request': request,
        'principal': principal,
        'page': page,
        'navigation': _visible_navigation(request),
        **_scope_context(scope, authorized_stores),
    }


def _form_values(form) -> tuple[dict[str, str], dict[str, str], ExchangeReturnInput | None]:
    values = {
        'original_purchase_date': str(form.get('original_purchase_date', '')).strip(),
        'original_ticket_number': str(form.get('original_ticket_number', '')).strip(),
        'exchange_ticket_number': str(form.get('exchange_ticket_number', '')).strip(),
        'items_text': str(form.get('items_text', '')).strip(),
        'reason_text': str(form.get('reason_text', '')).strip(),
        'refund_given': str(form.get('refund_given', '')).strip().upper(),
        'refund_approved_by': str(form.get('refund_approved_by', '')).strip(),
    }
    labels = {
        'original_ticket_number': 'Original ticket number',
        'exchange_ticket_number': 'Exchange ticket number',
        'items_text': 'Item information',
        'reason_text': 'Reason',
        'refund_approved_by': 'Refund approval name',
    }
    errors: dict[str, str] = {}
    try:
        original_purchase_date = date.fromisoformat(values['original_purchase_date'])
    except ValueError:
        original_purchase_date = None
        errors['original_purchase_date'] = 'Enter a valid original purchase date.'
    for field, label in labels.items():
        if not values[field]:
            errors[field] = f'{label} is required.'
    if values['refund_given'] not in {'Y', 'N'}:
        errors['refund_given'] = 'Choose whether a refund was given.'
    if errors or original_purchase_date is None:
        return values, errors, None
    return (
        values,
        errors,
        ExchangeReturnInput(
            original_purchase_date=original_purchase_date,
            original_ticket_number=values['original_ticket_number'],
            exchange_ticket_number=values['exchange_ticket_number'],
            items_text=values['items_text'],
            reason_text=values['reason_text'],
            refund_given=values['refund_given'] == 'Y',
            refund_approved_by=values['refund_approved_by'],
        ),
    )


def _render_form(
    request: Request,
    db: Session,
    principal: Principal,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    result: ActionResult | None = None,
    submission_token: str | None = None,
    status_code: int = 200,
):
    if principal.store_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Employee account has no assigned store')
    authorized_stores = list_authorized_stores(db, principal)
    scope = resolve_request_store_scope(request, db, principal, for_write=True)
    submitted_raw = request.query_params.get('submitted', '')
    success = None
    if submitted_raw.isdigit():
        detail = get_exchange_return_detail(
            db,
            record_id=int(submitted_raw),
            authorized_store_ids=(principal.store_id,),
        )
        if detail and detail['actor_principal_id'] == principal.id:
            success = {
                'record_id': detail['id'],
                'created_at': detail['created_at'],
                'duplicate': request.query_params.get('duplicate') == '1',
            }
    context = _base_context(
        request,
        principal,
        _page('Exchanges & Returns', 'Record a non-resellable exchange or return for your assigned store.', '/v2/customer-forms/exchanges-returns'),
        scope,
        authorized_stores,
    )
    context.update(
        {
            'values': values or {},
            'errors': errors or {},
            'result': result,
            'submission_token': submission_token or issue_submission_token(principal_id=principal.id),
            'success': success,
            'submitted_status': status_context('submitted'),
        }
    )
    return request.app.state.templates.TemplateResponse(
        'v2/exchanges_returns_form.html', context, status_code=status_code
    )


@router.get('')
def submission_page(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(store_access),
    db: Session = Depends(get_db),
):
    return _render_form(request, db, principal)


@router.post('')
async def submit_form(
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(store_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    resolve_request_store_scope(request, db, principal, for_write=True)
    form = await request.form()
    submission_token = str(form.get('submission_token', '')).strip()
    values, errors, parsed = _form_values(form)
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
            values=values,
            errors=errors,
            result=result,
            submission_token=submission_token,
            status_code=422,
        )
    try:
        outcome = submit_exchange_return(
            db,
            principal=principal,
            submission_token=submission_token,
            values=parsed,
            ip=get_client_ip(request),
        )
        db.commit()
    except (ValueError, PermissionError) as exc:
        db.rollback()
        errors = {'submission': str(exc)}
        result = ActionResult(
            kind=ResultKind.VALIDATION_ERROR if isinstance(exc, ValueError) else ResultKind.AUTHORIZATION_FAILURE,
            message=str(exc),
            save_outcome=SaveOutcome.NOTHING_SAVED,
            field_errors=errors,
        )
        return _render_form(
            request,
            db,
            principal,
            values=values,
            errors=errors,
            result=result,
            submission_token=issue_submission_token(principal_id=principal.id),
            status_code=422 if isinstance(exc, ValueError) else 403,
        )
    except (SQLAlchemyError, RuntimeError) as exc:
        db.rollback()
        correlation_id = str(uuid.uuid4())
        # Do not allow driver-rendered SQL parameters or submitted form values
        # to escape through an exception traceback.
        logger.error(
            'Exchange/return persistence failed correlation_id=%s error_type=%s',
            correlation_id,
            type(exc).__name__,
        )
        result = ActionResult(
            kind=ResultKind.SERVER_FAILURE,
            message='The form could not be saved. Nothing was committed. Try again with the reference below.',
            save_outcome=SaveOutcome.NOTHING_SAVED,
            correlation_id=correlation_id,
            safe_retry=True,
        )
        return _render_form(
            request,
            db,
            principal,
            values=values,
            errors={'submission': result.message},
            result=result,
            # Retain the same idempotency token when commit outcome may be
            # uncertain. A retry can then recognize a transaction that reached
            # PostgreSQL instead of creating a second record with a fresh token.
            submission_token=submission_token,
            status_code=500,
        )
    return RedirectResponse(
        f'/v2/customer-forms/exchanges-returns?submitted={outcome.record_id}&duplicate={int(outcome.duplicate)}',
        status_code=303,
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
    raw = {
        'from': request.query_params.get('from', '').strip(),
        'to': request.query_params.get('to', '').strip(),
        'actor': request.query_params.get('actor', '').strip(),
        'q': request.query_params.get('q', '').strip(),
        'refund': request.query_params.get('refund', '').strip().lower(),
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
    if raw['refund'] not in {'', 'yes', 'no'}:
        errors['refund'] = 'Choose Any, Yes, or No.'
    refund_given = None if raw['refund'] == '' else raw['refund'] == 'yes'
    if from_date and to_date and from_date > to_date:
        errors['to'] = 'To date must be on or after From date.'
    rows = [] if errors else list_exchange_returns(
        db,
        filters=HistoryFilters(
            store_ids=scope.store_ids,
            from_date=from_date,
            to_date=to_date,
            actor=raw['actor'],
            search=raw['q'],
            refund_given=refund_given,
        ),
    )
    context = _base_context(
        request,
        principal,
        _page('Exchange & Return History', 'Review submitted records across your authorized store scope.', '/v2/customer-forms/exchanges-returns/history'),
        scope,
        authorized_stores,
    )
    context.update({'rows': rows, 'filters': raw, 'errors': errors, 'submitted_status': status_context('submitted')})
    return request.app.state.templates.TemplateResponse(
        'v2/exchanges_returns_history.html', context, status_code=422 if errors else 200
    )


@router.get('/{record_id}')
def detail_page(
    record_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(management_access),
    db: Session = Depends(get_db),
):
    authorized_stores = list_authorized_stores(db, principal)
    scope = resolve_store_scope(
        principal=principal,
        authorized_stores=authorized_stores,
        request_all=True,
    )
    detail = get_exchange_return_detail(db, record_id=record_id, authorized_store_ids=scope.store_ids)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Exchange/return record not found')
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=principal.id,
            action='VIEWED',
            domain='CUSTOMER_FORMS',
            entity_type='exchange_return_form',
            entity_id=record_id,
            store_ids=(detail['store_id'],),
            metadata={'view': 'management_detail'},
        ),
        ip=get_client_ip(request),
    )
    db.commit()
    context = _base_context(
        request,
        principal,
        _page(f'Exchange & Return #{record_id}', 'Read-only submitted record and available audit evidence.', f'/v2/customer-forms/exchanges-returns/{record_id}'),
        scope,
        authorized_stores,
    )
    context.update({'detail': detail, 'submitted_status': status_context('submitted')})
    return request.app.state.templates.TemplateResponse('v2/exchanges_returns_detail.html', context)
