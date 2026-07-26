from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from time import perf_counter
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import OrderingInventoryRefreshRun
from app.services.v2_ordering_inventory_repository import (
    InventoryObservation,
    InventoryExpectedScope,
    load_inventory_expected_scope,
    persist_inventory_refresh,
    try_inventory_refresh_lock,
)
from app.services.v2_ordering_square_gateway import SquareOrderingReadGateway
from app.v2.audit import V2AuditEvent, write_v2_audit_event


COMPLETE = 'COMPLETE'
PARTIAL = 'PARTIAL'
FAILED = 'FAILED'
logger = logging.getLogger(__name__)


class InventoryRefreshInProgress(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderingInventoryRefreshResult:
    outcome: str
    correlation_id: str
    expected_variation_count: int
    active_store_count: int
    expected_pair_count: int
    covered_pair_count: int
    missing_pair_count: int
    square_request_count: int
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    error_code: str = ''
    error_summary: str = ''


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _duplicate_locations(scope: InventoryExpectedScope) -> tuple[str, ...]:
    by_location: dict[str, list[int]] = {}
    for store in scope.stores:
        if store.square_location_id:
            by_location.setdefault(store.square_location_id, []).append(store.store_id)
    return tuple(sorted(location for location, stores in by_location.items() if len(stores) > 1))


def refresh_ordering_current_inventory(
    db: Session,
    *,
    actor_principal_id: int,
    ip: str | None,
    gateway: SquareOrderingReadGateway | None = None,
    clock=lambda: datetime.now(tz=timezone.utc),
) -> OrderingInventoryRefreshResult:
    """Refresh Ordering-owned current counts without changing Square, V1, lifecycle, or touchscreen data."""
    if not try_inventory_refresh_lock(db):
        raise InventoryRefreshInProgress('An Ordering inventory refresh is already in progress.')

    started_at = _utc(clock())
    started_perf = perf_counter()
    correlation_id = str(uuid4())
    scope = load_inventory_expected_scope(db)
    expected_pairs = scope.expected_pair_count
    valid_stores = tuple(store for store in scope.stores if store.square_location_id)
    duplicate_locations = _duplicate_locations(scope)
    inventory_gateway = gateway or SquareOrderingReadGateway()
    observations: tuple[InventoryObservation, ...] = ()
    square_requests = 0
    error_code = ''
    error_summary = ''

    if duplicate_locations:
        outcome = FAILED
        error_code = 'DUPLICATE_STORE_LOCATION'
        error_summary = 'Two or more active stores share a Square location; no inventory rows were changed.'
    else:
        try:
            square_result = inventory_gateway.fetch_current_inventory_counts(
                location_ids=[str(store.square_location_id) for store in valid_stores],
                variation_ids=list(scope.variation_ids),
            )
            square_requests = square_result.metrics.request_count
            store_by_location = {str(store.square_location_id): store for store in valid_stores}
            expected_variations = set(scope.variation_ids)
            values: list[InventoryObservation] = []
            for (location_id, variation_id), count in sorted(square_result.counts.items()):
                store = store_by_location.get(location_id)
                if store is None or variation_id not in expected_variations:
                    continue
                values.append(
                    InventoryObservation(
                        square_variation_id=variation_id,
                        store_id=store.store_id,
                        square_location_id=location_id,
                        quantity=count.quantity,
                        source_calculated_at=count.calculated_at,
                    )
                )
            observations = tuple(values)
            covered = len(observations)
            missing = max(0, expected_pairs - covered)
            if missing == 0:
                outcome = COMPLETE
            elif covered:
                outcome = PARTIAL
                error_code = 'PARTIAL_COVERAGE'
                error_summary = f'{missing} expected active-store inventory pair(s) were not returned.'
            else:
                outcome = FAILED
                error_code = 'NO_COVERAGE'
                error_summary = 'Square returned no usable expected active-store inventory pairs.'
        except Exception as exc:
            failed_metrics = inventory_gateway.current_metrics() if hasattr(inventory_gateway, 'current_metrics') else None
            square_requests = failed_metrics.request_count if failed_metrics else 0
            outcome = FAILED
            error_code = f'SQUARE_{type(exc).__name__.upper()}'[:64]
            error_summary = f'Square inventory read failed ({type(exc).__name__}).'

    completed_at = _utc(clock())
    covered_pairs = len(observations) if outcome != FAILED else 0
    missing_pairs = max(0, expected_pairs - covered_pairs)
    if outcome == FAILED:
        observations = ()
    run = OrderingInventoryRefreshRun(
        correlation_id=correlation_id,
        result=outcome,
        expected_variation_count=len(scope.variation_ids),
        active_store_count=len(scope.stores),
        expected_pair_count=expected_pairs,
        covered_pair_count=covered_pairs,
        missing_pair_count=missing_pairs,
        square_request_count=square_requests,
        started_at=started_at,
        completed_at=completed_at,
        error_code=error_code or None,
        error_summary=error_summary or None,
        refreshed_by_principal_id=actor_principal_id,
    )
    persist_inventory_refresh(
        db,
        run=run,
        observations=observations,
        refreshed_at=completed_at,
    )
    duration = perf_counter() - started_perf
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=actor_principal_id,
            action=f'inventory_refresh_{outcome.casefold()}',
            domain='ordering_inventory',
            entity_type='inventory_refresh_run',
            entity_id=run.id,
            store_ids=tuple(sorted(store.store_id for store in scope.stores)),
            timestamp=completed_at,
            correlation_id=correlation_id,
            external_outcome={
                'result': outcome,
                'square_request_count': square_requests,
                'error_code': error_code or None,
            },
            metadata={
                'expected_variation_count': len(scope.variation_ids),
                'active_store_count': len(scope.stores),
                'expected_pair_count': expected_pairs,
                'covered_pair_count': covered_pairs,
                'missing_pair_count': missing_pairs,
                'duration_seconds': round(duration, 6),
            },
        ),
        ip=ip,
    )
    db.flush()
    logger.info(
        'v2_ordering_inventory_refresh_metrics',
        extra={
            'ordering_inventory_metrics': {
                'outcome': outcome,
                'expected_variation_count': len(scope.variation_ids),
                'active_store_count': len(scope.stores),
                'expected_pair_count': expected_pairs,
                'covered_pair_count': covered_pairs,
                'missing_pair_count': missing_pairs,
                'square_request_count': square_requests,
                'refresh_duration_seconds': duration,
                'correlation_id': correlation_id,
            }
        },
    )
    return OrderingInventoryRefreshResult(
        outcome=outcome,
        correlation_id=correlation_id,
        expected_variation_count=len(scope.variation_ids),
        active_store_count=len(scope.stores),
        expected_pair_count=expected_pairs,
        covered_pair_count=covered_pairs,
        missing_pair_count=missing_pairs,
        square_request_count=square_requests,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        error_code=error_code,
        error_summary=error_summary,
    )
