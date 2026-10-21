from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class DataFreshness(str, Enum):
    FRESH = 'FRESH'
    STALE = 'STALE'
    CRITICAL = 'CRITICAL'


class RecommendationConfidence(str, Enum):
    HIGH = 'HIGH'
    MEDIUM = 'MEDIUM'
    LOW = 'LOW'


class RecommendationActionability(str, Enum):
    ACTIONABLE = 'ACTIONABLE'
    INFORMATIONAL = 'INFORMATIONAL'
    BLOCKED = 'BLOCKED'


@dataclass(frozen=True)
class DataSourceEvidence:
    source: str
    observed_at: datetime | None
    available: bool = True
    complete: bool = True
    detail: str = ''


@dataclass(frozen=True)
class FreshnessAssessment:
    status: DataFreshness
    actionability: RecommendationActionability
    oldest_age_hours: float | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ConfidenceEvidence:
    new_product: bool = False
    sparse_history: bool = False
    incomplete_supporting_data: bool = False
    invalid_inventory: bool = False
    manual_assumption: bool = False
    reliability_warning: bool = False
    stale_or_critical: bool = False
    minor_warning: bool = False
    null_par_inference: bool = False
    stockout_adjusted: bool = False
    limited_history: bool = False


@dataclass(frozen=True)
class ConfidenceAssessment:
    level: RecommendationConfidence
    reason_codes: tuple[str, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def assess_freshness(
    sources: tuple[DataSourceEvidence, ...],
    *,
    as_of: datetime,
) -> FreshnessAssessment:
    """Apply the owner-approved 24/72-hour freshness boundaries."""
    now = _as_utc(as_of)
    reasons: list[str] = []
    ages: list[float] = []
    critical = False

    if not sources:
        return FreshnessAssessment(
            DataFreshness.CRITICAL,
            RecommendationActionability.BLOCKED,
            None,
            ('REQUIRED_DATA_UNAVAILABLE',),
        )

    for source in sources:
        code = source.source.upper().replace(' ', '_')
        if not source.available or source.observed_at is None:
            reasons.append(f'{code}_UNAVAILABLE')
            critical = True
            continue
        if not source.complete:
            reasons.append(f'{code}_INCOMPLETE')
            critical = True
        age_hours = max((now - _as_utc(source.observed_at)).total_seconds() / 3600, 0.0)
        ages.append(age_hours)
        if age_hours > 72:
            reasons.append(f'{code}_OLDER_THAN_72_HOURS')
            critical = True

    oldest = max(ages) if ages else None
    if critical:
        return FreshnessAssessment(
            DataFreshness.CRITICAL,
            RecommendationActionability.BLOCKED,
            oldest,
            tuple(dict.fromkeys(reasons)),
        )
    if oldest is not None and oldest > 24:
        stale_sources = [
            source.source.upper().replace(' ', '_') + '_STALE'
            for source in sources
            if source.observed_at is not None
            and max((now - _as_utc(source.observed_at)).total_seconds() / 3600, 0.0) > 24
        ]
        return FreshnessAssessment(
            DataFreshness.STALE,
            RecommendationActionability.INFORMATIONAL,
            oldest,
            tuple(stale_sources),
        )
    return FreshnessAssessment(
        DataFreshness.FRESH,
        RecommendationActionability.ACTIONABLE,
        oldest,
        (),
    )


def assess_confidence(evidence: ConfidenceEvidence) -> ConfidenceAssessment:
    """Classify data quality only. This result must never feed calculation logic."""
    low_conditions = (
        ('NEW_PRODUCT', evidence.new_product),
        ('SPARSE_HISTORY', evidence.sparse_history),
        ('INCOMPLETE_SUPPORTING_DATA', evidence.incomplete_supporting_data),
        ('INVALID_INVENTORY', evidence.invalid_inventory),
        ('MANUAL_ASSUMPTION', evidence.manual_assumption),
        ('RELIABILITY_WARNING', evidence.reliability_warning),
        ('STALE_OR_CRITICAL_DATA', evidence.stale_or_critical),
    )
    low_reasons = tuple(code for code, active in low_conditions if active)
    if low_reasons:
        return ConfidenceAssessment(RecommendationConfidence.LOW, low_reasons)

    medium_conditions = (
        ('MINOR_WARNING', evidence.minor_warning),
        ('NULL_PAR_DEMAND_INFERENCE', evidence.null_par_inference),
        ('STOCKOUT_ADJUSTED_VELOCITY', evidence.stockout_adjusted),
        ('LIMITED_BUT_SUFFICIENT_HISTORY', evidence.limited_history),
    )
    medium_reasons = tuple(code for code, active in medium_conditions if active)
    if medium_reasons:
        return ConfidenceAssessment(RecommendationConfidence.MEDIUM, medium_reasons)
    return ConfidenceAssessment(RecommendationConfidence.HIGH, ('COMPLETE_STABLE_INPUTS',))
