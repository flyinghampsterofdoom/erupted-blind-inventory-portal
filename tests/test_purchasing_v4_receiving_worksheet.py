"""Owner worksheet exercises existing receiving records with disposable Square simulation."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.purchasing_v4.receiving_app import create_app
from app.purchasing_v4.receiving_simulator import FixtureInventoryTransport, install
from app.purchasing_v4.receiving_square import InventoryAdapter
from app.purchasing_v4.receiving_worksheet_app import API_PATH, register
from app.security.passwords import hash_password
from test_purchasing_v4_receiving import receiving, purchasing, sheet, ready, engine, template, pytestmark


@pytest.fixture
def worksheet(receiving, monkeypatch):
    db, native_po, _ = receiving
    monkeypatch.setattr(settings, 'purchasing_owner_principal_id', 1)
    with db.begin() as c:
        c.execute(text("UPDATE principals SET password_hash=:password WHERE id=1"),
            {'password': hash_password('fixture-owner-password')})
        # The original legacy fixture uses explicit IDs.
        for table in ('purchase_order_lines', 'purchase_order_store_allocations'):
            c.exec_driver_sql(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))")
    install(db)
    adapter = InventoryAdapter(FixtureInventoryTransport(db))
    app = create_app(db.url, source_namespace='synthetic-fixture-source', owner_principal_id=1, inventory_adapter=adapter)
    register(app, app.state.engine, app.state.actor, adapter)
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        client.get('/login')
        headers = {'X-CSRF-Token': client.cookies['csrf_token']}
        assert client.post('/login', json=dict(username='v4-fixture', password='fixture-owner-password'), headers=headers).status_code == 200
        yield client, headers, db, native_po
    app.state.worker_engine.dispose()
    app.state.engine.dispose()


def shipments(client, headers, url, body):
    response = client.post(url+'/shipments', json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('native', [False, True])
def test_partial_then_overage_posts_only_entered_stores(worksheet, native):
    client, headers, db, native_po = worksheet
    po = native_po if native else 129
    url = f'{API_PATH}/orders/{po}'
    listing = client.get(API_PATH+'/orders')
    assert listing.status_code == 200, listing.text
    assert {po,129} <= {row['id'] for row in listing.json()['rows']}
    first = client.get(url)
    assert first.status_code == 200, first.text
    first = first.json()
    row = first['rows'][0]
    destination = next(store['id'] for store in first['stores'] if store['name'] == 'Longview')
    shipment = dict(request_key=str(uuid4()), version=first['version'], cells=[[row['id'], destination, 3]])
    assert client.post(url+'/shipments', json=shipment).status_code == 403
    original = shipments(client, headers, url, shipment)
    assert shipments(client, headers, url, shipment)['receipt_id'] == original['receipt_id']
    after = client.get(url).json()
    assert after['status'] == 'Partially Received'
    assert after['outstanding'] == first['outstanding'] - 3
    assert after['rows'][0]['requested'].get(str(destination), 0) == row['requested'].get(str(destination), 0) == 0
    assert after['rows'][0]['received'][str(destination)] == 3
    from app.services.purchasing_inventory_service import load_incoming_supply
    from sqlalchemy.orm import Session
    with Session(db) as session:
        incoming = [entry.quantity for entry in load_incoming_supply(session) if entry.purchase_order_id == po]
        assert sum(incoming) == after['outstanding']
    second = dict(request_key=str(uuid4()), version=after['version'],
        cells=[[row['id'], destination, after['outstanding']+2], [row['id'], 2, 0]])
    shipments(client, headers, url, second)
    final = client.get(url).json()
    assert final['status'] == 'Received'
    assert final['outstanding'] == 0
    assert final['previous'] == final['ordered']+2
    assert {(entry['store_id'],entry['quantity']) for entry in final['history'][0]['children']} == {
        (destination,after['outstanding']+2), (2,0)}
    with db.connect() as c:
        adjustments = c.execute(text("SELECT change->'adjustment' FROM purchasing_v4_simulator.adjustment")).scalars().all()
        assert len(adjustments) == 2
        assert {item.get('location_id') or item.get('to_location_id') for item in adjustments} == {'FIXTURE_LOC4'}


@pytest.mark.parametrize('native', [False, True])
def test_closure_keeps_stock_and_original_history(worksheet, native):
    client, headers, db, native_po = worksheet
    url = f'{API_PATH}/orders/{native_po if native else 129}'
    before = client.get(url).json()
    line = before['rows'][0]['id']
    shipments(client, headers, url, dict(request_key=str(uuid4()), version=before['version'], cells=[[line,2,2]]))
    counted = client.get(url).json()
    closure = dict(request_key=str(uuid4()), version=counted['version'])
    for _ in range(2):
        response = client.post(url+'/close', json=closure, headers=headers)
        assert response.status_code == 200, response.text
    after = client.get(url).json()
    assert after['status'] == 'Closed — Unfulfilled'
    assert after['closed'] == counted['outstanding']
    assert after['previous'] == counted['previous']
    assert after['rows'][0]['requested'] == before['rows'][0]['requested']
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM purchasing_v4_simulator.adjustment")).scalar_one() == 1


def test_native_restart_recovers_interrupted_and_skips_verified(worksheet, monkeypatch):
    from app.purchasing_v4 import receiving as native, receiving_worker as worker

    sends = []
    post = FixtureInventoryTransport.post

    def record_send(self, path, payload, **kwargs):
        if kwargs.get('inventory_quantity_write'):
            sends.append(payload['idempotency_key'])
        return post(self, path, payload, **kwargs)

    monkeypatch.setattr(FixtureInventoryTransport, 'post', record_send)
    client, headers, db, po = worksheet
    url = f'{API_PATH}/orders/{po}'
    before = client.get(url).json()
    line = before['rows'][0]['id']
    key = str(uuid4())
    with db.begin() as c:
        native.command(c, actor_id=1, command_id=key, purchase_order_id=po, expected_version=before['version'],
            kind='RECEIPT', lines=[dict(line_id=line, split={'1':4,'4':3})], reason='Send to Stores', confirmed=True, direct=True)
        commands = c.execute(text("SELECT id::text FROM purchasing_v4.inventory_command WHERE event_id=:event ORDER BY id"),
            {'event': key}).scalars().all()
    adapter = InventoryAdapter(FixtureInventoryTransport(db))
    assert worker.run(db,commands[0],adapter)['state'] == 'VERIFIED'

    def crash():
        raise KeyboardInterrupt('fixture worker interruption after simulated Square accepts')

    with pytest.raises(KeyboardInterrupt):
        worker.run(db,commands[1],adapter,after_remote=crash)
    assert client.get(url).json()['history'][0]['retry']
    for _ in range(2):
        response = client.post(url+f'/shipments/{key}/retry', json={}, headers=headers)
        assert response.status_code == 200, response.text
    assert client.get(url).json()['history'][0]['status'] == 'Posted to stores'
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM purchasing_v4_simulator.adjustment")).scalar_one() == 2
    assert sorted(sends) == commands


def test_legacy_uncertain_post_retries_the_original_payload(worksheet, monkeypatch):
    client, headers, db, _ = worksheet
    url = f'{API_PATH}/orders/129'
    order = client.get(url).json()
    line = order['rows'][0]['id']
    sends = []
    post = FixtureInventoryTransport.post

    def lose_first_reply(self, path, payload, **kwargs):
        result = post(self, path, payload, **kwargs)
        sends.append(payload)
        if len(sends) == 1:
            raise ConnectionError('Simulated connection loss after applying the adjustment')
        return result

    monkeypatch.setattr(FixtureInventoryTransport, 'post', lose_first_reply)
    body = dict(request_key=str(uuid4()), version=order['version'], cells=[[line,4,3]])
    receipt = shipments(client, headers, url, body)
    assert receipt['uncertain'] == 1
    assert client.get(url).json()['history'][0]['retry']
    assert shipments(client, headers, url, body)['receipt_id'] == receipt['receipt_id']
    assert sends[0] == sends[1]
    assert client.get(url).json()['previous'] == order['previous']+3
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM purchasing_v4_simulator.adjustment")).scalar_one() == 1
