from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
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
    calculate_report,
    catalog_rows,
    create_funding_account,
    delete_draft_report,
    finalize_report,
    overlapping_reports,
    record_ledger_entry,
    record_payment,
    report_position,
    reverse_adjustment,
    reverse_ledger_entry,
    reverse_payment,
    set_funding_account_status,
    update_credit_terms,
    void_report,
)


router = APIRouter()


def _back(path: str, *, message: str = '', error: str = '') -> RedirectResponse:
    query = f'?message={quote(message)}' if message else (f'?error={quote(error)}' if error else '')
    return RedirectResponse(f'{path}{query}', status_code=303)


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
        _funding_context(request, principal, label='SKU Mappings', path='/v2/funding-accounts/mappings',
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
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_report_new.html',
        _funding_context(request, principal, label='Create Report', path='/v2/funding-accounts/reports/new',
            accounts=accounts, report_stores=stores, overlaps=[], submitted={}, today=date.today()))


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
        overlaps=overlapping_reports(db, account_id=account.id, start_date=start_date, end_date=end_date)
        acknowledged=str(form.get('overlap_acknowledged') or '') == '1'
        if overlaps and not acknowledged:
            accounts=db.scalars(select(FundingAccount).where(FundingAccount.is_active.is_(True)).order_by(
                FundingAccount.account_type, FundingAccount.display_name)).all()
            stores=db.scalars(select(Store).where(Store.active.is_(True)).order_by(Store.name)).all()
            return request.app.state.templates.TemplateResponse('v2/order_payments/funding_report_new.html',
                _funding_context(request, principal, label='Create Report', path='/v2/funding-accounts/reports/new',
                    accounts=accounts, report_stores=stores, overlaps=overlaps, submitted=submitted,
                    submitted_store_ids=[int(v) for v in form.getlist('store_ids')], today=date.today()))
        report=calculate_report(db, account_id=account.id, start_date=start_date, end_date=end_date,
            store_ids=[int(v) for v in form.getlist('store_ids')], sku_filter=str(form.get('sku_filter') or ''),
            internal_note=str(form.get('internal_note') or ''), overlap_acknowledged=acknowledged,
            actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return RedirectResponse(f'/v2/funding-accounts/{account.id}/reports/{report.id}', status_code=303)
    except (ValueError, TypeError, InvalidOperation) as exc:
        db.rollback(); return _back('/v2/funding-accounts/reports/new', error=str(exc))


@router.get('/v2/funding-accounts/{account_id}')
def funding_account_detail_page(account_id: int, request: Request, _feature: Principal = Depends(feature_access),
    principal: Principal = Depends(owner_access), db: Session = Depends(get_db)):
    try: summary=account_summary(db, account_id=account_id)
    except LookupError as exc: raise HTTPException(status_code=404) from exc
    actors={row.id: row.username for row in db.scalars(select(PrincipalRecord)).all()}
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_account_detail.html',
        _funding_context(request, principal, label=summary['account'].display_name,
            path=f'/v2/funding-accounts/{account_id}', summary=summary, actors=actors, today=date.today()))


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
    _action_gate(account); form=await request.form(); allocations={}
    for report_id, amount in zip(form.getlist('allocation_report_id'), form.getlist('allocation_amount')):
        if str(report_id).strip() and str(amount).strip(): allocations[int(report_id)]=Decimal(str(amount))
    try:
        record_payment(db, account_id=account.id, entry_type=str(form.get('entry_type') or 'PAYMENT'),
            amount=Decimal(str(form.get('amount') or '')), payment_date=date.fromisoformat(str(form.get('payment_date') or '')),
            payment_source=str(form.get('payment_source') or ''), confirmation_number=str(form.get('confirmation_number') or ''),
            reason=str(form.get('reason') or ''), internal_note=str(form.get('internal_note') or ''),
            allocations=allocations, actor_id=principal.id, ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='Settlement activity recorded.')
    except (ValueError, InvalidOperation) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}', error=str(exc))


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
    lines=db.scalars(select(FundingReportLine).where(FundingReportLine.report_id==report.id).order_by(
        FundingReportLine.product_name_snapshot, FundingReportLine.store_id)).all()
    exclusions=db.scalars(select(FundingReportExclusion).where(FundingReportExclusion.report_id==report.id).order_by(
        FundingReportExclusion.reason_code, FundingReportExclusion.id)).all()
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
    purchase_order_source_rows=[{**row, **line_totals.get(f"PO_LINE:{row.get('purchase_order_line_id')}", {
        'units_sold': Decimal('0'), 'units_returned': Decimal('0'),
        'net_units': Decimal('0'), 'calculated_cogs': Decimal('0'),
    })} for row in source_scope.get('source_lines', [])]
    selected_stores=db.scalars(select(Store).where(Store.id.in_(report.store_ids or [-1])).order_by(Store.name)).all()
    return request.app.state.templates.TemplateResponse('v2/order_payments/funding_report_detail.html',
        _funding_context(request, principal, label=f'Report {report.report_number}',
            path=f'/v2/funding-accounts/{account.id}/reports/{report.id}', account=account, report=report,
            lines=lines, exclusions=exclusions, links=links, sales=sales, returns=returns,
            position=position, adjustment_rows=adjustment_rows, overlaps=overlaps,
            payment_allocations=payment_allocations, payment_rows=payment_rows,
            reversed_adjustment_ids=reversed_adjustment_ids, reversed_payment_ids=reversed_payment_ids,
            adjustment_types=sorted(ADJUSTMENT_TYPES), actors=actors, today=date.today(),
            source_scope=source_scope, purchase_order_source_rows=purchase_order_source_rows,
            selected_store_names=[row.name for row in selected_stores]))


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
        delete_draft_report(db, report_id=report.id, actor_id=principal.id,
            reason=str(form.get('reason') or ''), ip=get_client_ip(request))
        db.commit(); return _back(f'/v2/funding-accounts/{account.id}', message='Draft report deleted.')
    except (ValueError, LookupError) as exc:
        db.rollback(); return _back(f'/v2/funding-accounts/{account.id}/reports/{report.id}', error=str(exc))


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
