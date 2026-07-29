from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_definitive_data_source_map_contains_mandatory_registry_and_gates():
    document = (ROOT / 'docs/v2/data-source-map.md').read_text()
    required = (
        '# V2 definitive data-source map',
        '## Source registry',
        'Canonical writer',
        'Missing behavior',
        '## Misleading, legacy, dormant, or parallel structures',
        '## Silent fallback audit',
        '## Unsupported fields and source ambiguities',
        '## Mandatory source declaration gate for future V2 work',
        '## Order Payments pre-deployment audit result',
        'purchase_order_store_allocations',
        'purchase_order_receipts',
        'V2_CONSIGNMENT_COGS_ACTIONS_ENABLED',
    )
    for value in required:
        assert value in document


def test_order_payment_reader_does_not_import_square_orders_or_v1_writers():
    source = (ROOT / 'app/services/v2_order_payments_service.py').read_text()
    assert 'SquareOrdersReader' not in source
    assert '/v2/orders/search' not in source
    for writer in (
        'submit_purchase_order',
        'save_purchase_order_received_quantities',
        'scan_purchase_order_barcode',
        'cancel_purchase_order_barcode_scan',
        'receive_purchase_order',
    ):
        assert writer not in source
    assert 'PurchaseOrderStoreAllocation' in source
    assert 'V1 store-allocation receipt rows' in source
    assert 'line_allocations or [None]' not in source


def test_internal_preview_templates_label_the_external_cogs_gate():
    template_dir = ROOT / 'app/templates/v2/order_payments'
    templates = {
        name: (template_dir / name).read_text()
        for name in ('consignment_vendor.html', 'attribution.html', 'report_preview.html')
    }
    assert 'Report creation is temporarily unavailable' in templates['consignment_vendor.html']
    assert 'Sales data tools are temporarily unavailable' in templates['attribution.html']
    assert 'Report actions are temporarily unavailable' in templates['report_preview.html']
    assert all('cogs_actions_enabled' in value for value in templates.values())
