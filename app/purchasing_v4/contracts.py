"""Explicit evidence and mathematical input contracts, not a purchasing engine."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from fractions import Fraction


class EvidenceState(str, Enum):
    KNOWN = 'KNOWN'
    UNKNOWN = 'UNKNOWN'
    UNAVAILABLE = 'UNAVAILABLE'


class Availability(str, Enum):
    AVAILABLE = 'CONFIRMED_AVAILABLE'
    STOCKOUT = 'CONFIRMED_STOCKOUT'
    UNKNOWN = 'UNKNOWN'


@dataclass(frozen=True)
class QuantityEvidence:
    state: EvidenceState
    value: Decimal | None
    source: str
    reason: str = ''
    observed_at: datetime | None = None
    references: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.state, EvidenceState):
            raise ValueError('An explicit evidence state is required')
        if self.state == EvidenceState.KNOWN:
            if not isinstance(self.value, Decimal) or not self.value.is_finite():
                raise ValueError('Known evidence requires an exact finite Decimal')
        elif self.value is not None:
            raise ValueError('Unknown/unavailable evidence cannot contain a numeric value')
        if not self.source or (self.state != EvidenceState.KNOWN and not self.reason):
            raise ValueError('Evidence needs source and unresolved reason')
        if self.observed_at is not None and self.observed_at.utcoffset() is None:
            raise ValueError('Evidence timestamps must be timezone aware')
        if not isinstance(self.references, tuple) or any(not isinstance(r,str) or not r for r in self.references):
            raise ValueError('Evidence references must be immutable nonempty strings')


@dataclass(frozen=True)
class ExposureDay:
    day: date
    availability: Availability
    sales: QuantityEvidence
    availability_reason: str = ''
    availability_references: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.day) is not date or not isinstance(self.availability,Availability) or not isinstance(self.sales,QuantityEvidence):
            raise ValueError('Dated, typed exposure evidence required')
        if not isinstance(self.availability_reason,str) or not isinstance(self.availability_references,tuple) or any(not isinstance(r,str) for r in self.availability_references):
            raise ValueError('Immutable exposure provenance required')


@dataclass(frozen=True)
class WindowContract:
    """No lookback, recent influence or sensitivity defaults are installed."""
    lookback_days: int
    recent_days: int
    minimum_available_days: int = 14

    def __post_init__(self):
        values = (self.lookback_days, self.recent_days, self.minimum_available_days)
        if any(type(value) is not int for value in values):
            raise ValueError('Window lengths must be integers')
        if not 0 < self.recent_days < self.lookback_days:
            raise ValueError('Recent window must be within historical lookback')
        if not 0 < self.minimum_available_days <= self.lookback_days:
            raise ValueError('Minimum history must fit the lookback')

    def split(self, cutoff: date):
        """Half-open completed-business-date windows; no overlapping observations."""
        return ((cutoff - timedelta(days=self.lookback_days),
                 cutoff - timedelta(days=self.recent_days)),
                (cutoff - timedelta(days=self.recent_days), cutoff))


@dataclass(frozen=True)
class CandidateModelInputs:
    """Explicit candidate proportions, no active profile or default weights.

    Configuration persistence remains later phase work. This input contract
    alone cannot generate a recommendation.
    """
    windows: WindowContract
    recent_influence: Fraction
    increasing_sensitivity: Fraction
    decreasing_sensitivity: Fraction
    increasing_limit: Fraction
    decreasing_limit: Fraction

    def __post_init__(self):
        if not isinstance(self.windows, WindowContract):
            raise ValueError('An explicit window contract is required')
        for value in (self.recent_influence, self.increasing_sensitivity,
                      self.decreasing_sensitivity, self.increasing_limit, self.decreasing_limit):
            if not isinstance(value, Fraction) or not 0 <= value <= 1:
                raise ValueError('Candidate proportions must be explicit exact values in [0,1]')


def confirmed_velocity(days: tuple[ExposureDay, ...], minimum_days: int) -> Fraction | None:
    """Reference contract only: no adaptive weights or replenishment decisions."""
    if type(minimum_days) is not int or minimum_days < 1:
        raise ValueError('Minimum reliable days must be positive')
    if len({row.day for row in days}) != len(days):
        raise ValueError('Duplicate exposure dates')
    eligible = [row for row in days if row.availability == Availability.AVAILABLE
                and row.sales.state == EvidenceState.KNOWN]
    if any(row.sales.value < 0 for row in days if row.sales.state == EvidenceState.KNOWN):
        raise ValueError('Gross observed sale quantities cannot be negative')
    if len(eligible) < minimum_days:
        return None
    return sum((Fraction(row.sales.value) for row in eligible), Fraction()) * 7 / len(eligible)


def final_pack_conversion(unit_need: Fraction, pack_size: int) -> int:
    """Cover each store's unit need with complete owner-defined distribution packs."""
    if not isinstance(unit_need, Fraction) or unit_need < 0:
        raise ValueError('Unit need must be an exact nonnegative Fraction')
    if type(pack_size) is not int or pack_size < 1:
        raise ValueError('Pack size must be a positive integer')
    if unit_need == 0:
        return 0
    denominator=pack_size*unit_need.denominator
    return (unit_need.numerator+denominator-1)//denominator


def classify_interval(opening: QuantityEvidence, deltas: tuple[Decimal, ...],
                      *, complete: bool, physical_count: bool = False) -> Availability:
    """Full-interval proof only; no endpoint-only availability inference.

    Caller must certify ordered, exhaustive movements over the interval. Mixed
    exposure, a count correction, incomplete coverage or a negative balance is
    unknown. This contract does not invent intraday timestamps from daily sums.
    """
    if not complete or physical_count or opening.state != EvidenceState.KNOWN:
        return Availability.UNKNOWN
    balance = opening.value
    values = [balance]
    for delta in deltas:
        if not isinstance(delta, Decimal) or not delta.is_finite():
            raise ValueError('Movement deltas must be exact finite Decimal')
        balance += delta
        values.append(balance)
    if min(values) > 0:
        return Availability.AVAILABLE
    if all(value == 0 for value in values):
        return Availability.STOCKOUT
    return Availability.UNKNOWN
