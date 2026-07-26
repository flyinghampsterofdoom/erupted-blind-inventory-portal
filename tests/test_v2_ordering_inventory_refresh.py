from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services import v2_ordering_inventory_refresh_service as service
from app.services.v2_ordering_inventory_repository import InventoryExpectedScope, InventoryStoreIdentity
from app.services.v2_ordering_square_gateway import (
    SquareInventoryCount,
    SquareInventoryCountReadResult,
    SquareOrderingReadMetrics,
)


START = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
END = START + timedelta(seconds=2)


class Db:
    def flush(self):
        pass


def _scope(*, missing_location=False, duplicate=False):
    second_location = 'LOC-1' if duplicate else ('LOC-2' if not missing_location else None)
    return InventoryExpectedScope(
        ('VAR-1', 'VAR-2'),
        (
            InventoryStoreIdentity(1, 'Andresen', 'LOC-1'),
            InventoryStoreIdentity(2, 'Hazel Dell', second_location),
        ),
    )


def _setup(monkeypatch, scope):
    captured = {}
    monkeypatch.setattr(service, 'try_inventory_refresh_lock', lambda _db: True)
    monkeypatch.setattr(service, 'load_inventory_expected_scope', lambda _db: scope)
    monkeypatch.setattr(
        service,
        'persist_inventory_refresh',
        lambda _db, **kwargs: captured.update(kwargs) or setattr(kwargs['run'], 'id', 91),
    )
    monkeypatch.setattr(service, 'write_v2_audit_event', lambda *_args, **kwargs: captured.setdefault('audit', kwargs['event']))
    values = iter((START, END))
    return captured, lambda: next(values)


def test_complete_refresh_is_store_isolated_and_preserves_explicit_zero(monkeypatch):
    captured, clock = _setup(monkeypatch, _scope())
    counts = {
        (location, variation): SquareInventoryCount(location, variation, quantity, START)
        for location, variation, quantity in (
            ('LOC-1', 'VAR-1', Decimal('0')),
            ('LOC-2', 'VAR-1', Decimal('3')),
            ('LOC-1', 'VAR-2', Decimal('4')),
            ('LOC-2', 'VAR-2', Decimal('5')),
        )
    }
    gateway = SimpleNamespace(fetch_current_inventory_counts=lambda **_kwargs: SquareInventoryCountReadResult(
        counts, SquareOrderingReadMetrics(request_count=1)
    ))

    result = service.refresh_ordering_current_inventory(
        Db(), actor_principal_id=6, ip=None, gateway=gateway, clock=clock,
    )

    assert result.outcome == 'COMPLETE'
    assert (result.covered_pair_count, result.missing_pair_count, result.square_request_count) == (4, 0, 1)
    observations = captured['observations']
    assert {(row.store_id, row.square_variation_id): row.quantity for row in observations}[(1, 'VAR-1')] == 0
    assert captured['refreshed_at'] == END
    assert captured['audit'].correlation_id == result.correlation_id


def test_partial_refresh_preserves_missing_pairs_instead_of_writing_zero(monkeypatch):
    captured, clock = _setup(monkeypatch, _scope())
    count = SquareInventoryCount('LOC-1', 'VAR-1', Decimal('7'), START)
    gateway = SimpleNamespace(fetch_current_inventory_counts=lambda **_kwargs: SquareInventoryCountReadResult(
        {('LOC-1', 'VAR-1'): count}, SquareOrderingReadMetrics(request_count=2)
    ))

    result = service.refresh_ordering_current_inventory(
        Db(), actor_principal_id=6, ip=None, gateway=gateway, clock=clock,
    )

    assert result.outcome == 'PARTIAL'
    assert (result.covered_pair_count, result.missing_pair_count) == (1, 3)
    assert len(captured['observations']) == 1
    assert captured['observations'][0].quantity == 7


def test_failed_refresh_writes_run_but_no_current_observations(monkeypatch):
    captured, clock = _setup(monkeypatch, _scope())

    class Gateway:
        def fetch_current_inventory_counts(self, **_kwargs):
            raise RuntimeError('private Square detail')

        def current_metrics(self):
            return SquareOrderingReadMetrics(request_count=1)

    result = service.refresh_ordering_current_inventory(
        Db(), actor_principal_id=6, ip=None, gateway=Gateway(), clock=clock,
    )

    assert result.outcome == 'FAILED'
    assert captured['observations'] == ()
    assert result.error_summary == 'Square inventory read failed (RuntimeError).'
    assert 'private Square detail' not in result.error_summary


def test_missing_store_location_is_partial_and_duplicate_location_fails_closed(monkeypatch):
    captured, clock = _setup(monkeypatch, _scope(missing_location=True))
    counts = {
        ('LOC-1', variation): SquareInventoryCount('LOC-1', variation, Decimal('1'), START)
        for variation in ('VAR-1', 'VAR-2')
    }
    gateway = SimpleNamespace(fetch_current_inventory_counts=lambda **_kwargs: SquareInventoryCountReadResult(
        counts, SquareOrderingReadMetrics(request_count=1)
    ))
    partial = service.refresh_ordering_current_inventory(
        Db(), actor_principal_id=6, ip=None, gateway=gateway, clock=clock,
    )
    assert partial.outcome == 'PARTIAL' and partial.missing_pair_count == 2

    duplicate_captured, duplicate_clock = _setup(monkeypatch, _scope(duplicate=True))
    failed = service.refresh_ordering_current_inventory(
        Db(), actor_principal_id=6, ip=None, gateway=gateway, clock=duplicate_clock,
    )
    assert failed.outcome == 'FAILED'
    assert duplicate_captured['observations'] == ()


def test_overlapping_refresh_is_rejected_before_square(monkeypatch):
    monkeypatch.setattr(service, 'try_inventory_refresh_lock', lambda _db: False)
    with pytest.raises(service.InventoryRefreshInProgress):
        service.refresh_ordering_current_inventory(Db(), actor_principal_id=6, ip=None)
