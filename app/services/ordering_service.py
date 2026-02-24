from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import OrderingMathSetting, VendorOrderingSetting


@dataclass(frozen=True)
class OrderingMathParams:
    reorder_weeks: int
    stock_up_weeks: int
    history_lookback_days: int


def _validate_math_params(*, reorder_weeks: int, stock_up_weeks: int, history_lookback_days: int) -> None:
    if reorder_weeks <= 0:
        raise ValueError('Reorder weeks must be greater than zero')
    if stock_up_weeks <= reorder_weeks:
        raise ValueError('Stock-up weeks must be greater than reorder weeks')
    if history_lookback_days < 7 or history_lookback_days > 730:
        raise ValueError('History lookback days must be between 7 and 730')


def get_or_create_ordering_math_settings(db: Session) -> OrderingMathSetting:
    row = db.execute(select(OrderingMathSetting).where(OrderingMathSetting.id == 1)).scalar_one_or_none()
    if row:
        return row

    row = OrderingMathSetting(
        id=1,
        default_reorder_weeks=settings.ordering_reorder_weeks_default,
        default_stock_up_weeks=settings.ordering_stock_up_weeks_default,
        default_history_lookback_days=settings.ordering_history_lookback_days_default,
    )
    _validate_math_params(
        reorder_weeks=row.default_reorder_weeks,
        stock_up_weeks=row.default_stock_up_weeks,
        history_lookback_days=row.default_history_lookback_days,
    )
    db.add(row)
    db.flush()
    return row


def resolve_effective_math_params(db: Session, *, vendor_id: int | None = None) -> OrderingMathParams:
    base = get_or_create_ordering_math_settings(db)
    reorder_weeks = base.default_reorder_weeks
    stock_up_weeks = base.default_stock_up_weeks
    history_lookback_days = base.default_history_lookback_days

    if vendor_id is not None:
        vendor_override = db.execute(
            select(VendorOrderingSetting).where(VendorOrderingSetting.vendor_id == vendor_id)
        ).scalar_one_or_none()
        if vendor_override:
            reorder_weeks = vendor_override.reorder_weeks
            stock_up_weeks = vendor_override.stock_up_weeks
            history_lookback_days = vendor_override.history_lookback_days

    _validate_math_params(
        reorder_weeks=reorder_weeks,
        stock_up_weeks=stock_up_weeks,
        history_lookback_days=history_lookback_days,
    )
    return OrderingMathParams(
        reorder_weeks=reorder_weeks,
        stock_up_weeks=stock_up_weeks,
        history_lookback_days=history_lookback_days,
    )
