from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.services.mock_snapshot_provider import MockSnapshotProvider
from app.services.square_snapshot_provider import SquareSnapshotProvider


@lru_cache(maxsize=1)
def get_snapshot_provider():
    provider = settings.snapshot_provider.strip().lower()
    if provider == 'square':
        return SquareSnapshotProvider()
    return MockSnapshotProvider()
