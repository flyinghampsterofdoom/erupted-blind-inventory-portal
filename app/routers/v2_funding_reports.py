from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import Principal
from app.config import settings
from app.db import get_db
from app.dependencies import get_client_ip
from app.models import (
    ConsignmentReturnFact,
    ConsignmentSaleFact,
    FundingAccount,
    FundingPayment,
    FundingPaymentAllocation,
    FundingReport,
    FundingReportAdjustment,
    FundingReportExclusion,
    FundingReportFactLink,
    FundingReportLine,
    FundingSkuMapping,
    PaymentMethod,
    Principal as PrincipalRecord,
    Store,
    Vendor,
    VendorPaymentSetting,
)
from app.routers.v2_order_payments import _context, _page, feature_access, owner_access
from app.security.csrf import verify_csrf
from app.services.v2_funding_reports_service import (
    ADJUSTMENT_TYPES,
    account_summary,
    active_adjustments,
    active_payment_allocations,
    add_adjustment,
    bulk_assign_skus,
    calculate_combined_report,
    calculate_report,
    catalog_rows,
    combined_report_members,
    correct_funding_po_line_cost,
    create_funding_account,
    delete_report,
    delete_draft_report,
    eligible_vendors_for_account,
    finalize_report,
    funding_account_vendor_memberships,
    funding_po_cost_correction_history,
    funding_report_required_coverage_start,
    funding_report_fifo_exceptions,
    funding_report_source_readiness,
    is_combined_report,
    overlapping_reports,
    record_compact_payment,
    record_ledger_entry,
    record_payment,
    report_position,
    resolve_account_vendor,
    resolve_assigned_po_line_identities,
    resolve_funding_po_line_identity,
    resolve_funding_report_fifo_exception,
    reverse_adjustment,
    reverse_ledger_entry,
    reverse_payment,
    set_funding_account_status,
    update_credit_terms,
    void_report,
)
from app.services.v2_order_payments_service import portal_today
from app.services.v2_square_data_service import refresh_square_sales_data


router = APIRouter()


def _back(path: str, *, message: str = '', error: str = '') -> RedirectResponse:
    query = f'?message={quote(message)}' if message else (f'?error={quote(error)}' if error else '')
    base, separator, fragment = path.partition('#')
    location = f'{base}{query}'
    if separator:
        location = f'{location}#{fragment}'
    return RedirectResponse(location, status_code=303)


def _action_gate(account: FundingAccount) -> None:
    enabled = (
        settings.v2_consignment_cogs_actions_enabled
        if account.account_type == 'CONSIGNMENT'
        else settings.v2_credit_card_cogs_actions_enabled
    )
    if not enabled:
        raise HTTPException(status_code=403, detail=f'{account.account_type.title()} report actions are not enabled.')


def _funding_context(request: Request, principal: Principal, *, label: str, path: str, **values) -> dict:
    context = _context(request, principal, page=_page(label, 'Funding account reports and settlement history.', path), **values)
    context['payment_tabs'] = (
        ('Order Payments', '/v2/order-payments'),
        ('Financial Assignment', '/v2/order-payments/vendor-reassignment'),
        ('Payment Methods', '/v2/payment-methods'),
        ('Funding Accounts', '/v2/funding-accounts'),
        ('Consignment', '/v2/consignment'),
    )
    context['consignment_actions_enabled'] = settings.v2_consignment_cogs_actions_enabled
    context['credit_card_actions_enabled'] = settings.v2_credit_card_cogs_actions_enabled
    context['money'] = lambda value: f'${Decimal(str(value or 0)):,.2f}'
    return context


def _purchase_order_source_display_rows(source_scope: dict, line_totals: dict) -> list[dict]:
    rows = []
    empty_totals = {
        'units_sold': Decimal('0'), 'units_returned': Decimal('0'),
        'net_units': Decimal('0'), 'calculated_cogs': Decimal('0'),
    }
    for source in source_scope.get('source_lines', []):
        row = {**source, **line_totals.get(
            f"PO_LINE:{source.get('purchase_order_line_id')}", empty_totals)}
        # JSON snapshots preserve PO costs as strings; restore the numeric type
        # expected by the shared currency formatter before rendering history.
        if row.get('unit_cost') is not None:
            row['unit_cost'] = Decimal(str(row['unit_cost']))
        rows.append(row)
    return rows


def _report_history_rows(summary: dict, vendors: dict[int, Vendor] | None = None) -> list[dict]:
    vendors = vendors or {}
    rows = []
    for report in summary['reports']:
        position = summary['positions'][report.id]
        rows.append({
            'report': report,
            'vendor_name': ('All vendors' if is_combined_report(report) else (
                vendors[report.vendor_id].name
                if report.vendor_id in vendors else 'Unknown/Legacy'
            )),
            'effective_cogs': (
                position['adjusted_amount']
                if position['adjustments']
                else report.calculated_cogs
            ),
            'paid': position['settled_amount'] > 0 and position['remaining_amount'] == 0,
            'version_token': (
                f'{report.status}|'
                f'{(report.updated_at or report.created_at).isoformat()}'
            ),
        })
    return rows


def _report_history_date(value: date) -> str:
    return value.strftime('%m/%d/%Y')


@router.get('/v2/funding-accounts')
def funding_accounts_page(request: Request, _feature: Principal = Depends(feature_access),
                          principal: Principal = Depends(owner_access), db: Session = Depends(get_db)):
    accounts = db.scalars(select(FundingAccount).order_by(FundingAccount.account_type, FundingAccount.display_name)).all()
    summaries = [account_summary(db, account_id=row.id) for row in accounts]
    configured_vendor_ids = {row.vendor_id for row in accounts if row.vendor_id is not None}
    consignment_vendors = db.scalars(select(Vendor).join(
        VendorPaymentSetting, VendorPaymentSetting.vendor_id == Vendor.id
    ).join(PaymentMethod, PaymentMethod.id == VendorPaymentSetting.default_payment_method_id).where(
        Vendor.active.is_(True), PaymentMethod.category == 'CONSIGNMENT',
        Vendor.id.not_in(configured_vendor_ids or {-1}),
    ).order_by(Vendor.name)).all()
    configured_method_ids = {row.payment_method_id for row in accounts if row.payment_method_id is not None}
    card_methods = db.scalars(select(PaymentMethod).where(
        PaymentMethod.category == 'CREDIT_CARD', PaymentMethod.is_active.is_(True),
        PaymentMethod.id.not_in(configured_method_ids or {-1}),
    ).order_by(PaymentMethod.display_name)).all()
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_accounts.html',
        _funding_context(request, principal, label='Funding Accounts', path='/v2/funding-accounts',
            accounts=accounts, summaries=summaries, consignment_vendors=consignment_vendors,
            card_methods=card_methods))


@router.post('/v2/funding-accounts')
async def create_funding_account_action(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    form = await request.form(); account_type = str(form.get('account_type') or '').upper()
    if account_type == 'CONSIGNMENT' and not settings.v2_consignment_cogs_actions_enabled:
        raise HTTPException(status_code=403)
    if account_type == 'CREDIT_CARD' and not settings.v2_credit_card_cogs_actions_enabled:
        raise HTTPException(status_code=403)
    try:
        create_funding_account(db, account_type=account_type,
            vendor_id=int(form.get('vendor_id')) if form.get('vendor_id') else None,
            payment_method_id=int(form.get('payment_method_id')) if form.get('payment_method_id') else None,
            display_name=str(form.get('display_name') or ''), issuer=str(form.get('issuer') or ''),
            account_nickname=str(form.get('account_nickname') or ''), last_four=str(form.get('last_four') or ''),
            internal_notes=str(form.get('internal_notes') or ''), actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back('/v2/funding-accounts', message='Funding account created.')
    except (ValueError, TypeError) as exc:
        db.rollback(); return _back('/v2/funding-accounts', error=str(exc))


@router.get('/v2/funding-accounts/mappings')
def funding_mappings_page(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db)):
    accounts = db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(
        FundingAccount.account_type, FundingAccount.display_name)).all()
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_mappings.html',
        _funding_context(request, principal, label='Legacy Identity Repair', path='/v2/funding-accounts/mappings',
            accounts=accounts, rows=catalog_rows(db), today=date.today()))


@router.post('/v2/funding-accounts/mappings')
async def funding_mappings_action(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    form = await request.form()
    try:
        account = db.get(FundingAccount, int(str(form.get('account_id') or '')))
        if account is None: raise ValueError('Choose an account.')
        _action_gate(account)
        rows = bulk_assign_skus(db, account_id=account.id, skus=[str(v) for v in form.getlist('skus')],
            effective_date=date.fromisoformat(str(form.get('effective_date') or '')),
            unit_cost=Decimal(str(form.get('unit_cost') or '')), reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back('/v2/funding-accounts/mappings', message=f'{len(rows)} SKU mapping(s) saved.')
    except (ValueError, TypeError, InvalidOperation) as exc:
        db.rollback(); return _back('/v2/funding-accounts/mappings', error=str(exc))


@router.get('/v2/funding-accounts/reports/new')
def funding_report_new_page(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db)):
    accounts = db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(
        FundingAccount.account_type, FundingAccount.display_name)).all()
    stores = db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all()
    raw_account_id = str(request.query_params.get('account_id') or '').strip()
    selected_account = db.get(FundingAccount, int(raw_account_id)) if raw_account_id.isdigit() else None
    eligible_vendors = (eligible_vendors_for_account(db, account=selected_account)
        if selected_account is not None else [])
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_report_new.html',
        _funding_context(request, principal, label='Create Report', path='/v2/funding-accounts/reports/new',
            accounts=accounts, report_stores=stores, overlaps=[],
            submitted={'account_id': raw_account_id}, selected_account=selected_account,
            eligible_vendors=eligible_vendors, today=portal_today()))


@router.post('/v2/funding-accounts/reports')
async def calculate_funding_report_action(request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    form = await request.form(); submitted = dict(form)
    try:
        account_id_value = str(form.get('account_id') or '').strip()
        if not account_id_value:
            raise ValueError('Choose an account.')
        account = db.get(FundingAccount, int(account_id_value))
        if account is None: raise ValueError('Choose an account.')
        _action_gate(account)
        start_date=date.fromisoformat(str(form.get('start_date') or ''))
        end_date=date.fromisoformat(str(form.get('end_date') or ''))
        raw_vendor_id = str(form.get('vendor_id') or '').strip()
        vendor = resolve_account_vendor(db, account=account,
            vendor_id=int(raw_vendor_id) if raw_vendor_id else None)
        if account.account_type == 'CREDIT_CARD':
            resolve_assigned_po_line_identities(
                db, account=account, vendor=vendor, actor_id=principal.id,
                ip=get_client_ip(request),
            )
        overlaps=overlapping_reports(db, account_id=account.id, vendor_id=vendor.id,
            start_date=start_date, end_date=end_date)
        acknowledged=str(form.get('overlap_acknowledged') or '') == '1'
        if overlaps and not acknowledged:
            accounts=db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(
                FundingAccount.account_type, FundingAccount.display_name)).all()
            stores=db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all()
            return request.app.state.templates.TemplateResponse('v2/order_payments/funding_report_new.html',
                _funding_context(request, principal, label='Create Report', path='/v2/funding-accounts/reports/new',
                    accounts=accounts, report_stores=stores, overlaps=overlaps, submitted=submitted,
                    selected_account=account,
                    eligible_vendors=eligible_vendors_for_account(db, account=account),
                    submitted_store_ids=[int(v) for v in form.getlist('store_ids')], today=portal_today()))
        coverage_start_date = funding_report_required_coverage_start(
            db, account=account, vendor=vendor, requested_start=start_date
        )
        readiness = funding_report_source_readiness(
            db, start_date=coverage_start_date, end_date=end_date
        )
        if readiness['blockers']:
            refresh = refresh_square_sales_data(
                actor_id=principal.id,
                force=True,
                start_at=readiness['period_start_at'],
                end_at=datetime.now(timezone.utc),
            )
            if refresh.state == 'failed':
                raise ValueError(
                    'Square sales data is incomplete and the automatic update failed. '
                    f'{refresh.message}'
                )
            if refresh.state == 'updating':
                raise ValueError(
                    'Square sales data is incomplete and an update is already running. '
                    'Wait for Square Data to show Current, then calculate again.'
                )
            db.expire_all()
        report=calculate_report(db, account_id=account.id, start_date=start_date, end_date=end_date,
            vendor_id=vendor.id,
            store_ids=[int(v) for v in form.getlist('store_ids')], sku_filter=str(form.get('sku_filter') or ''),
            internal_note=str(form.get('internal_note') or ''), overlap_acknowledged=acknowledged,
            actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return RedirectResponse(f'/v2/funding-accounts/{account.id}/reports/{report.id}', status_code=303)
    except (ValueError, TypeError, InvalidOperation) as exc:
        db.rollback(); return _back('/v2/funding-accounts/reports/new', error=str(exc))


@router.get('/v2/funding-accounts/{account_id}/reports/combined/new')
def combined_funding_report_new_page(
    account_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
):
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404)
    vendors = eligible_vendors_for_account(db, account=account)
    return request.app.state.templates.TemplateResponse(
        'v2/order_payments/funding_combined_report_new.html',
        _funding_context(
            request,
            principal,
            label='Create Combined Report',
            path=f'/v2/funding-accounts/{account.id}/reports/combined/new',
            account=account,
            eligible_vendors=vendors,
            overlaps=[],
            submitted={},
            today=portal_today(),
        ),
    )


@router.post('/v2/funding-accounts/{account_id}/reports/combined')
async def calculate_combined_funding_report_action(
    account_id: int,
    request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404)
    _action_gate(account)
    form = await request.form()
    submitted = dict(form)
    try:
        start_date = date.fromisoformat(str(form.get('start_date') or ''))
        end_date = date.fromisoformat(str(form.get('end_date') or ''))
        vendors = eligible_vendors_for_account(db, account=account)
        if not vendors:
            raise ValueError('This Funding Account has no eligible vendors.')
        overlaps = []
        for vendor in vendors:
            overlaps.extend(overlapping_reports(
                db,
                account_id=account.id,
                vendor_id=vendor.id,
                start_date=start_date,
                end_date=end_date,
            ))
        overlaps = list({row.id: row for row in overlaps}.values())
        acknowledged = str(form.get('overlap_acknowledged') or '') == '1'
        if overlaps and not acknowledged:
            return request.app.state.templates.TemplateResponse(
                'v2/order_payments/funding_combined_report_new.html',
                _funding_context(
                    request,
                    principal,
                    label='Create Combined Report',
                    path=f'/v2/funding-accounts/{account.id}/reports/combined/new',
                    account=account,
                    eligible_vendors=vendors,
                    overlaps=overlaps,
                    submitted=submitted,
                    today=portal_today(),
                    error=(
                        'Review and acknowledge the overlapping reporting periods '
                        'to continue.'
                    ),
                ),
            )
        coverage_start = min(
            funding_report_required_coverage_start(
                db, account=account, vendor=vendor, requested_start=start_date
            )
            for vendor in vendors
        )
        readiness = funding_report_source_readiness(
            db, start_date=coverage_start, end_date=end_date
        )
        if readiness['blockers']:
            refresh = refresh_square_sales_data(
                actor_id=principal.id,
                force=True,
                start_at=readiness['period_start_at'],
                end_at=datetime.now(timezone.utc),
            )
            if refresh.state != 'current':
                raise ValueError(
                    'Square data is incomplete for at least one vendor. '
                    + refresh.message
                )
            db.expire_all()
        report = calculate_combined_report(
            db,
            account_id=account.id,
            start_date=start_date,
            end_date=end_date,
            store_ids=[],
            sku_filter='',
            internal_note=str(form.get('internal_note') or ''),
            actor_id=principal.id,
            overlap_acknowledged=acknowledged,
            ip=get_client_ip(request),
        )
        db.commit()
        return RedirectResponse(
            f'/v2/funding-accounts/{account.id}/reports/{report.id}',
            status_code=303,
        )
    except (ValueError, TypeError, InvalidOperation) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/combined/new',
            error=str(exc),
        )


@router.get('/v2/funding-accounts/{account_id}')
def funding_account_detail_page(account_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db)):
    try: summary=account_summary(
        db, account_id=account_id, include_purchase_order_lines=True)
    except LookupError as exc: raise HTTPException(status_code=404) from exc
    actors={row.id: row.username for row in db.scalars(select(PrincipalRecord)).all()}
    account = summary['account']
    vendor_memberships = funding_account_vendor_memberships(db, account=account)
    eligible_vendors = [membership.vendor for membership in vendor_memberships]
    vendor_ids = {row.vendor_id for row in summary['reports'] if row.vendor_id is not None}
    vendor_ids.update(row.vendor_id for row in summary['payments'] if row.vendor_id is not None)
    vendors = {row.id: row for row in db.scalars(select(Vendor).where(
        Vendor.id.in_(vendor_ids or [-1]))).all()}
    raw_payment_vendor = str(request.query_params.get('payment_vendor_id') or '').strip()
    selected_payment_vendor = None
    if raw_payment_vendor:
        try:
            selected_payment_vendor = resolve_account_vendor(db, account=account,
                vendor_id=int(raw_payment_vendor), purpose='payment')
        except (ValueError, TypeError):
            selected_payment_vendor = None
    payment_open_reports = [row for row in summary['open_reports']
        if account.account_type == 'CONSIGNMENT'
        or (selected_payment_vendor is not None and row.vendor_id == selected_payment_vendor.id)]
    vendor_payment_remaining = {
        membership.vendor.id: sum(
            (summary['positions'][row.id]['remaining_amount'] for row in summary['open_reports']
             if row.vendor_id == membership.vendor.id),
            Decimal('0'),
        )
        for membership in vendor_memberships
    }
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_account_detail.html',
        _funding_context(request, principal, label=summary['account'].display_name,
            path=f'/v2/funding-accounts/{account_id}', summary=summary, actors=actors,
            report_history=_report_history_rows(summary, vendors), report_history_date=_report_history_date,
            eligible_vendors=eligible_vendors, vendor_memberships=vendor_memberships,
            vendor_payment_remaining=vendor_payment_remaining,
            selected_payment_vendor=selected_payment_vendor,
            payment_open_reports=payment_open_reports, vendors=vendors,
            cost_corrections=funding_po_cost_correction_history(
                db, account_id=account.id),
            today=portal_today()))


@router.post('/v2/funding-accounts/{account_id}/po-lines/{line_id}/resolve')
async def funding_po_line_identity_action(
    account_id: int, line_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        resolve_funding_po_line_identity(
            db,
            account_id=account_id,
            purchase_order_line_id=line_id,
            square_variation_id=str(form.get('square_variation_id') or ''),
            reason=str(form.get('reason') or ''),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
        return _back(
            f'/v2/funding-accounts/{account_id}#funded-inventory',
            message='PO line product identity corrected.',
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account_id}#funded-inventory', error=str(exc)
        )


@router.post('/v2/funding-accounts/{account_id}/po-lines/{line_id}/cost')
async def funding_po_line_cost_action(
    account_id: int, line_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    form = await request.form()
    try:
        result = correct_funding_po_line_cost(
            db,
            account_id=account_id,
            purchase_order_line_id=line_id,
            unit_cost=Decimal(str(form.get('unit_cost') or '')),
            reason=str(form.get('reason') or ''),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
        invalidated = len(result['invalidated_draft_report_ids'])
        message = 'PO line unit cost corrected.'
        if invalidated:
            message += f' {invalidated} affected draft report(s) were invalidated; regenerate them.'
        if result['finalized_report_impacts']:
            message += ' Finalized values were preserved; review the posted adjustment shown below.'
        return _back(
            f'/v2/funding-accounts/{account_id}#funded-inventory', message=message)
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, InvalidOperation) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account_id}#funded-inventory', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/terms')
async def funding_account_terms_action(account_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id)
    if account is None: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    def optional_decimal(name):
        raw=str(form.get(name) or '').strip(); return Decimal(raw) if raw else None
    def optional_date(name):
        raw=str(form.get(name) or '').strip(); return date.fromisoformat(raw) if raw else None
    try:
        update_credit_terms(db, account_id=account.id, credit_limit=optional_decimal('credit_limit'),
            promotional_apr=optional_decimal('promotional_apr'), promotional_start_date=optional_date('promotional_start_date'),
            promotional_expiration_date=optional_date('promotional_expiration_date'), standard_apr=optional_decimal('standard_apr'),
            internal_notes=str(form.get('internal_notes') or ''), actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='APR and account settings saved.')
    except (ValueError, InvalidOperation) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/status')
async def funding_account_status_action(account_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id)
    if account is None: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        set_funding_account_status(db, account_id=account.id,
            is_active=str(form.get('is_active') or '') == '1', reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='Account status saved.')
    except (ValueError, LookupError) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/ledger')
async def funding_ledger_action(account_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id)
    if account is None: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        estimate_raw=str(form.get('inventory_backed_estimate') or '').strip()
        record_ledger_entry(db, account_id=account.id, entry_type=str(form.get('entry_type') or ''),
            direction=str(form.get('direction') or ''), amount=Decimal(str(form.get('amount') or '')),
            effective_date=date.fromisoformat(str(form.get('effective_date') or '')),
            reason=str(form.get('reason') or ''), internal_note=str(form.get('internal_note') or ''),
            actor_id=principal.id, ip=get_client_ip(request),
            inventory_backed_estimate=Decimal(estimate_raw) if estimate_raw else None)
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='Account activity recorded.')
    except (ValueError, InvalidOperation) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/ledger/{entry_id}/reverse')
async def funding_ledger_reverse_action(account_id: int, entry_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id)
    if account is None: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        entry=reverse_ledger_entry(db, entry_id=entry_id, reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request))
        if entry.account_id != account.id:
            raise ValueError('Account activity does not belong to this account.')
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='Account activity reversal recorded.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/payments')
async def funding_payment_action(account_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id)
    if account is None: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        allocations={}
        for report_id, amount in zip(form.getlist('allocation_report_id'), form.getlist('allocation_amount')):
            if str(report_id).strip() and str(amount).strip():
                allocations[int(report_id)]=Decimal(str(amount))
        record_payment(db, account_id=account.id, entry_type=str(form.get('entry_type') or 'PAYMENT'),
            vendor_id=(int(str(form.get('vendor_id'))) if str(form.get('vendor_id') or '').strip() else None),
            amount=Decimal(str(form.get('amount') or '')), payment_date=date.fromisoformat(str(form.get('payment_date') or '')),
            payment_source=str(form.get('payment_source') or ''), confirmation_number=str(form.get('confirmation_number') or ''),
            reason=str(form.get('reason') or ''), internal_note=str(form.get('internal_note') or ''),
            allocations=allocations, actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='Settlement activity recorded.')
    except (ValueError, InvalidOperation) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/vendor-payments/{vendor_id}')
async def compact_account_vendor_payment_action(
    account_id: int, vendor_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    account = db.get(FundingAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404)
    _action_gate(account)
    form = await request.form()
    try:
        raw_amount = str(form.get('amount') or '').strip()
        record_compact_payment(
            db,
            account_id=account.id,
            vendor_id=vendor_id,
            amount=Decimal(raw_amount) if raw_amount else None,
            paid_in_full=str(form.get('paid_in_full') or '') == '1',
            payment_date=date.fromisoformat(str(form.get('payment_date') or '')),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
        return _back(
            f'/v2/funding-accounts/{account.id}#assigned-vendors',
            message='Vendor payment recorded.',
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account.id}#assigned-vendors', error=str(exc)
        )


@router.post('/v2/funding-accounts/{account_id}/reports/{combined_report_id}/vendor-payments/{report_id}')
async def compact_vendor_payment_action(
    account_id: int, combined_report_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    account = db.get(FundingAccount, account_id)
    combined = db.get(FundingReport, combined_report_id)
    if account is None or combined is None or combined.account_id != account.id:
        raise HTTPException(status_code=404)
    _action_gate(account)
    form = await request.form()
    try:
        if report_id not in {row.id for row in combined_report_members(db, report=combined)}:
            raise ValueError('Vendor report does not belong to this combined report.')
        raw_amount = str(form.get('amount') or '').strip()
        record_compact_payment(
            db,
            account_id=account.id,
            report_id=report_id,
            amount=Decimal(raw_amount) if raw_amount else None,
            paid_in_full=str(form.get('paid_in_full') or '') == '1',
            payment_date=date.fromisoformat(str(form.get('payment_date') or '')),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{combined.id}#vendor-obligations',
            message='Vendor payment recorded.',
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{combined.id}#vendor-obligations',
            error=str(exc),
        )


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/combined-payments')
async def compact_combined_payment_action(
    account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    account = db.get(FundingAccount, account_id)
    report = db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id:
        raise HTTPException(status_code=404)
    _action_gate(account)
    form = await request.form()
    try:
        raw_amount = str(form.get('amount') or '').strip()
        record_compact_payment(
            db,
            account_id=account.id,
            report_id=report.id,
            combined=True,
            amount=Decimal(raw_amount) if raw_amount else None,
            paid_in_full=str(form.get('paid_in_full') or '') == '1',
            payment_date=date.fromisoformat(str(form.get('payment_date') or '')),
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{report.id}',
            message='Combined payment recorded.',
        )
    except (ValueError, InvalidOperation, TypeError) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc)
        )


@router.post('/v2/funding-accounts/{account_id}/payments/{payment_id}/reverse')
async def funding_payment_reverse_action(account_id: int, payment_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id)
    if account is None: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        original=db.get(FundingPayment, payment_id)
        if original is None or original.account_id != account.id:
            raise ValueError('Payment does not belong to this account.')
        reverse_payment(db, payment_id=payment_id, reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/funding-accounts/{account.id}', message='Payment reversal recorded.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


@router.get('/v2/funding-accounts/{account_id}/reports/{report_id}')
def funding_report_detail_page(account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db)):
    report=db.get(FundingReport, report_id); account=db.get(FundingAccount, account_id)
    if report is None or account is None or report.account_id != account.id: raise HTTPException(status_code=404)
    if is_combined_report(report):
        members = combined_report_members(db, report=report)
        vendors = {
            row.id: row for row in db.scalars(select(Vendor).where(
                Vendor.id.in_([member.vendor_id for member in members] or [-1])
            )).all()
        }
        member_rows = []
        for member in members:
            position = report_position(db, report_id=member.id)
            member_rows.append({
                'report': member,
                'vendor': vendors.get(member.vendor_id),
                'position': position,
                'fifo_exceptions': funding_report_fifo_exceptions(
                    db, report_id=member.id
                ),
                'purchase_order_ids': (
                    (member.warning_summary or {}).get('purchase_order_scope') or {}
                ).get('purchase_order_ids', []),
            })
        return request.app.state.templates.TemplateResponse(
            'v2/order_payments/funding_combined_report_detail.html',
            _funding_context(
                request,
                principal,
                label=f'Combined Report {report.report_number}',
                path=f'/v2/funding-accounts/{account.id}/reports/{report.id}',
                account=account,
                report=report,
                member_rows=member_rows,
                position=report_position(db, report_id=report.id),
                today=portal_today(),
            ),
        )
    lines=db.scalars(select(FundingReportLine).where(FundingReportLine.report_id==report.id).order_by(
        FundingReportLine.product_name_snapshot, FundingReportLine.store_id)).all()
    exclusions=db.scalars(select(FundingReportExclusion).where(FundingReportExclusion.report_id==report.id).order_by(
        FundingReportExclusion.reason_code, FundingReportExclusion.id)).all()
    fifo_exceptions=funding_report_fifo_exceptions(db, report_id=report.id)
    links=db.scalars(select(FundingReportFactLink).where(FundingReportFactLink.report_id==report.id)).all()
    sales={row.id:row for row in db.scalars(select(ConsignmentSaleFact).where(
        ConsignmentSaleFact.id.in_([x.sale_fact_id for x in links if x.sale_fact_id] or [-1]))).all()}
    returns={row.id:row for row in db.scalars(select(ConsignmentReturnFact).where(
        ConsignmentReturnFact.id.in_([x.return_fact_id for x in links if x.return_fact_id] or [-1]))).all()}
    position=report_position(db, report_id=report.id)
    adjustment_rows=db.scalars(select(FundingReportAdjustment).where(
        FundingReportAdjustment.report_id==report.id).order_by(FundingReportAdjustment.id)).all()
    reversed_adjustment_ids={row.reversed_adjustment_id for row in adjustment_rows
        if row.reversed_adjustment_id is not None}
    payment_allocations=db.scalars(select(FundingPaymentAllocation).where(
        FundingPaymentAllocation.report_id==report.id).order_by(FundingPaymentAllocation.id)).all()
    payment_rows={row.id: row for row in db.scalars(select(FundingPayment).where(
        FundingPayment.id.in_([allocation.payment_id for allocation in payment_allocations] or [-1]))).all()}
    reversed_payment_ids=set(db.scalars(select(FundingPayment.reversed_payment_id).where(
        FundingPayment.reversed_payment_id.in_(list(payment_rows) or [-1]))).all())
    actors={row.id: row.username for row in db.scalars(select(PrincipalRecord)).all()}
    overlaps=db.scalars(select(FundingReport).where(FundingReport.id.in_(report.overlapping_report_ids or [-1]))).all()
    source_scope=(report.warning_summary or {}).get('purchase_order_scope') or {}
    line_totals={}
    for line in lines:
        source_key=(line.warning_state if str(line.warning_state or '').startswith('PO_LINE:')
                    else f'SKU:{line.normalized_sku}')
        totals=line_totals.setdefault(source_key, {
            'units_sold': Decimal('0'), 'units_returned': Decimal('0'),
            'net_units': Decimal('0'), 'calculated_cogs': Decimal('0'),
        })
        totals['units_sold'] += Decimal(str(line.units_sold))
        totals['units_returned'] += Decimal(str(line.units_returned))
        totals['net_units'] += Decimal(str(line.net_units))
        totals['calculated_cogs'] += Decimal(str(line.extended_cogs))
    purchase_order_source_rows=_purchase_order_source_display_rows(source_scope, line_totals)
    selected_stores=db.scalars(select(Store).where(Store.id.in_(report.store_ids or [-1])).order_by(Store.name)).all()
    report_vendor = db.get(Vendor, report.vendor_id) if report.vendor_id is not None else None
    report_cost_corrections = [
        {**correction, 'impact': next(
            impact for impact in correction.get('finalized_report_impacts', [])
            if int(impact.get('report_id') or 0) == report.id
        )}
        for correction in funding_po_cost_correction_history(db, account_id=account.id)
        if any(int(impact.get('report_id') or 0) == report.id
               for impact in correction.get('finalized_report_impacts', []))
    ]
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_report_detail.html',
        _funding_context(request, principal, label=f'Report {report.report_number}',
            path=f'/v2/funding-accounts/{account.id}/reports/{report.id}', account=account, report=report,
            lines=lines, exclusions=exclusions, fifo_exceptions=fifo_exceptions,
            links=links, sales=sales, returns=returns,
            position=position, adjustment_rows=adjustment_rows, overlaps=overlaps,
            payment_allocations=payment_allocations, payment_rows=payment_rows,
            reversed_adjustment_ids=reversed_adjustment_ids, reversed_payment_ids=reversed_payment_ids,
            adjustment_types=sorted(ADJUSTMENT_TYPES), actors=actors, today=date.today(),
            report_vendor=report_vendor,
            source_scope=source_scope, purchase_order_source_rows=purchase_order_source_rows,
            report_cost_corrections=report_cost_corrections,
            selected_store_names=[row.name for row in selected_stores]))


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/fifo-exceptions/{exception_id}')
async def funding_report_fifo_exception_action(
    account_id: int, report_id: int, exception_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    account = db.get(FundingAccount, account_id)
    report = db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id:
        raise HTTPException(status_code=404)
    _action_gate(account)
    form = await request.form()
    raw_cost = str(form.get('unit_cost') or '').strip()
    try:
        exception = resolve_funding_report_fifo_exception(
            db,
            report_id=report.id,
            exception_id=exception_id,
            action=str(form.get('action') or ''),
            reason=str(form.get('reason') or ''),
            unit_cost=Decimal(raw_cost) if raw_cost else None,
            actor_id=principal.id,
            ip=get_client_ip(request),
        )
        db.commit()
        message = (
            'Sale included with the owner-entered cost basis.'
            if exception.status == 'INCLUDED'
            else 'Sale excluded from this draft report.'
        )
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{report.id}#fifo-exceptions',
            message=message,
        )
    except (LookupError, ValueError, InvalidOperation) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{report.id}#fifo-exceptions',
            error=str(exc),
        )


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/discard')
async def funding_report_discard_action(
    account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db),
    _csrf: None = Depends(verify_csrf),
):
    account = db.get(FundingAccount, account_id)
    report = db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id:
        raise HTTPException(status_code=404)
    _action_gate(account)
    form = await request.form()
    try:
        delete_draft_report(
            db,
            report_id=report.id,
            actor_id=principal.id,
            reason=str(form.get('reason') or '') or 'Owner discarded a draft with FIFO exceptions.',
            ip=get_client_ip(request),
        )
        db.commit()
        return _back(
            f'/v2/funding-accounts/{account.id}#reports',
            message='Draft report discarded. Purchase orders, receipts, inventory, and Square sales were unchanged.',
        )
    except (LookupError, ValueError) as exc:
        db.rollback()
        return _back(
            f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc)
        )


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/finalize')
def funding_report_finalize_action(account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id); report=db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id: raise HTTPException(status_code=404)
    _action_gate(account)
    try:
        finalize_report(db, report_id=report.id, actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', message='Report finalized.')
    except (ValueError, LookupError) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/delete')
async def funding_report_delete_action(account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id); report=db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id: raise HTTPException(status_code=404)
    form=await request.form()
    try:
        delete_report(db, account_id=account.id, report_id=report.id,
            expected_token=str(form.get('expected_token') or ''), actor_id=principal.id,
            reason='Owner-confirmed permanent deletion', ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}#reports',
            message='Report permanently deleted.')
    except (ValueError, LookupError) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}#reports', error=str(exc))
    except SQLAlchemyError:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}#reports',
            error='The report could not be deleted. No records were changed; please try again.')


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/void')
async def funding_report_void_action(account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id); report=db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        void_report(db, report_id=report.id, reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', message='Report void recorded; history was preserved.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/adjustments')
async def funding_report_adjustment_action(account_id: int, report_id: int, request: Request,
    _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id); report=db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        add_adjustment(db, report_id=report.id, adjustment_type=str(form.get('adjustment_type') or ''),
            direction=str(form.get('direction') or ''), amount=Decimal(str(form.get('amount') or '')),
            effective_date=date.fromisoformat(str(form.get('effective_date') or '')), reason=str(form.get('reason') or ''),
            internal_note=str(form.get('internal_note') or ''), owner_confirmed=str(form.get('owner_confirmed') or '')=='1',
            actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', message='Report adjustment recorded.')
    except (ValueError, InvalidOperation) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc))


@router.post('/v2/funding-accounts/{account_id}/reports/{report_id}/adjustments/{adjustment_id}/reverse')
async def funding_report_adjustment_reverse_action(account_id: int, report_id: int, adjustment_id: int,
    request: Request, _feature: Principal = Depends(feature_access), principal: Principal = Depends(owner_access),
    db: Session = Depends(get_db), _csrf: None = Depends(verify_csrf)):
    account=db.get(FundingAccount, account_id); report=db.get(FundingReport, report_id)
    if account is None or report is None or report.account_id != account.id: raise HTTPException(status_code=404)
    _action_gate(account); form=await request.form()
    try:
        original=db.get(FundingReportAdjustment, adjustment_id)
        if original is None or original.report_id != report.id:
            raise ValueError('Adjustment does not belong to this report.')
        reverse_adjustment(db, adjustment_id=adjustment_id, reason=str(form.get('reason') or ''),
            actor_id=principal.id, ip=get_client_ip(request)); db.commit()
        return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', message='Adjustment reversal recorded.')
    except ValueError as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc))
