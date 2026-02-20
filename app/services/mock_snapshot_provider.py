from __future__ import annotations

from decimal import Decimal

from app.services.snapshot_provider import CountItemInput


class MockSnapshotProvider:
    def __init__(self) -> None:
        self.catalog_by_category = {
            'LATTE': [
                ('VAR-001', 'SKU-1001', 'Erupted Latte', '12oz'),
                ('VAR-002', 'SKU-1002', 'Erupted Latte', '16oz'),
                ('VAR-007', 'SKU-1007', 'Lava Vanilla Latte', '12oz'),
            ],
            'COLD_BREW': [
                ('VAR-003', 'SKU-1003', 'Volcanic Cold Brew', '12oz'),
                ('VAR-004', 'SKU-1004', 'Volcanic Cold Brew', '16oz'),
                ('VAR-008', 'SKU-1008', 'Magma Nitro Brew', '16oz'),
            ],
            'MOCHA': [
                ('VAR-005', 'SKU-1005', 'Ash Mocha', '12oz'),
                ('VAR-006', 'SKU-1006', 'Ash Mocha', '16oz'),
                ('VAR-009', 'SKU-1009', 'Pyro Dark Mocha', '16oz'),
            ],
            'ESPRESSO': [
                ('VAR-010', 'SKU-1010', 'Core Espresso', 'Single Shot'),
                ('VAR-011', 'SKU-1011', 'Core Espresso', 'Double Shot'),
                ('VAR-012', 'SKU-1012', 'Ember Americano', '16oz'),
            ],
        }

    def _campaign_key(self, campaign_id: int) -> str:
        keys = sorted(self.catalog_by_category.keys())
        return keys[(campaign_id - 1) % len(keys)]

    def list_count_items(self, *, store_id: int, campaign_id: int) -> list[CountItemInput]:
        campaign_key = self._campaign_key(campaign_id)
        base = self.catalog_by_category[campaign_key]
        return [
            CountItemInput(
                variation_id=variation_id,
                sku=sku,
                item_name=item_name,
                variation_name=variation_name,
                source_catalog_version=f'mock-{campaign_key}-campaign-{campaign_id}',
            )
            for variation_id, sku, item_name, variation_name in base
        ]

    def fetch_current_on_hand(self, *, store_id: int, variation_ids: list[str]) -> dict[str, Decimal]:
        values: dict[str, Decimal] = {}
        for variation_id in variation_ids:
            checksum = sum(ord(char) for char in variation_id)
            base = (checksum % 11) + 4
            store_offset = store_id % 3
            values[variation_id] = Decimal(str(base + store_offset))
        return values
