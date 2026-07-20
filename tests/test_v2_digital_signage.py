from __future__ import annotations

import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import date, datetime, time, timezone

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.auth import Principal, Role
from app.config import settings
from app.models import (
    DigitalSignageDisplay,
    DigitalSignageGroupDisplay,
    DigitalSignageGroupItem,
    DigitalSignageMediaAsset,
    Principal as PrincipalModel,
    PrincipalRole,
)
from app.routers.v2_digital_signage import remove_item_route
from app.schema_contract import HEAD_REVISION, upgrade_database
from app.security.display_sessions import DISPLAY_SESSION_COOKIE, create_display_session, load_display_session
from app.security.sessions import load_session_from_token
from app.security.passwords import verify_password
from app.services.access_control_service import fallback_allowed_for_role
from app.services.digital_signage_media_service import (
    MediaValidationError,
    store_or_reuse_image,
    validate_image_upload,
)
from app.services.digital_signage_service import (
    GroupInput,
    SignageValidationError,
    add_group_item,
    create_display,
    effective_playlist,
    reorder_group_items,
    reset_display_password,
    save_group,
    update_display,
)
from app.services.digital_signage_storage import InMemorySignageObjectStorage, StorageUnavailable, configured_signage_storage


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


def image_bytes(fmt='PNG', size=(1600, 900)) -> bytes:
    if fmt == 'WEBP':
        # Import the one optional encoder directly instead of making Pillow scan every plugin.
        from PIL import WebPImagePlugin  # noqa: F401
    out = io.BytesIO()
    Image.new('RGB', size, '#d84a35').save(out, format=fmt)
    return out.getvalue()


def test_image_validation_accepts_real_supported_images_and_checks_aspect_ratio(monkeypatch):
    monkeypatch.setattr(settings, 'digital_signage_max_upload_bytes', 5_000_000)
    image = validate_image_upload(filename='ad.png', browser_content_type='image/png', content=image_bytes())
    assert (image.content_type, image.width, image.height) == ('image/png', 1600, 900)
    assert image.approximately_widescreen is True
    portrait = validate_image_upload(
        filename='portrait.webp', browser_content_type='image/webp', content=image_bytes('WEBP', (600, 900))
    )
    assert portrait.approximately_widescreen is False


@pytest.mark.parametrize(
    ('filename', 'content_type', 'content', 'message'),
    [
        ('animation.zip', 'application/zip', b'PK-not-enabled', 'HTML animation packages are not enabled yet.'),
        ('animation.html', 'text/html', b'<html></html>', 'HTML animation packages are not enabled yet.'),
        ('fake.jpg', 'image/jpeg', b'not-an-image', 'not a valid supported image'),
        ('wrong.jpg', 'image/jpeg', image_bytes(), 'extension does not match'),
    ],
)
def test_invalid_and_html_uploads_are_rejected(filename, content_type, content, message):
    with pytest.raises(MediaValidationError, match=message):
        validate_image_upload(filename=filename, browser_content_type=content_type, content=content)


def test_media_storage_fails_closed_without_r2_configuration(monkeypatch):
    for name in ('r2_endpoint_url', 'r2_bucket_name', 'r2_access_key_id', 'r2_secret_access_key'):
        monkeypatch.setattr(settings, name, None)
    with pytest.raises(StorageUnavailable, match='not configured'):
        configured_signage_storage()


def test_signage_permissions_default_to_management_only():
    keys = (
        'digital_signage.view', 'digital_signage.manage_groups', 'digital_signage.manage_media',
        'digital_signage.manage_displays', 'digital_signage.reset_display_credentials', 'nav.digital_signage.all',
    )
    assert all(fallback_allowed_for_role(role=Role.ADMIN, permission_key=key) for key in keys)
    assert all(fallback_allowed_for_role(role=Role.MANAGER, permission_key=key) for key in keys)
    assert not any(fallback_allowed_for_role(role=Role.LEAD, permission_key=key) for key in keys)
    assert not any(fallback_allowed_for_role(role=Role.STORE, permission_key=key) for key in keys)


def test_daily_window_rejects_missing_equal_and_overnight_values():
    base = dict(name='Campaign', start_date=date(2026, 7, 20), end_date=None, priority=1, is_enabled=True, display_ids=())
    from app.services.digital_signage_service import validate_group_input
    with pytest.raises(SignageValidationError, match='both'):
        validate_group_input(GroupInput(daily_start_time=time(9), daily_end_time=None, **base))
    with pytest.raises(SignageValidationError, match='Overnight'):
        validate_group_input(GroupInput(daily_start_time=time(21), daily_end_time=time(6), **base))
    with pytest.raises(SignageValidationError, match='Overnight'):
        validate_group_input(GroupInput(daily_start_time=time(9), daily_end_time=time(9), **base))


def test_player_contract_rotates_locally_and_keeps_last_playlist():
    script = open('app/static/v2/display.js', encoding='utf-8').read()
    stylesheet = open('app/static/v2/display.css', encoding='utf-8').read()
    template = open('app/templates/display/player.html', encoding='utf-8').read()
    sessions = open('app/security/sessions.py', encoding='utf-8').read()
    assert "setTimeout(refresh, 300000)" in script
    assert 'localStorage.setItem' in script and 'Offline · showing saved rotation' in script
    assert 'preload(following.media_url' in script and 'item.permanent' in script
    assert 'new XMLHttpRequest()' in script and "request.open('GET', '/display/api/playlist', true)" in script
    assert not any(unsupported in script for unsupported in ('=>', 'async ', 'await ', '?.', '??', 'fetch(', 'const ', 'let '))
    assert 'inset:' not in stylesheet and '.display-stage{position:fixed;top:0;right:0;bottom:0;left:0' in stylesheet
    assert 'data-display-player' in template and 'v2-sidebar' not in template
    assert "{'/v2-assets/display.css', '/v2-assets/display.js'}" in sessions


@pytest.fixture
def signage_db():
    if not ADMIN_URL:
        pytest.skip('set TEST_POSTGRES_ADMIN_URL for Digital Signage PostgreSQL integration')
    admin = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_signage_{uuid.uuid4().hex[:10]}'
    database_url = f'{ADMIN_URL.rsplit("/", 1)[0]}/{database_name}'
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    upgrade_database(database_url)
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        model = PrincipalModel(username='manager', password_hash='unused', role=PrincipalRole.MANAGER, active=True)
        db.add(model); db.commit(); manager_id = model.id
    manager = Principal(id=manager_id, username='manager', role=Role.MANAGER, store_id=None, active=True)
    try:
        yield Session, manager, engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin.dispose()


def test_display_capacity_uniqueness_password_and_session_revocation(signage_db, monkeypatch):
    Session, manager, _engine = signage_db
    monkeypatch.setattr(settings, 'digital_signage_max_active_displays', 9)
    with Session() as db:
        displays = []
        for index in range(9):
            display, password = create_display(
                db, principal=manager, name=f'TV{index}', username=f'tv{index}', password=None,
                is_enabled=True, ip=None,
            )
            assert verify_password(password, display.password_hash)
            displays.append(display)
        db.commit()
        assert db.scalar(select(func.count(DigitalSignageDisplay.id))) == 9
    with Session() as db:
        with pytest.raises(SignageValidationError, match='Only 9'):
            create_display(db, principal=manager, name='TV10', username='tv10', password=None, is_enabled=True, ip=None)
        db.rollback()
        disabled, _ = create_display(db, principal=manager, name='TV-DISABLED', username='tv-disabled', password=None, is_enabled=False, ip=None)
        db.commit()
        assert disabled.is_enabled is False
    with Session() as db:
        first = db.execute(select(DigitalSignageDisplay).where(DigitalSignageDisplay.name == 'TV0')).scalar_one()
        token = create_display_session(db, display_id=first.id, ip=None, user_agent='test')
        db.commit()
        assert load_display_session(db, token)[1].id == first.id
        new_password = reset_display_password(db, display_id=first.id, principal=manager, password=None, ip=None)
        db.commit()
        assert verify_password(new_password, first.password_hash)
        assert load_display_session(db, token) is None


def test_media_dedup_group_assignments_and_permanent_playlist(signage_db):
    Session, manager, _engine = signage_db
    storage = InMemorySignageObjectStorage()
    image = validate_image_upload(filename='ad.png', browser_content_type='image/png', content=image_bytes())
    with Session() as db:
        one, _ = create_display(db, principal=manager, name='ONE', username='one', password=None, is_enabled=True, ip=None)
        two, _ = create_display(db, principal=manager, name='TWO', username='two', password=None, is_enabled=True, ip=None)
        asset, reused = store_or_reuse_image(db, principal=manager, image=image, storage=storage, ip=None)
        same, reused_again = store_or_reuse_image(db, principal=manager, image=image, storage=storage, ip=None)
        assert reused is False and reused_again is True and same.id == asset.id and storage.put_count == 1
        group = save_group(db, principal=manager, ip=None, value=GroupInput(
            name='Permanent', start_date=date(2026, 7, 1), end_date=None,
            daily_start_time=None, daily_end_time=None, priority=50, is_enabled=True,
            display_ids=(one.id, two.id),
        ))
        add_group_item(db, group_id=group.id, media_asset_id=asset.id, duration_seconds=None, is_permanent=True, principal=manager, ip=None)
        db.commit()
        assert db.scalar(select(func.count(DigitalSignageGroupDisplay.id))) == 2
        result = effective_playlist(db, display=one, now=datetime(2026, 7, 20, 18, tzinfo=timezone.utc))
        assert result['mode'] == 'PERMANENT' and len(result['items']) == 1
        assert result['items'][0]['media_url'].startswith('/display/media/')


def test_remove_and_reorder_group_items_without_constraint_violation(signage_db):
    Session, manager, _engine = signage_db
    storage = InMemorySignageObjectStorage()
    first_image = validate_image_upload(filename='first.png', browser_content_type='image/png', content=image_bytes())
    second_image = validate_image_upload(
        filename='second.png', browser_content_type='image/png', content=image_bytes(size=(1601, 900))
    )

    with Session() as db:
        first_asset, _ = store_or_reuse_image(db, principal=manager, image=first_image, storage=storage, ip=None)
        second_asset, _ = store_or_reuse_image(db, principal=manager, image=second_image, storage=storage, ip=None)
        group = save_group(db, principal=manager, ip=None, value=GroupInput(
            name='Removal regression', start_date=date(2026, 7, 1), end_date=None,
            daily_start_time=None, daily_end_time=None, priority=10, is_enabled=True, display_ids=(),
        ))
        first_item = add_group_item(
            db, group_id=group.id, media_asset_id=first_asset.id, duration_seconds=12,
            is_permanent=False, principal=manager, ip=None,
        )
        second_item = add_group_item(
            db, group_id=group.id, media_asset_id=second_asset.id, duration_seconds=12,
            is_permanent=False, principal=manager, ip=None,
        )
        db.commit()

        response = remove_item_route(
            group_id=group.id, item_id=first_item.id,
            request=Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': [], 'client': None}),
            _feature=manager, principal=manager, _csrf=None, db=db,
        )

        assert response.status_code == 303
        remaining = db.execute(select(DigitalSignageGroupItem).where(
            DigitalSignageGroupItem.advertisement_group_id == group.id
        )).scalars().all()
        assert [(item.id, item.sort_order) for item in remaining] == [(second_item.id, 0)]

        restored_item = add_group_item(
            db, group_id=group.id, media_asset_id=first_asset.id, duration_seconds=12,
            is_permanent=False, principal=manager, ip=None,
        )
        db.commit()
        reorder_group_items(
            db, group_id=group.id, ordered_ids=[restored_item.id, second_item.id], principal=manager, ip=None,
        )
        db.commit()

        reordered = db.execute(select(DigitalSignageGroupItem).where(
            DigitalSignageGroupItem.advertisement_group_id == group.id
        ).order_by(DigitalSignageGroupItem.sort_order)).scalars().all()
        assert [(item.id, item.sort_order) for item in reordered] == [
            (restored_item.id, 0), (second_item.id, 1),
        ]


def test_concurrent_duplicate_uploads_resolve_to_one_asset(signage_db):
    Session, manager, _engine = signage_db
    storage = InMemorySignageObjectStorage()
    image = validate_image_upload(filename='same.png', browser_content_type='image/png', content=image_bytes())
    barrier = Barrier(2)

    def upload_once():
        with Session() as db:
            barrier.wait()
            asset, reused = store_or_reuse_image(db, principal=manager, image=image, storage=storage, ip=None)
            db.commit()
            return asset.id, reused

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: upload_once(), range(2)))
    assert {asset_id for asset_id, _reused in results} == {results[0][0]}
    assert sorted(reused for _asset_id, reused in results) == [False, True]
    assert storage.put_count == 1
    with Session() as db:
        assert db.scalar(select(func.count(DigitalSignageMediaAsset.id))) == 1


def test_concurrent_active_display_limit_is_enforced(signage_db, monkeypatch):
    Session, manager, _engine = signage_db
    monkeypatch.setattr(settings, 'digital_signage_max_active_displays', 9)
    barrier = Barrier(10)

    def create_one(index: int):
        with Session() as db:
            barrier.wait()
            try:
                display, _password = create_display(
                    db, principal=manager, name=f'Concurrent TV {index}', username=f'concurrent-tv-{index}',
                    password=None, is_enabled=True, ip=None,
                )
                db.commit()
                return display.id
            except SignageValidationError:
                db.rollback()
                return None

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(create_one, range(10)))
    assert len([display_id for display_id in results if display_id is not None]) == 9
    with Session() as db:
        assert db.scalar(select(func.count(DigitalSignageDisplay.id))) == 9


@pytest.mark.skipif(os.getenv('RUN_REAL_R2_TESTS') != '1', reason='set RUN_REAL_R2_TESTS=1 for private R2 integration')
def test_real_r2_private_delivery_and_authentication_separation(signage_db):
    from app.routers.display import display_media, playlist

    Session, manager, _engine = signage_db
    storage = configured_signage_storage()
    image = validate_image_upload(
        filename='checkpoint-private.png', browser_content_type='image/png', content=image_bytes(size=(777, 333))
    )

    def request_with_cookie(name: str | None = None, value: str | None = None, etag: str | None = None):
        headers = []
        if name and value:
            headers.append((b'cookie', f'{name}={value}'.encode()))
        if etag:
            headers.append((b'if-none-match', etag.encode()))
        return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': headers, 'query_string': b''})

    with Session() as db:
        one, _ = create_display(db, principal=manager, name='R2 ONE', username='r2-one', password=None, is_enabled=True, ip=None)
        two, _ = create_display(db, principal=manager, name='R2 TWO', username='r2-two', password=None, is_enabled=True, ip=None)
        asset, reused = store_or_reuse_image(db, principal=manager, image=image, storage=storage, ip=None)
        same, reused_again = store_or_reuse_image(db, principal=manager, image=image, storage=storage, ip=None)
        assert reused is False and reused_again is True and same.id == asset.id
        assert asset.storage_key == f'digital-signage/images/{image.content_hash}'
        assert asset.original_filename not in asset.storage_key
        group = save_group(db, principal=manager, ip=None, value=GroupInput(
            name='R2 Assigned', start_date=date(2026, 1, 1), end_date=None,
            daily_start_time=None, daily_end_time=None, priority=10, is_enabled=True, display_ids=(one.id,),
        ))
        add_group_item(db, group_id=group.id, media_asset_id=asset.id, duration_seconds=12, is_permanent=False, principal=manager, ip=None)
        one_token = create_display_session(db, display_id=one.id, ip=None, user_agent='r2-checkpoint-one')
        two_token = create_display_session(db, display_id=two.id, ip=None, user_agent='r2-checkpoint-two')
        db.commit()

        with pytest.raises(HTTPException) as unauthenticated:
            display_media(asset.public_token, request_with_cookie(), db)
        assert unauthenticated.value.status_code == 401
        with pytest.raises(HTTPException) as employee_only:
            display_media(asset.public_token, request_with_cookie(settings.session_cookie_name, 'employee-cookie'), db)
        assert employee_only.value.status_code == 401
        with pytest.raises(HTTPException) as other_display:
            display_media(asset.public_token, request_with_cookie(DISPLAY_SESSION_COOKIE, two_token), db)
        assert other_display.value.status_code == 404
        assert load_session_from_token(db, one_token) is None

        playlist_response = playlist(request_with_cookie(DISPLAY_SESSION_COOKIE, one_token), db)
        playlist_body = playlist_response.body.decode()
        assert asset.storage_key not in playlist_body and asset.public_token in playlist_body
        assert playlist_response.headers['cache-control'] == 'private, no-cache'

        response = display_media(asset.public_token, request_with_cookie(DISPLAY_SESSION_COOKIE, one_token), db)
        assert response.body == image.content
        assert response.headers['content-type'] == 'image/png'
        assert response.headers['content-length'] == str(len(image.content))
        assert response.headers['etag'] == f'"{image.content_hash}"'
        assert response.headers['cache-control'].startswith('private,')
        assert response.headers['x-content-type-options'] == 'nosniff'
        conditional = display_media(
            asset.public_token, request_with_cookie(DISPLAY_SESSION_COOKIE, one_token, response.headers['etag']), db
        )
        assert conditional.status_code == 304 and conditional.body == b''

        update_display(
            db, display_id=one.id, principal=manager, name=one.name, slug=one.slug,
            username=one.username, is_enabled=False, ip=None,
        )
        db.commit()
        with pytest.raises(HTTPException) as disabled:
            display_media(asset.public_token, request_with_cookie(DISPLAY_SESSION_COOKIE, one_token), db)
        assert disabled.value.status_code == 401


def test_migration_head_includes_signage_revision():
    assert HEAD_REVISION == '20260720_0006'
