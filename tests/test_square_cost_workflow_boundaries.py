from __future__ import annotations

import ast
from pathlib import Path

from app.models import VendorSkuConfig
from app.schema_contract import HEAD_REVISION


def _calls_named(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    return sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
        for node in ast.walk(tree)
    )


def test_destructive_mapping_sync_has_only_two_known_workflow_callers():
    callers = {}
    for path in Path('app').rglob('*.py'):
        count = _calls_named(path, 'sync_vendor_sku_configs_from_square')
        if count:
            callers[str(path)] = count
    assert callers == {
        'app/routers/management.py': 1,
        'app/services/purchase_order_admin_service.py': 1,
    }


def test_vendor_sync_and_both_po_generation_modes_use_cost_safe_sync():
    sync_source = Path(
        'app/services/square_ordering_data_service.py'
    ).read_text(encoding='utf-8')
    sync_body = sync_source.split(
        'def sync_vendor_sku_configs_from_square', 1
    )[1].split('\ndef ', 1)[0]
    assert 'existing.unit_cost = prior_default.unit_cost' in sync_body
    assert 'existing.unit_cost = _money_from_cents' not in sync_body
    assert 'existing.unit_cost = variation' not in sync_body
    assert 'existing.unit_cost = vdata' not in sync_body
    assert 'unit_cost=(prior_default.unit_cost if prior_default is not None else None)' in sync_body

    purchase_source = Path(
        'app/services/purchase_order_admin_service.py'
    ).read_text(encoding='utf-8')
    generation_body = purchase_source.split(
        'def generate_purchase_orders', 1
    )[1].split('\ndef ', 1)[0]
    assert 'sync_vendor_sku_configs_from_square' in generation_body
    assert '.unit_cost =' not in generation_body

    route_source = Path('app/routers/management.py').read_text(encoding='utf-8')
    assert "@router.post('/ordering-tool/generate')" in route_source
    assert "@router.post('/ordering-tool/generate-full-stock')" in route_source
    assert 'include_full_stock_lines=True' in route_source


def test_funding_consignment_and_refresh_workflows_do_not_invoke_mapping_sync():
    protected_sources = [
        Path('app/services/v2_consignment_facts_service.py'),
        Path('app/services/v2_funding_reports_service.py'),
        Path('app/services/v2_order_payments_service.py'),
        Path('app/services/v2_square_data_service.py'),
        Path('app/services/v2_ordering_catalog_service.py'),
        Path('app/services/v2_ordering_inventory_refresh_service.py'),
    ]
    for path in protected_sources:
        source = path.read_text(encoding='utf-8')
        assert 'sync_vendor_sku_configs_from_square' not in source
        assert 'VendorSkuConfig.unit_cost =' not in source


def test_only_explicit_owner_workflows_assign_saved_cost_attributes():
    assignments = []
    for path in Path('app').rglob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == 'unit_cost':
                    assignments.append((str(path), getattr(node, 'lineno', 0)))
    assert {path for path, _line in assignments} == {
        'app/services/purchase_order_admin_service.py',
        'app/services/square_ordering_data_service.py',
        'app/services/v2_funding_reports_service.py',
    }


def test_unknown_vendor_mapping_cost_is_supported_by_model_and_migration():
    column = VendorSkuConfig.__table__.c.unit_cost
    assert column.nullable is True
    assert column.server_default is None
    assert HEAD_REVISION == '20260923_0026'
    migration = Path(
        'migrations/versions/20260911_0024_square_cost_authority.py'
    ).read_text(encoding='utf-8')
    assert "nullable=True" in migration
    assert "server_default=None" in migration
    assert "WHERE unit_cost IS NULL" in migration
    assert "down_revision = '20260828_0028'" in migration
    upgrade = migration.split('def upgrade()', 1)[1].split('def downgrade()', 1)[0]
    assert 'UPDATE vendor_sku_configs' not in upgrade
