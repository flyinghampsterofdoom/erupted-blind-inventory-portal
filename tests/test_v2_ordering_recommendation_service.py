from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.models import ParLevelSource
from app.services.purchase_order_math_service import LineMathInput, OrderingMathParams, compute_line_recommendation
from app.services.v2_ordering_normalization_service import (
    DailyQuantity,
    IncomingSupply,
    RawRecommendationCandidate,
    normalize_candidate,
)
from app.services.v2_ordering_policy_service import (
    DataFreshness,
    DataSourceEvidence,
    RecommendationActionability,
    RecommendationConfidence,
)
from app.services.v2_ordering_recommendation_service import calculate_recommendation
from app.services.v2_ordering_data_coordinator import OrderingDashboardData
from app.services.v2_ordering_view_model_service import dashboard_view


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


def _days(value: Decimal = Decimal('1'), count: int = 56):
    end = NOW.date() - timedelta(days=1)
    return tuple(DailyQuantity(end - timedelta(days=offset), value) for offset in range(count))


def _candidate(**overrides):
    values = dict(
        store_id=1,
        store_name='HWY99',
        vendor_id=2,
        vendor_name='Vendor',
        sku='SKU-1',
        variation_id='VAR-1',
        item_name='Item',
        variation_name='Variation',
        as_of=NOW,
        current_on_hand=Decimal('10'),
        inventory_valid=True,
        daily_sales=_days(),
        daily_inventory_deltas=(),
        sources=(
            DataSourceEvidence('catalog', NOW),
            DataSourceEvidence('inventory', NOW),
            DataSourceEvidence('sales', NOW),
            DataSourceEvidence('stockout_history', NOW),
        ),
        reorder_weeks=5,
        stock_up_weeks=10,
        manual_level=None,
        manual_target=None,
        manual_locked=False,
        par_is_manual=False,
        incoming_supply=(),
        non_sellable_quantity=None,
        non_sellable_resolved=False,
        product_created_at=NOW - timedelta(days=100),
        confirmed_discontinued=False,
        supporting_warnings=(),
    )
    values.update(overrides)
    return normalize_candidate(RawRecommendationCandidate(**values))


def test_phase1_matches_v1_raw_math_when_approved_policy_differences_do_not_apply():
    phase1 = calculate_recommendation(_candidate())
    v1 = compute_line_recommendation(
        LineMathInput(
            sku='SKU-1',
            current_on_hand=Decimal('10'),
            in_transit_qty=0,
            history_daily_units=[Decimal('1')] * 28,
            unit_pack_size=1,
            min_order_qty=0,
            par_source=ParLevelSource.DYNAMIC,
        ),
        OrderingMathParams(reorder_weeks=5, stock_up_weeks=10, history_lookback_days=28),
    )
    assert phase1.primary.adjusted_daily_velocity == Decimal('1.0000')
    assert phase1.suggested_reorder_level == v1.suggested_reorder_level == 35
    assert phase1.suggested_target_level == v1.suggested_stock_up_level == 70
    assert phase1.calculated_quantity == v1.raw_recommended_qty == 60


def test_zero_null_and_manual_lock_remain_distinct():
    zero = calculate_recommendation(
        _candidate(manual_level=0, manual_target=0, manual_locked=True, par_is_manual=True)
    )
    null = calculate_recommendation(_candidate())
    assert zero.effective_reorder_level == 0
    assert zero.effective_target_level == 0
    assert zero.calculated_quantity == 0
    assert 'MANUAL_INPUT_LOCKED' in {warning.code for warning in zero.warnings}
    assert 'NULL_PAR_DEMAND_INFERENCE' not in {warning.code for warning in zero.warnings}
    assert null.effective_target_level == 70
    assert 'NULL_PAR_DEMAND_INFERENCE' in {warning.code for warning in null.warnings}


def test_store_result_does_not_use_other_store_inventory():
    first = calculate_recommendation(_candidate(store_id=1, current_on_hand=10))
    second = calculate_recommendation(_candidate(store_id=1, current_on_hand=10))
    _unrelated = calculate_recommendation(_candidate(store_id=2, current_on_hand=1000))
    assert first == second
    assert first.calculated_quantity == 60


def test_stockout_days_are_removed_from_eligible_days():
    end = NOW.date() - timedelta(days=1)
    result = calculate_recommendation(
        _candidate(
            current_on_hand=0,
            daily_inventory_deltas=(DailyQuantity(end, Decimal('-5')),),
        )
    )
    assert result.primary.stockout_days == 1
    assert result.primary.eligible_days == 27
    assert result.primary.adjusted_daily_velocity == Decimal('1.0370')
    assert result.confidence == RecommendationConfidence.MEDIUM


def test_new_product_and_established_zero_sales_rules():
    new = calculate_recommendation(
        _candidate(
            daily_sales=_days(Decimal('0')),
            product_created_at=NOW - timedelta(days=5),
        )
    )
    established = calculate_recommendation(_candidate(daily_sales=_days(Decimal('0'))))
    assert new.calculated_quantity is None
    assert new.actionability == RecommendationActionability.BLOCKED
    assert new.confidence == RecommendationConfidence.LOW
    assert established.calculated_quantity == 0
    assert established.displayed_quantity == 0


def test_thirteen_eligible_zero_sales_days_are_insufficient_but_fourteen_are_accepted():
    thirteen = calculate_recommendation(
        _candidate(daily_sales=_days(Decimal('0')), product_created_at=NOW - timedelta(days=13))
    )
    fourteen = calculate_recommendation(
        _candidate(daily_sales=_days(Decimal('0')), product_created_at=NOW - timedelta(days=14))
    )
    assert thirteen.primary.eligible_days == 13
    assert thirteen.calculated_quantity is None
    assert fourteen.primary.eligible_days == 14
    assert fourteen.calculated_quantity == 0


def test_incoming_supply_and_non_sellable_quantity_are_explained():
    result = calculate_recommendation(
        _candidate(
            current_on_hand=20,
            incoming_supply=(IncomingSupply(9, 10, NOW - timedelta(days=31)),),
            non_sellable_quantity=5,
            non_sellable_resolved=True,
        )
    )
    assert result.sellable_on_hand == Decimal('15')
    assert result.incoming_supply == 10
    assert result.calculated_quantity == 45
    codes = {warning.code for warning in result.warnings}
    assert {'AGED_IN_TRANSIT_SUPPLY', 'NON_SELLABLE_SUBTRACTED'} <= codes


def test_discontinued_product_suppresses_actionable_quantity():
    result = calculate_recommendation(_candidate(confirmed_discontinued=True))
    assert result.calculated_quantity is None
    assert result.displayed_quantity is None
    assert result.actionability == RecommendationActionability.BLOCKED
    assert 'CONFIRMED_DISCONTINUED' in result.blocking_reasons


def test_stale_keeps_calculation_informational_and_critical_suppresses_display():
    stale_sources = (DataSourceEvidence('inventory', NOW - timedelta(hours=48)),)
    critical_sources = (DataSourceEvidence('inventory', NOW - timedelta(hours=73)),)
    fresh = calculate_recommendation(_candidate())
    stale = calculate_recommendation(_candidate(sources=stale_sources))
    critical = calculate_recommendation(_candidate(sources=critical_sources))

    assert fresh.freshness == DataFreshness.FRESH
    assert stale.freshness == DataFreshness.STALE
    assert stale.actionability == RecommendationActionability.INFORMATIONAL
    assert stale.calculated_quantity == fresh.calculated_quantity
    assert stale.displayed_quantity == fresh.calculated_quantity
    assert stale.confidence == RecommendationConfidence.LOW
    assert critical.freshness == DataFreshness.CRITICAL
    assert critical.calculated_quantity == fresh.calculated_quantity
    assert critical.displayed_quantity is None
    assert critical.actionability == RecommendationActionability.BLOCKED


def test_confidence_never_changes_calculated_quantity():
    high = calculate_recommendation(
        _candidate(manual_level=35, manual_target=70, par_is_manual=True)
    )
    medium = calculate_recommendation(_candidate())
    low = calculate_recommendation(_candidate(supporting_warnings=('MINOR_DATA_GAP',)))
    assert (high.confidence, medium.confidence, low.confidence) == (
        RecommendationConfidence.HIGH,
        RecommendationConfidence.MEDIUM,
        RecommendationConfidence.LOW,
    )
    assert high.calculated_quantity == medium.calculated_quantity == low.calculated_quantity == 60


def test_view_model_retains_policy_inputs_sources_warnings_and_blocking_reasons():
    critical = calculate_recommendation(
        _candidate(sources=(DataSourceEvidence('inventory', None, available=False, detail='timeout'),))
    )
    view = dashboard_view(OrderingDashboardData(NOW, (critical,)))
    row = view.rows[0]
    assert row.quantity == '—'
    assert row.freshness == 'CRITICAL'
    assert row.confidence == 'LOW'
    assert row.applied_policies == critical.applied_policies
    assert row.source_rows[0]['detail'] == 'timeout'
    assert row.warning_messages
    assert row.blocking_reasons == ('INVENTORY_UNAVAILABLE',)
    assert dict(row.explanation_inputs)['calculated_quantity'] == str(critical.calculated_quantity)
