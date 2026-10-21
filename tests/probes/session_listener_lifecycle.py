"""Fresh-interpreter probe; launched by test_session_listener_lifecycle.py only."""
import json
import os
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, event, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

url = make_url(os.environ['DATABASE_URL'])
assert url.host in {'127.0.0.1', 'localhost'}
assert url.database.startswith('erupted_purchasing_p1_template_')
connect = socket.socket.connect


def local_only(self, address):
    if isinstance(address, tuple):
        assert address[0] in {'127.0.0.1', 'localhost', '::1'}, 'External network forbidden'
    return connect(self, address)


socket.socket.connect = local_only
mode, entry = sys.argv[1:]
if entry == 'database':
    from app.db import SessionLocal
    from app import models as m
    engine = SessionLocal.kw['bind']
else:
    from app import models as m
    engine = create_engine(url)
    from sqlalchemy.orm import sessionmaker
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# No prior request or service import may make this a warm-start test.
assert 'app.services.access_control_service' not in sys.modules
assert 'app.services.v2_scheduling_policy_service' not in sys.modules
registered = []
listen = event.api.listen


def track(target, identifier, callback, *args, **kwargs):
    if target is Session:
        registered.append((identifier, callback.__module__, callback.__name__))
    return listen(target, identifier, callback, *args, **kwargs)


event.api.listen = track


def scheduling_write(db, number):
    row = m.Store(name='Cold-start synthetic store ' + str(number), active=True)
    db.add(row)
    db.flush()  # Original candidate fails here, importing permissions during flush.
    row.active = False
    db.flush()
    assert db.scalar(select(m.Store.active).where(m.Store.id == row.id)) is False
    db.rollback()
    db.add(m.Store(name='After rollback ' + str(number), active=True))
    db.flush()
    db.rollback()


def permission_write(db, number):
    principal = m.Principal(username='cold-principal-' + str(number),
                            password_hash='synthetic', role='ADMIN', active=True)
    db.add(principal)
    db.flush()
    db.info['permission_override_maps'] = ('sentinel',)
    override = m.PrincipalPermissionOverride(principal_id=principal.id,
        permission_key='management.access', allowed=False,
        updated_by_principal_id=principal.id)
    db.add(override)
    db.flush()
    assert 'permission_override_maps' not in db.info
    from app.auth import Principal, Role
    from app.services.access_control_service import effective_permission_flags, principal_has_permission
    actor = Principal(principal.id, principal.username, Role.ADMIN, None, True)
    assert not effective_permission_flags(db, principal=actor)['management.access']
    with db.begin_nested() as nested:
        override.allowed = True
        db.flush()
        assert effective_permission_flags(db, principal=actor)['management.access']
        nested.rollback()
    assert not principal_has_permission(db, principal=actor,
        permission_key='management.access', fallback_allowed=True)
    override.allowed = True
    db.flush()
    assert principal_has_permission(db, principal=actor,
        permission_key='management.access', fallback_allowed=False)
    effective_permission_flags(db, principal=actor)
    db.execute(update(m.PrincipalPermissionOverride).where(
        m.PrincipalPermissionOverride.id == override.id).values(allowed=False))
    assert 'permission_override_maps' not in db.info
    assert not principal_has_permission(db, principal=actor,
        permission_key='management.access', fallback_allowed=True)
    db.rollback()
    scheduling_write(db, number)


if mode == 'requests':
    from fastapi import Depends, FastAPI
    from starlette.testclient import TestClient
    from app.session_events import install_session_listeners
    app = FastAPI()
    barrier = Barrier(8)
    held = []

    def get_db():
        with SessionLocal() as db:
            held.append(db)  # Retain identities until all responses are checked.
            yield db

    @app.get('/first/{number}')
    def first(number: int, db=Depends(get_db)):
        barrier.wait(timeout=20)
        install_session_listeners()  # Concurrent, idempotent initialization.
        scheduling_write(db, number)
        permission_write(db, number)
        return {'session': id(db), 'number': number}

    with TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda n: client.get('/first/' + str(n)), range(8)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()['session'] for r in responses}) == 8
    assert {r.json()['number'] for r in responses} == set(range(8))
else:
    with SessionLocal() as db:
        if mode == 'permission':
            permission_write(db, 0)
        else:
            scheduling_write(db, 0)
            permission_write(db, 0)
        from app.session_events import _LISTENERS, install_session_listeners
        counts = tuple(len(tuple(getattr(db.dispatch, name))) for name, _, _ in _LISTENERS)
        for number in range(3):
            install_session_listeners()
            scheduling_write(db, number + 1)
            permission_write(db, number + 1)
        assert counts == tuple(len(tuple(getattr(db.dispatch, name))) for name, _, _ in _LISTENERS)
        assert all(event.contains(Session, name, callback) for name, callback, _ in _LISTENERS)

assert registered == [], registered
engine.dispose()
print(json.dumps({'mode': mode, 'entry': entry, 'status': 'PASS',
                  'late_listener_registrations': registered,
                  'all_writes_rolled_back': True}))
