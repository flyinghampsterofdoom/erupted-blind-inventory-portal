from datetime import datetime, timedelta, timezone

from app.services.v2_ordering_policy_service import (
    ConfidenceEvidence,
    DataFreshness,
    DataSourceEvidence,
    RecommendationActionability,
    RecommendationConfidence,
    assess_confidence,
    assess_freshness,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


def _source(hours: float, **kwargs) -> DataSourceEvidence:
    return DataSourceEvidence('inventory', NOW - timedelta(hours=hours), **kwargs)


def test_freshness_boundaries_are_exact_and_deterministic():
    fresh = assess_freshness((_source(24),), as_of=NOW)
    just_stale = assess_freshness((_source(24.01),), as_of=NOW)
    stale = assess_freshness((_source(72),), as_of=NOW)
    critical = assess_freshness((_source(72.01),), as_of=NOW)

    assert (fresh.status, fresh.actionability) == (DataFreshness.FRESH, RecommendationActionability.ACTIONABLE)
    assert (just_stale.status, just_stale.actionability) == (
        DataFreshness.STALE,
        RecommendationActionability.INFORMATIONAL,
    )
    assert stale.status == DataFreshness.STALE
    assert (critical.status, critical.actionability) == (
        DataFreshness.CRITICAL,
        RecommendationActionability.BLOCKED,
    )


def test_unavailable_or_incomplete_required_source_is_critical():
    unavailable = assess_freshness((DataSourceEvidence('sales', None, available=False),), as_of=NOW)
    incomplete = assess_freshness((_source(1, complete=False),), as_of=NOW)
    assert unavailable.status == DataFreshness.CRITICAL
    assert unavailable.reason_codes == ('SALES_UNAVAILABLE',)
    assert incomplete.status == DataFreshness.CRITICAL
    assert incomplete.reason_codes == ('INVENTORY_INCOMPLETE',)


def test_confidence_low_precedes_medium_and_high():
    result = assess_confidence(
        ConfidenceEvidence(new_product=True, null_par_inference=True, stockout_adjusted=True)
    )
    assert result.level == RecommendationConfidence.LOW
    assert result.reason_codes == ('NEW_PRODUCT',)


def test_confidence_medium_conditions_and_high_baseline():
    medium = assess_confidence(ConfidenceEvidence(null_par_inference=True, stockout_adjusted=True))
    high = assess_confidence(ConfidenceEvidence())
    assert medium.level == RecommendationConfidence.MEDIUM
    assert medium.reason_codes == ('NULL_PAR_DEMAND_INFERENCE', 'STOCKOUT_ADJUSTED_VELOCITY')
    assert high.level == RecommendationConfidence.HIGH
    assert high.reason_codes == ('COMPLETE_STABLE_INPUTS',)
