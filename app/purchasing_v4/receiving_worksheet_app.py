"""One owner worksheet over the existing legacy and V4 receiving services."""
import json
from pathlib import Path
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, StrictInt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import PurchaseOrder, Vendor
from app.security.csrf import verify_csrf
from app.services import purchase_order_admin_service as legacy
from app.services.purchasing_inventory_service import purchasing_store_locations
from . import controls, receiving, receiving_worker
from .isolation import require_isolated_url
from .production_target import OwnerInspectionTarget

STORE_NAMES = ('Andresen', 'HWY 99', 'SR503', 'Longview')
PAGE_PATH = '/purchasing-v4/receiving'
API_PATH = '/api/purchasing-v4/receiving'


class Decision(BaseModel):
    request_key: UUID
    version: StrictInt


class Shipment(Decision):
    cells: list[tuple[StrictInt, StrictInt, StrictInt]]


def _writer(c, po_id):
    if not c.scalar(text("SELECT to_regclass('purchasing_v4.po_ownership')")):
        return None
    return c.execute(text('SELECT writer FROM purchasing_v4.po_ownership WHERE purchase_order_id=:id'),
        {'id': po_id}).scalar_one_or_none()


def _store_columns(stores):
    def key(name):
        return ''.join(char for char in name.casefold() if char.isalnum()).replace('highway99','hwy99')

    by_name = {}
    for store in stores:
        name = key(store['name'])
        if name in by_name:
            raise controls.InvalidPolicy('The receiving store identity is ambiguous')
        by_name[name] = store['id']
    if any(key(name) not in by_name for name in STORE_NAMES):
        raise controls.InvalidPolicy('The four receiving stores must have verified store identities')
    return [dict(id=by_name[key(name)], name=name) for name in STORE_NAMES]


def _native_workspace(c, po, actor_id):
    data = receiving.projection(c, po.id, actor_id=actor_id, load_unopened=True, history_limit=None)
    rows = [dict(id=line['id'], name=line['item_name'], sku=line['sku'], variant=line['variation_name'],
        ordered=line['ordered_qty'], previous=line['received_good'], outstanding=receiving.worksheet_outstanding(line),
        closed=sum(line['closed'].values()), requested=line['ordered'], received=line['allocated'])
        for line in data['lines']]
    postings = {}
    for line in data['lines']:
        for operation in line['posting']:
            event_id = operation['event_id']
            postings.setdefault(event_id, {})[line['id'], operation['destination_store_id']] = operation['state']
    history = []
    for event in reversed(data['history']):
        event_id = event['id']
        lines = event['payload']['lines']
        direct = event['payload'].get('direct', False)
        movements = postings.get(event_id, {})
        states = movements.values()
        entries = [dict(line_id=line['line_id'], store_id=int(store), quantity=qty if event['kind'] != 'CLOSE' else 0,
            closed=qty if event['kind'] == 'CLOSE' else 0,
            posting=('Posted' if movements.get((line['line_id'], int(store))) == 'VERIFIED' else 'Posting pending')
                if direct and qty and event['kind'] == 'RECEIPT' else None)
            for line in lines for store, qty in line.get('split', line.get('stores', {})).items()
            if direct or qty]
        history.append(dict(id=event_id, kind=event['kind'], at=event['created_at'], actor_id=event['actor_id'],
            received=sum(line.get('good', 0) for line in lines),
            retry=direct and any(state != 'VERIFIED' for state in states), children=entries,
            status='Posting pending' if any(state != 'VERIFIED' for state in states)
                else 'Posted to stores' if states else 'Recorded',
            note=event['payload'].get('reason') if not direct else None))
    return rows, _store_columns(data['snapshot']['stores']), data['version'], history


def _legacy_workspace(db, po, actor_id):
    data = legacy.receiving_workspace(db, purchase_order_id=po.id, actor_principal_id=actor_id)
    rows = [dict(id=row['id'], name=row['name'], sku=row['sku'], variant=row['variant'],
        ordered=row['ordered'], previous=row['previous'], outstanding=row['outstanding'], closed=row['closed'],
        requested={k: v['intended'] for k, v in row['stores'].items()},
        received={k: v['previous'] for k, v in row['stores'].items()}) for row in data['rows']]
    stores = _store_columns([dict(id=s.id, name=s.name) for s in legacy._receiving_store_rows(db)])
    history = []
    for event in data['history']:
        children = [*event['children'], *(dict(line_id=line, store_id=store, quantity=qty, closed=0)
            for line, store, qty in event['zero_cells'])]
        history.append(dict(event, children=children))
    return rows, stores, legacy._receiving_revision(db, po.id), history


def register(app, engine, owner, adapter=None, *, target=None):
    if target is None:
        require_isolated_url(engine.url)
        if not getattr(getattr(adapter, 'client', None), 'isolated_simulator', False):
            raise PermissionError('The worksheet preview requires the isolated inventory simulator')
    else:
        if not isinstance(target, OwnerInspectionTarget) or adapter is not None:
            raise PermissionError('Production receiving requires the verified owner target and established PO posting service')
        with engine.connect() as c:
            target.verify(c)
    inventory_post = adapter.client.post if adapter is not None else None
    mode = 'preview' if target is None else 'production'

    def native_order(c, po_id):
        native = _writer(c, po_id) == 'V4'
        if native and adapter is None:
            raise HTTPException(503, 'Receiving for V4-authorized orders is paused')
        return native

    @app.get(PAGE_PATH, response_class=HTMLResponse)
    def page(actor_id=Depends(owner)):
        static = Path(__file__).with_name('worksheet_static')
        assets = json.loads((static / 'assets.json').read_text())
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Receiving · Purchasing V4</title>
        <link rel="stylesheet" href="/worksheet-assets/{assets['css']}"></head>
        <body><main id="receiving-sheet" data-owner="{actor_id}" data-mode="{mode}"></main>
        <script type="module" src="/worksheet-assets/{assets['receiving_sheet_js']}"></script></body></html>"""

    @app.get(API_PATH + '/orders')
    def orders(before: int | None = None, actor_id=Depends(owner)):
        with engine.connect() as c:
            rows = c.execute(text("""SELECT p.id,v.name AS vendor
                FROM public.purchase_orders p JOIN public.vendors v ON v.id=p.vendor_id
                WHERE p.status NOT IN ('DRAFT','CANCELLED') AND (CAST(:before AS bigint) IS NULL OR p.id<:before)
                ORDER BY p.id DESC LIMIT 101"""), {'before': before}).mappings().all()
        return dict(rows=[dict(row) for row in rows[:100]], next_cursor=rows[99]['id'] if len(rows)>100 else None)

    @app.get(API_PATH + '/orders/{po_id}')
    def worksheet(po_id: int, actor_id=Depends(owner)):
        with Session(engine) as db:
            # The revision and all displayed quantities come from the same database snapshot.
            c = db.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
            po = db.get(PurchaseOrder, po_id)
            if po is None:
                raise HTTPException(404, 'Purchase order not found')
            native = native_order(c, po_id)
            rows, stores, version, history = (_native_workspace(c, po, actor_id) if native
                else _legacy_workspace(db, po, actor_id))
            ordered = sum(row['ordered'] for row in rows)
            previous = sum(row['previous'] for row in rows)
            outstanding = sum(row['outstanding'] for row in rows)
            closed = sum(row['closed'] for row in rows)
            placed = po.status.value not in ('DRAFT','CANCELLED')
            status = ('Closed — Unfulfilled' if closed and not outstanding else 'Received' if ordered and not outstanding
                else 'Partially Received' if previous else 'Awaiting Delivery') if placed else po.status.value.title()
            return dict(id=po.id, vendor=db.get(Vendor, po.vendor_id).name, rows=rows, stores=stores,
                version=version, history=history, ordered=ordered, previous=previous, outstanding=outstanding,
                closed=closed, status=status, can_receive=placed and bool(outstanding))

    @app.post(API_PATH + '/orders/{po_id}/shipments', dependencies=[Depends(verify_csrf)])
    def ship(po_id: int, value: Shipment, actor_id=Depends(owner)):
        key = str(value.request_key)
        cells = {}
        for line, store, count in value.cells:
            if (line, store) in cells:
                raise controls.InvalidPolicy('Each receiving cell can appear once')
            cells[line,store] = receiving.units(count)
        if not cells:
            raise controls.InvalidPolicy('Enter at least one received quantity')
        with Session(engine) as db:
            c = db.connection()
            native = native_order(c, po_id)
            if native:
                split = {}
                for (line, store), count in cells.items():
                    split.setdefault(line, {})[str(store)] = count
                receiving.command(c, actor_id=actor_id, command_id=key, purchase_order_id=po_id,
                    expected_version=value.version, kind='RECEIPT', lines=[dict(line_id=line, split=quantities)
                    for line, quantities in split.items()], reason='Send to Stores', confirmed=True, direct=True)
                db.commit()
            else:
                allowed = {s['id'] for s in _store_columns([dict(id=s.id, name=s.name) for s in legacy._receiving_store_rows(db)])}
                verified, _ = purchasing_store_locations(db)
                if any(store not in allowed or store not in verified for _, store in cells):
                    raise controls.InvalidPolicy('Use the four receiving stores')
                try:
                    return legacy.submit_receiving_shipment(db, purchase_order_id=po_id,
                        received_qty_by_line_store=cells, actor_principal_id=actor_id, request_key=key,
                        worksheet_version=value.version, inventory_post=inventory_post)
                except legacy.ReceivingPostingInterrupted as exc:
                    raise HTTPException(503, str(exc)) from exc
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
        try:
            receiving_worker.process_worksheet_receipt(engine, po_id, key, adapter, actor_id)
        except Exception as exc:
            raise HTTPException(503, 'Shipment recorded. Retry the original shipment to recover posting.') from exc
        return dict(receipt_id=key)

    @app.post(API_PATH + '/orders/{po_id}/close', dependencies=[Depends(verify_csrf)])
    def close(po_id: int, value: Decision, actor_id=Depends(owner)):
        key = str(value.request_key)
        with Session(engine) as db:
            c = db.connection()
            if native_order(c, po_id):
                receiving.command(c, actor_id=actor_id, command_id=key, purchase_order_id=po_id,
                    expected_version=value.version, kind='CLOSE', lines=[], reason='Closed — Unfulfilled',
                    confirmed=True, direct=True)
            else:
                try:
                    legacy.close_receiving_shortages(db, purchase_order_id=po_id, actor_principal_id=actor_id,
                        request_key=key, worksheet_version=value.version)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
            db.commit()
        return dict(closed=True)

    @app.post(API_PATH + '/orders/{po_id}/shipments/{receipt_id}/retry', dependencies=[Depends(verify_csrf)])
    def retry(po_id: int, receipt_id: str, actor_id=Depends(owner)):
        with Session(engine) as db:
            if not native_order(db.connection(), po_id):
                try:
                    return legacy.receive_purchase_order(db, purchase_order_id=po_id, actor_principal_id=actor_id,
                        receipt_id=int(receipt_id), inventory_post=inventory_post)
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
        receiving_worker.process_worksheet_receipt(engine, po_id, controls.uuid(receipt_id), adapter, actor_id)
        return dict(receipt_id=receipt_id)
