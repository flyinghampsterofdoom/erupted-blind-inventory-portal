from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import Principal, Role
from app.config import settings
from app.models import PaymentMethod
from app.routers.v2_order_payments import (
    FEATURE_KEY,
    cogs_actions_access,
    feature_access,
    owner_access,
)
from app.security.csrf import verify_csrf
from app.services.v2_order_payments_service import (
    calculate_consignment_balance,
    masked_payment_method,
    oldest_first_allocation,
    portal_today,
    validate_payment_method,
    validate_report_email,
)


def test_payment_method_validation_covers_all_categories_and_rejects_sensitive_identifiers():
    for category in ('WIRE', 'CREDIT_CARD', 'DEBIT_CARD', 'CONSIGNMENT'):
        validate_payment_method(
            display_name='Configured method',
            category=category,
            last_four='4182',
            term_days=None,
        )
    validate_payment_method(
        display_name='Net 45',
        category='TERMS',
        last_four=None,
        term_days=45,
    )
    with pytest.raises(ValueError, match='positive term'):
        validate_payment_method(
            display_name='Bad terms',
            category='TERMS',
            last_four=None,
            term_days=0,
        )
    with pytest.raises(ValueError, match='final four'):
        validate_payment_method(
            display_name='Unsafe card',
            category='CREDIT_CARD',
            last_four='4111111111111111',
            term_days=None,
        )


def test_masked_display_never_exposes_more_than_last_four():
    method = PaymentMethod(
        display_name='Chase Business Checking',
        category='WIRE',
        last_four='4182',
        created_by_principal_id=1,
        updated_by_principal_id=1,
    )
    assert masked_payment_method(method) == 'Chase Business Checking •••• 4182'
    assert '••••' in masked_payment_method(method)


def test_vendor_report_email_validation_is_optional_but_strict():
    assert validate_report_email('') is None
    assert validate_report_email('  AP@example.com ') == 'ap@example.com'
    with pytest.raises(ValueError, match='valid report email'):
        validate_report_email('not-an-email')


def test_paid_date_default_uses_portal_local_date():
    assert portal_today(datetime(2026, 7, 28, 6, 30, tzinfo=timezone.utc)).isoformat() == '2026-07-27'


def test_rolling_balance_and_oldest_first_allocation_match_consignment_definition():
    allocations, excess = oldest_first_allocation(
        Decimal('3000'),
        [(10, Decimal('1000')), (11, Decimal('1400'))],
    )
    assert allocations == [(10, Decimal('1000.00')), (11, Decimal('1400.00'))]
    assert excess == Decimal('600.00')
    balance = calculate_consignment_balance(
        {
            'COGS_GENERATED': Decimal('2400'),
            'REPLENISHMENT_APPLIED': Decimal('2400'),
            'REPLENISHMENT_CREDIT_CREATED': excess,
        }
    )
    assert balance.unreplenished_cogs == Decimal('0.00')
    assert balance.available_replenishment_credit == Decimal('600.00')


def test_unreplenished_cogs_and_credit_are_never_misleadingly_negative():
    balance = calculate_consignment_balance(
        {
            'COGS_GENERATED': Decimal('100'),
            'REPLENISHMENT_APPLIED': Decimal('150'),
            'REPLENISHMENT_CREDIT_CREATED': Decimal('10'),
            'REPLENISHMENT_CREDIT_USED': Decimal('25'),
        }
    )
    assert balance.unreplenished_cogs == Decimal('0.00')
    assert balance.available_replenishment_credit == Decimal('0.00')


def test_feature_is_principal_scoped_and_owner_access_hard_denies_store(monkeypatch):
    owner = Principal(id=7, username='owner', role=Role.ADMIN, store_id=None, active=True)
    store = Principal(id=8, username='store', role=Role.STORE, store_id=1, active=True)
    monkeypatch.setattr(settings, 'v2_enabled_features', '')
    monkeypatch.setattr(settings, 'v2_principal_features', f'7:{FEATURE_KEY}')
    assert feature_access(owner) == owner
    with pytest.raises(HTTPException) as hidden:
        feature_access(store)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        owner_access(store)
    assert denied.value.status_code == 404


def test_external_cogs_actions_have_a_separate_default_off_gate(monkeypatch):
    monkeypatch.setattr(settings, 'v2_consignment_cogs_actions_enabled', False)
    with pytest.raises(HTTPException) as disabled:
        cogs_actions_access()
    assert disabled.value.status_code == 404
    monkeypatch.setattr(settings, 'v2_consignment_cogs_actions_enabled', True)
    assert cogs_actions_access() is None

    from app.main import app

    cogs_mutations = [
        route for route in app.routes
        if getattr(route, 'path', '').startswith('/v2/consignment')
        and getattr(route, 'methods', set()) == {'POST'}
        and '/adjustments' not in getattr(route, 'path', '')
    ]
    assert cogs_mutations
    for route in cogs_mutations:
        assert cogs_actions_access in [dependency.call for dependency in route.dependant.dependencies], route.path


def test_all_mutations_have_feature_owner_and_csrf_dependencies():
    from app.main import app

    mutations = [
        route
        for route in app.routes
        if getattr(route, 'path', '').startswith(
            ('/v2/order-payments', '/v2/payment-methods', '/v2/vendors/', '/v2/consignment')
        )
        and getattr(route, 'methods', set()) == {'POST'}
    ]
    assert mutations
    for route in mutations:
        calls = [dependency.call for dependency in route.dependant.dependencies]
        assert feature_access in calls, route.path
        assert owner_access in calls, route.path
        assert verify_csrf in calls, route.path


def test_authorized_owner_payment_methods_page_renders(monkeypatch):
    from app.routers import v2_order_payments as router

    owner = Principal(id=7, username='owner', role=Role.ADMIN, store_id=None, active=True)

    class Rows:
        def all(self):
            return []

    class Db:
        def scalars(self, _query):
            return Rows()

        def execute(self, _query):
            return Rows()

    class Templates:
        def TemplateResponse(self, name, context):
            return name, context

    monkeypatch.setattr(router, '_context', lambda _request, _principal, **values: values)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(templates=Templates())))
    name, context = router.payment_methods_page(request, owner, owner, Db())
    assert name == 'v2/order_payments/payment_methods.html'
    assert context['methods'] == []
    assert context['vendors'] == []


def test_consignment_copy_never_uses_paid_unpaid_balance_labels():
    root = Path(__file__).resolve().parents[1]
    for name in ('consignment.html', 'consignment_vendor.html'):
        text = (root / 'app/templates/v2/order_payments' / name).read_text(encoding='utf-8')
        assert 'Current amount paid' not in text
        assert 'Current amount outstanding' not in text
        assert 'Amount still to replace' in text
        assert 'Available credit' in text


def test_consignment_summary_uses_owner_facing_columns_and_empty_email_copy():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'app/templates/v2/order_payments/consignment.html').read_text(encoding='utf-8')
    for label in (
        'Vendor', 'Inventory on hand', 'COGS this cycle', 'Replenishment applied',
        'Amount still to replace', 'Available credit', 'Pending orders', 'Actions',
    ):
        assert f'<th>{label}</th>' in text
    assert 'Report email not set' in text
    assert 'COGS reporting is not yet enabled.' in text


def test_read_only_detail_has_no_mutating_form():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'app/templates/v2/order_payments/detail.html').read_text(encoding='utf-8')
    assert '<form' not in text
    assert 'saved purchase-order lines' in text.lower()
