"""Shared evidence rules. No replenishment mathematics and no Square writes."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = ZoneInfo('America/Los_Angeles')


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def quantity(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ValueError, InvalidOperation):
        return None


@dataclass(frozen=True)
class SalesCoverage:
    start_at: datetime
    end_at: datetime
    location_ids: tuple[str, ...]
    complete: bool
    observed_at: datetime
    reasons: tuple[str, ...] = ()

    def covers(self, start_at, end_at, locations) -> bool:
        return (self.complete and utc(self.start_at) <= utc(start_at)
                and utc(self.end_at) >= utc(end_at)
                and set(locations).issubset(self.location_ids))


@dataclass(frozen=True)
class InventoryEvidence:
    quantity: Decimal | None
    retrieved_at: datetime | None
    source_calculated_at: datetime | None = None
    reasons: tuple[str, ...] = ()

    def usable(self, *, as_of: datetime) -> Decimal | None:
        if self.quantity is None or self.retrieved_at is None or self.reasons:
            return None
        age = (utc(as_of) - utc(self.retrieved_at)).total_seconds()
        return self.quantity if 0 <= age <= 86400 else None


def record_run(db, *, source, status, started_at, completed_at, scope, evidence):
    from app.models import PurchasingEvidenceRun
    row = PurchasingEvidenceRun(source=source, status=status, started_at=utc(started_at),
        completed_at=utc(completed_at), scope=scope, evidence=evidence)
    db.add(row)
    db.flush()
    return row


def sales_coverage_for(db, *, start_at, end_at, location_ids, as_of):
    """Only durable completed runs can establish coverage, not an empty facts query."""
    from sqlalchemy import select
    from sqlalchemy import or_, and_
    from app.models import PurchasingEvidenceRun, PurchasingSaleCorrection, ConsignmentSaleFact
    from datetime import timedelta
    # Gross-unit source corrections never rewrite finalized financing. Financial
    # recommendations stay blocked until the existing owner review resolves them.
    correction_period=and_(
        PurchasingSaleCorrection.location_id.in_(location_ids),
        PurchasingSaleCorrection.business_date >= utc(start_at).astimezone(BUSINESS_TIMEZONE).date(),
        PurchasingSaleCorrection.business_date <= (utc(end_at)-timedelta(microseconds=1)).astimezone(BUSINESS_TIMEZONE).date())
    original_period=and_(
        ConsignmentSaleFact.square_location_id.in_(location_ids),
        ConsignmentSaleFact.transacted_at>=utc(start_at),ConsignmentSaleFact.transacted_at<utc(end_at))
    if db.scalar(select(PurchasingSaleCorrection.square_order_id).outerjoin(ConsignmentSaleFact,and_(
            PurchasingSaleCorrection.kind=='LINE',
            PurchasingSaleCorrection.square_order_id==ConsignmentSaleFact.square_order_id,
            PurchasingSaleCorrection.square_line_item_uid==ConsignmentSaleFact.square_line_item_uid)).where(
            PurchasingSaleCorrection.reason!='SOURCE_RESTORED',or_(correction_period,original_period)).limit(1)) is not None:
        return False
    if utc(end_at) <= utc(start_at) or not location_ids or any(not value for value in location_ids):
        return False
    rows = db.scalars(select(PurchasingEvidenceRun).where(PurchasingEvidenceRun.source == 'SALES')
                      .order_by(PurchasingEvidenceRun.completed_at.desc())).all()
    intervals = []
    for row in rows:
        # Old/updated-at envelopes do not prove when sales occurred. No backfill.
        if (row.evidence.get('source_filter') != 'CLOSED_AT'
                or row.evidence.get('coverage_contract') != 2
                or row.evidence.get('source_complete') is not True):
            continue
        if row.status != 'COMPLETE' or not 0 <= (utc(as_of)-utc(row.completed_at)).total_seconds() <= 86400:
            continue
        if not set(location_ids).issubset(row.scope.get('location_ids', [])):
            continue
        try:
            begin, end = (utc(datetime.fromisoformat(row.scope[name])) for name in ('start_at', 'end_at'))
        except (KeyError, TypeError, ValueError):
            continue
        intervals.append((begin, end))
    covered = utc(start_at)
    for begin, end in sorted(intervals):
        if begin > covered:
            break
        covered = max(covered, end)
    return covered >= utc(end_at)


def inventory_verification_runs(db, *, as_of):
    from datetime import timedelta
    from sqlalchemy import select
    from app.models import PurchasingEvidenceRun
    return db.scalars(select(PurchasingEvidenceRun).where(PurchasingEvidenceRun.source == 'INVENTORY',
        PurchasingEvidenceRun.completed_at >= utc(as_of)-timedelta(hours=24),
        PurchasingEvidenceRun.completed_at <= utc(as_of))
        .order_by(PurchasingEvidenceRun.completed_at.desc(), PurchasingEvidenceRun.id.desc())).all()


def inventory_pair_verified(db, *, variation_id, store_id, observation, as_of, runs=None, receipt_requirements=None):
    from sqlalchemy import select
    from app.models import PurchasingEvidenceRun
    if observation is None or InventoryEvidence(quantity(observation.counted_quantity), observation.refreshed_at).usable(as_of=as_of) is None:
        return False
    from app.services.purchasing_inventory_service import receipt_observation_requirements, receipt_observation_verified
    if receipt_requirements is None:
        receipt_requirements = receipt_observation_requirements(db, variation_ids=[variation_id], store_ids=[store_id])
    if not receipt_observation_verified(receipt_requirements, store_id=store_id, variation_id=variation_id,
            observed_at=observation.refreshed_at, calculated_at=getattr(observation, 'source_calculated_at', None)):
        return False
    rows = inventory_verification_runs(db, as_of=as_of) if runs is None else runs
    for row in rows:
        if variation_id in row.scope.get('variation_ids', []) and store_id in row.scope.get('store_ids', []):
            pair = [variation_id, store_id]
            return row.status != 'FAILED' and pair in row.evidence.get('observed_pairs', []) and utc(observation.refreshed_at) >= utc(row.completed_at)
    return False
