"""Photo revalidation must preserve staff authorization and survive session expiry."""
from types import SimpleNamespace
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app.services.flavor_explorer import photo_media,staff_access
from test_v2_primary import primary,login
from test_v2_scheduling_foundation import scheduling_db
from app.models import WebSession
from sqlalchemy import select
from datetime import datetime,timedelta,timezone

SHA='a'*64
PATH='/flavor-explorer/media/photos/'+SHA+'.webp'

def test_revalidation_checks_authorization_without_reading_the_blob_again(monkeypatch):
    calls=[];authorized=[True]
    def check(request):
        calls.append('auth')
        if not authorized[0]:raise HTTPException(403,'Denied')
    def asset(sha):
        calls.append('blob');return {'data':b'image bytes','mime':'image/webp'}
    monkeypatch.setattr(staff_access,'require_staff_view',check)
    app=FastAPI();app.include_router(photo_media.router)
    app.state.flavor_intelligence=SimpleNamespace(photos=SimpleNamespace(asset=asset))
    with TestClient(app) as client:
        first=client.get(PATH);assert first.status_code==200 and first.content==b'image bytes'
        assert first.headers['cache-control']=='private, no-cache'
        tag=first.headers['etag']
        again=client.get(PATH,headers={'If-None-Match':tag})
        assert again.status_code==304 and again.content==b''
        assert calls==['auth','blob','auth']
        authorized[0]=False
        assert client.get(PATH,headers={'If-None-Match':tag}).status_code==403
        assert calls==['auth','blob','auth','auth']

def test_expired_real_session_redirect_is_not_cached_and_photo_recovers_after_login(primary,monkeypatch):
    primary.account('ADMIN',linked=False)
    monkeypatch.setattr(primary.app.state,'flavor_intelligence',
        SimpleNamespace(photos=SimpleNamespace(asset=lambda sha:{'data':b'image bytes','mime':'image/webp'})),raising=False)
    assert login(primary).status_code==303
    first=primary.client.get(PATH);assert first.status_code==200
    with primary.Session() as db:
        session=db.scalar(select(WebSession).order_by(WebSession.id.desc()))
        session.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.commit()
    expired=primary.client.get(PATH,headers={'If-None-Match':first.headers['etag']})
    assert expired.status_code==303 and expired.headers['location'].startswith('/login')
    assert expired.headers['cache-control']=='no-store, private'
    assert login(primary).status_code==303
    recovered=primary.client.get(PATH,headers={'If-None-Match':first.headers['etag']})
    assert recovered.status_code==304
