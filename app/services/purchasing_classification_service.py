"""COGS classification observes Square ownership, never the selected PO supplier."""
from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import select
from app.config import settings
from app.models import PaymentMethod, PurchasingProductAuthority, Vendor
from app.services.purchasing_evidence_service import utc


@dataclass(frozen=True)
class CogsClassification:
    category: str
    reason: str
    square_vendor_ids: tuple[str, ...]


def square_vendor_ids(variation_data: dict) -> tuple[str, ...]:
    infos = variation_data.get('item_variation_vendor_infos') or variation_data.get('item_variation_vendor_info_data') or []
    values = set()
    for info in infos:
        if not isinstance(info, dict):
            continue
        data = info.get('item_variation_vendor_info_data', info)
        if isinstance(data, dict) and str(data.get('vendor_id') or '').strip():
            values.add(str(data['vendor_id']).strip())
    return tuple(sorted(values))


def classify_product(db, *, variation_id: str, as_of: datetime) -> CogsClassification:
    row = db.get(PurchasingProductAuthority, variation_id)
    unknown = lambda reason: CogsClassification('UNKNOWN', reason, tuple(row.square_vendor_ids) if row else ())
    if row is None:
        return unknown('NO_SQUARE_VENDOR_EVIDENCE')
    if row.cash_payment_method_id is not None:
        method = db.get(PaymentMethod, row.cash_payment_method_id)
        if method and method.is_active and method.category in {'WIRE', 'DEBIT_CARD'} and method.last_four == '9671':
            return CogsClassification('CASH_9671', 'EXPLICIT_LOCAL_EXCEPTION', tuple(row.square_vendor_ids))
        return unknown('INVALID_CASH_EXCEPTION')
    age = (utc(as_of) - utc(row.observed_at)).total_seconds()
    if not row.source_complete or not 0 <= age <= 86400:
        return unknown('VENDOR_EVIDENCE_INCOMPLETE_OR_STALE')
    ids = tuple(sorted(set(row.square_vendor_ids)))
    if len(ids) != 1:
        return unknown('AMBIGUOUS_OR_MISSING_CANONICAL_VENDOR')
    vendor = db.scalar(select(Vendor).where(Vendor.square_vendor_id == ids[0], Vendor.active.is_(True)))
    if vendor is None:
        return unknown('UNVERIFIED_VENDOR')
    big, streamline = settings.purchasing_big_square_vendor_id, settings.purchasing_streamline_square_vendor_id
    if not big or not streamline or big == streamline:
        return unknown('CONSIGNMENT_IDENTITIES_NOT_CONFIGURED')
    category = 'BIG' if ids[0] == big else 'STREAMLINE' if ids[0] == streamline else 'CREDIT_CARD_POOL'
    return CogsClassification(category, 'VERIFIED_SQUARE_VENDOR', ids)
