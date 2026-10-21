from pathlib import Path

from app.auth import Role
from app.services.access_control_service import fallback_allowed_for_role


ROOT = Path(__file__).resolve().parents[1]


def test_touchscreen_permissions_default_to_admin_and_manager_only():
    keys = (
        'touchscreen.view', 'touchscreen.manage_flavors', 'touchscreen.manage_categories',
        'touchscreen.manage_media', 'touchscreen.manage_mappings', 'touchscreen.manage_recommendations',
        'touchscreen.manage_devices', 'touchscreen.publish', 'touchscreen.preview', 'nav.touchscreen.all',
    )
    assert all(fallback_allowed_for_role(role=Role.ADMIN, permission_key=key) for key in keys)
    assert all(fallback_allowed_for_role(role=Role.MANAGER, permission_key=key) for key in keys)
    assert not any(fallback_allowed_for_role(role=Role.LEAD, permission_key=key) for key in keys)
    assert not any(fallback_allowed_for_role(role=Role.STORE, permission_key=key) for key in keys)


def test_public_touchscreen_has_no_v2_shell_and_required_controls():
    template = (ROOT / 'app/templates/touchscreen/app.html').read_text()
    assert 'v2-sidebar' not in template and 'data-touchscreen-app' in template
    assert template.count('data-reset') >= 2
    assert all(value in template for value in ('data-format="both"', 'data-format="salt"', 'data-format="freebase"'))
    assert 'data-detail' in template and 'data-detail-close' in template


def test_touchscreen_javascript_immediate_filters_persistence_and_inactivity_reset():
    script = (ROOT / 'app/static/v2/touchscreen.js').read_text()
    assert "state={format:'both'" in script
    assert "fetch('/touchscreen/api/catalog?'" in script
    assert 'history.pushState' in script and 'detail.showModal()' in script
    assert 'setTimeout(reset,minutes*60000)' in script
    assert "state.fruit.has(id)?state.fruit.delete(id):state.fruit.add(id)" in script


def test_customer_routes_are_employee_session_exempt_but_device_authenticated():
    sessions = (ROOT / 'app/security/sessions.py').read_text()
    router = (ROOT / 'app/routers/touchscreen.py').read_text()
    assert "request.url.path.startswith('/touchscreen/')" in sessions
    assert '_device_from_cookie' in router and 'load_touchscreen_device' in router
    assert 'store_id=device.store_id' in router
    assert "request.query_params.get('store_id'" not in router


def test_management_mutations_are_feature_capability_and_csrf_gated():
    router = (ROOT / 'app/routers/v2_touchscreen.py').read_text()
    assert "require_v2_feature('touchscreen_v2')" in router
    for key in ('manage_flavors', 'manage_categories', 'manage_media', 'manage_mappings', 'manage_recommendations', 'manage_devices', 'publish', 'preview'):
        assert f"touchscreen.{key}" in router
    assert router.count('Depends(verify_csrf)') >= 8


def test_customer_response_contract_omits_sensitive_fields_and_square_images():
    catalog = (ROOT / 'app/services/touchscreen_catalog_service.py').read_text()
    forbidden = ('unit_cost', 'wholesale', 'vendor_payment', 'employee_data', 'available_quantity\': inventory')
    assert not any(value in catalog for value in forbidden)
    assert 'square_image' not in catalog.lower()
    assert "f'/touchscreen/media/{asset.public_token}'" in catalog


def test_touchscreen_upload_has_its_own_limit_and_secure_shared_validator():
    router = (ROOT / 'app/routers/v2_touchscreen.py').read_text()
    media = (ROOT / 'app/services/touchscreen_media_service.py').read_text()
    assert 'len(content) > settings.touchscreen_max_upload_bytes' in router
    assert 'validate_image_upload' in media
    assert "storage_key = f'touchscreen/images/{image.content_hash}'" in media


def test_media_archival_checks_both_product_surfaces():
    signage_media = (ROOT / 'app/services/digital_signage_media_service.py').read_text()
    assert 'TouchscreenFlavorMedia' in signage_media
    assert 'reference_count or touchscreen_reference_count' in signage_media


def test_responsive_grid_and_no_horizontal_layout_contract():
    css = (ROOT / 'app/static/v2/touchscreen.css').read_text()
    assert 'grid-template-columns:repeat(4' in css
    assert '@media(max-width:1180px)' in css and '@media(max-width:850px)' in css and '@media(max-width:560px)' in css
    assert 'overflow-x:scroll' not in css and 'overflow-x:auto' not in css
