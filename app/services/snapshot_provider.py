from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class CountItemInput:
    variation_id: str
    sku: str | None
    item_name: str
    variation_name: str
    source_catalog_version: str | None = None


class SnapshotProvider(Protocol):
    def list_count_items(self, *, store_id: int, campaign_id: int) -> list[CountItemInput]: ...

    def fetch_current_on_hand(self, *, store_id: int, variation_ids: list[str]) -> dict[str, Decimal]: ...
