from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import ConsignmentSalesSyncState
from app.services.v2_consignment_facts_service import (
    ImportResult,
    synchronize_square_facts,
)

FRESH_FOR = timedelta(hours=24)
RUN_ABANDONED_AFTER = timedelta(minutes=30)
INCREMENTAL_OVERLAP = timedelta(hours=72)
INITIAL_LOOKBACK = timedelta(days=120)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SquareDataRefreshResult:
    started: bool
    state: str
    message: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    import_result: ImportResult | None = None


def square_data_status(db: Session, *, now: datetime | None = None) -> dict:
    captured_at = _utc(now or _now())
    state = db.get(ConsignmentSalesSyncState, 1)
    if state is None:
        return {
            "state": "never",
            "label": "Never updated",
            "last_successful_at": None,
            "coverage_start_at": None,
            "coverage_through_at": None,
            "last_error": None,
            "age_hours": None,
        }

    attempted_at = _utc(state.last_attempted_at) if state.last_attempted_at else None
    successful_at = _utc(state.last_successful_at) if state.last_successful_at else None
    if state.last_result == "RUNNING" and attempted_at is not None:
        if captured_at - attempted_at <= RUN_ABANDONED_AFTER:
            status = "updating"
            label = "Updating"
        else:
            status = "failed"
            label = "Update interrupted"
    elif state.last_result == "FAILED":
        status = "failed"
        label = "Update failed"
    elif successful_at is None:
        status = "never"
        label = "Never updated"
    elif captured_at - successful_at >= FRESH_FOR:
        status = "stale"
        label = "Stale"
    else:
        status = "current"
        label = "Current"

    age_hours = None
    if successful_at is not None:
        age_hours = max(0, int((captured_at - successful_at).total_seconds() // 3600))
    return {
        "state": status,
        "label": label,
        "last_successful_at": successful_at,
        "coverage_start_at": (
            _utc(state.last_successful_start_at)
            if state.last_successful_start_at
            else None
        ),
        "coverage_through_at": (
            _utc(state.last_successful_through_at)
            if state.last_successful_through_at
            else None
        ),
        "last_error": state.last_error if status == "failed" else None,
        "age_hours": age_hours,
    }


def square_data_needs_refresh(db: Session, *, now: datetime | None = None) -> bool:
    return square_data_status(db, now=now)["state"] in {"never", "stale", "failed"}


def _claim_refresh(*, actor_id: int, force: bool, now: datetime) -> bool:
    with SessionLocal() as db:
        state = db.execute(
            select(ConsignmentSalesSyncState)
            .where(ConsignmentSalesSyncState.id == 1)
            .with_for_update()
        ).scalar_one_or_none()
        if state is None:
            state = ConsignmentSalesSyncState(id=1, updated_by_principal_id=actor_id)
            db.add(state)
            db.flush()
        status = square_data_status(db, now=now)
        if status["state"] == "updating":
            db.rollback()
            return False
        if not force and status["state"] == "current":
            db.rollback()
            return False
        state.last_attempted_at = now
        state.last_result = "RUNNING"
        state.last_error = None
        state.updated_by_principal_id = actor_id
        db.commit()
        return True


def _refresh_bounds(
    *, start_at: datetime | None, end_at: datetime | None
) -> tuple[datetime, datetime]:
    captured_end = _utc(end_at or _now())
    if start_at is not None:
        return _utc(start_at), captured_end
    with SessionLocal() as db:
        state = db.get(ConsignmentSalesSyncState, 1)
        if state is not None and state.last_successful_through_at is not None:
            return _utc(
                state.last_successful_through_at
            ) - INCREMENTAL_OVERLAP, captured_end
    return captured_end - INITIAL_LOOKBACK, captured_end


def refresh_square_sales_data(
    *,
    actor_id: int,
    force: bool = False,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> SquareDataRefreshResult:
    now = _now()
    if not _claim_refresh(actor_id=actor_id, force=force, now=now):
        return SquareDataRefreshResult(
            False, "updating", "A Square data update is already running."
        )
    requested_start, requested_end = _refresh_bounds(start_at=start_at, end_at=end_at)
    try:
        with SessionLocal() as db:
            imported = synchronize_square_facts(
                db,
                start_at=requested_start,
                end_at=requested_end,
                actor_id=actor_id,
            )
            db.commit()
        return SquareDataRefreshResult(
            True,
            "current",
            (
                f"Square data updated: {imported.orders} orders, "
                f"{imported.sales_created} new sales, and "
                f"{imported.returns_created} new returns."
            ),
            requested_start,
            requested_end,
            imported,
        )
    except Exception as exc:
        error = str(exc)[:1000]
        with SessionLocal() as failed_db:
            state = failed_db.get(ConsignmentSalesSyncState, 1)
            if state is None:
                state = ConsignmentSalesSyncState(
                    id=1, updated_by_principal_id=actor_id
                )
                failed_db.add(state)
            state.last_result = "FAILED"
            state.last_error = error
            state.updated_by_principal_id = actor_id
            failed_db.commit()
        return SquareDataRefreshResult(
            True,
            "failed",
            f"Square data update failed: {error}",
            requested_start,
            requested_end,
        )


def refresh_square_sales_data_after_login(actor_id: int) -> None:
    refresh_square_sales_data(actor_id=actor_id, force=False)


def serializable_square_data_status(db: Session) -> dict:
    status = square_data_status(db)
    return {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in status.items()
    }
