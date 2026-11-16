"""Dashboard reads are reconciled to disposable persisted receiving fixtures only.

No test connects to an actual purchasing database or actual Square service. The
optional review files contain only these explicitly synthetic persisted records.
"""
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.purchasing_v4 import receiving as native_receiving
from app.purchasing_v4.isolation import readonly_snapshot
from app.purchasing_v4.reconciliation import public_fingerprints
from app.purchasing_v4.receiving_worksheet_app import API_PATH
from app.services import purchase_order_admin_service as legacy
from test_purchasing_v4_receiving import receiving, purchasing, sheet, ready, engine, template, pytestmark
from test_purchasing_v4_receiving_worksheet import worksheet, shipments
from test_purchasing_v4_submission import fresh, review, authorize
from test_purchasing_v4_quotations import quote


URL = API_PATH + '/orders'


def fingerprint(db):
    with readonly_snapshot(db) as c:
        result = public_fingerprints(c)
        for table in ('receiving_order', 'receiving_head', 'receiving_event', 'inventory_balance',
                'inventory_operation', 'inventory_command'):
            result['v4.' + table] = c.execute(text(f"""SELECT md5(coalesce(
                jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,''))
                FROM purchasing_v4.{table} t""")).scalar_one()
        result['simulated_inventory'] = c.execute(text("""SELECT md5(coalesce(
            jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,''))
            FROM purchasing_v4_simulator.adjustment t""")).scalar_one()
        return result


def legacy_order(db, index, *, cost='2.50', status='IN_TRANSIT', order_date=True):
    """New, unreceived synthetic V2 order, distinct from the V4 fixture product."""
    po, line, allocation = 20000 + index, 30000 + index, 40000 + index
    with db.begin() as c:
        c.execute(text("""INSERT INTO purchase_orders(id,vendor_id,status,created_by_principal_id,ordered_at)
            VALUES (:po,2,CAST(:status AS purchase_order_status),1,:at)"""),
            dict(po=po, status=status, at=datetime.fromisoformat('2026-11-01T12:00:00+00:00') if order_date else None))
        c.execute(text("""INSERT INTO purchase_order_lines(id,purchase_order_id,variation_id,item_name,
            variation_name,ordered_qty,received_qty_total,unit_cost)
            VALUES (:line,:po,'FIXTURE_V2','Synthetic dashboard product','Second',10,0,:cost)"""),
            dict(line=line, po=po, cost=cost))
        c.execute(text("""INSERT INTO purchase_order_store_allocations(id,purchase_order_line_id,
            store_id,allocated_qty,expected_qty,store_received_qty) VALUES (:id,:line,2,10,10,0)"""),
            dict(id=allocation, line=line))
    return po


def new_order(db, source, native, index):
    if not native:
        return legacy_order(db, index)
    current = fresh((db, source), title='Synthetic receiving dashboard fixture ' + str(index))
    return authorize(current, review(current))['children'][0]['purchase_order_id']


def get_sheet(client, po):
    response = client.get(URL + '/' + str(po))
    assert response.status_code == 200, response.text
    return response.json()


def send(client, headers, po, count):
    order = get_sheet(client, po)
    shipments(client, headers, URL + '/' + str(po), dict(request_key=str(uuid4()), version=order['version'],
        cells=[[order['rows'][0]['id'], 2, count]]))
    return get_sheet(client, po)


def get_dashboard(client, **params):
    response = client.get(URL, params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('native', [False, True])
def test_five_receiving_states_match_persisted_worksheets_without_get_writes(worksheet, receiving, native):
    client, headers, db, _ = worksheet
    scenarios = {}
    for index, scenario in enumerate(('none', 'one_partial', 'multiple_partial', 'full', 'closed'), 1):
        po = new_order(db, receiving[2], native, index)
        before = get_sheet(client, po)
        if scenario == 'one_partial':
            send(client, headers, po, 1)
        elif scenario == 'multiple_partial':
            send(client, headers, po, 1)
            send(client, headers, po, 1)
        elif scenario == 'full':
            send(client, headers, po, before['ordered'])
        elif scenario == 'closed':
            partial = send(client, headers, po, 1)
            result = client.post(URL + '/' + str(po) + '/close', headers=headers,
                json=dict(request_key=str(uuid4()), version=partial['version']))
            assert result.status_code == 200, result.text
        scenarios[scenario] = po
    previous = fingerprint(db)
    response = get_dashboard(client, include_closed=True, limit=200)
    rows = {row['id']: row for row in response['rows']}
    expected = dict(none='Not Received', one_partial='Partially Received',
        multiple_partial='Partially Received', full='Fully Received', closed='Closed — Unfulfilled')
    worksheets = {}
    for scenario, po in scenarios.items():
        row, detail = rows[po], get_sheet(client, po)
        worksheets[str(po)] = detail
        assert row['receiving_status'] == expected[scenario]
        assert row['ordered_units'] == detail['ordered']
        assert row['received_units'] == detail['previous']
        assert row['outstanding_units'] == detail['outstanding']
        assert row['closed_unfulfilled_units'] == detail['closed']
        assert row['partial'] == (scenario in ('one_partial', 'multiple_partial', 'closed'))
        assert row['is_closed'] == (scenario in ('full', 'closed'))
        with db.connect() as c:
            unit = c.execute(text('SELECT unit_cost FROM purchase_order_lines WHERE purchase_order_id=:id'), {'id': po}).scalar_one()
        assert Decimal(row['original_order_value']) == unit * detail['ordered']
        assert Decimal(row['received_value']) == unit * detail['previous']
        assert (row['last_received_at'] is None) == (scenario == 'none')
    assert rows[scenarios['multiple_partial']]['received_units'] == 2
    assert rows[scenarios['closed']]['outstanding_units'] == 0
    assert rows[scenarios['closed']]['missing_units'] > 0
    open_rows = [row for row in rows.values() if not row['is_closed'] and row['outstanding_units']]
    summary = response['summary']
    assert summary['open_orders'] == len(open_rows)
    assert summary['partial_orders'] == sum(row['receiving_status'] == 'Partially Received' for row in open_rows)
    assert summary['outstanding_units'] == sum(row['outstanding_units'] for row in open_rows)
    # Untouched historical fixture PO 129 has an unknown saved cost; report unknown.
    assert summary['unknown_cost_orders'] == 1
    assert summary['outstanding_merchandise_cost'] is None
    filtered = get_dashboard(client, q='PO #' + str(scenarios['full']), include_closed=True)
    assert [row['id'] for row in filtered['rows']] == [scenarios['full']]
    assert filtered['summary'] == summary
    visible = get_dashboard(client)
    assert scenarios['closed'] not in {row['id'] for row in visible['rows']}
    assert scenarios['full'] not in {row['id'] for row in visible['rows']}
    assert fingerprint(db) == previous
    # Review exports are local bounded-development artifacts, never CI outputs.
    session = os.environ.get('AVA_BOUNDED_SESSION', '')
    source = Path(__file__).resolve().parents[1]
    registered_source = Path('/Volumes/AvaOne-Development-Retained/sessions') / session / 'source'
    if (os.environ.get('AVA_BOUNDED_PROJECT') == 'erupted'
            and session.startswith('AVA-ERUPTED-') and not os.environ.get('CI')
            and source == registered_source):
        # Export only this isolated fixture for visual review; never live records.
        directory = Path(__file__).resolve().parents[1] / 'docs' / 'development'
        directory.mkdir(parents=True, exist_ok=True)
        fixture = dict(response, review_fixture=True,
            fixture_source='Disposable PostgreSQL records with simulated inventory; no production records',
            scenarios=scenarios, writer='V4' if native else 'LEGACY')
        (directory / 'receiving-dashboard-fixture.json').write_text(json.dumps(fixture, indent=2) + '\n')
        (directory / 'receiving-dashboard-worksheet-fixture.json').write_text(json.dumps(
            dict(review_fixture=True, worksheets=worksheets), indent=2) + '\n')


@pytest.mark.parametrize('native', [False, True])
def test_corrections_reconcile_cumulative_receiving_without_advancing_delivery_date(worksheet, receiving, native):
    client, headers, db, _ = worksheet
    po = new_order(db, receiving[2], native, 20)
    order = send(client, headers, po, 2)
    before = get_dashboard(client, q=str(po))['rows'][0]
    line_id = order['rows'][0]['id']
    if native:
        with db.begin() as c:
            native_receiving.command(c, actor_id=1, command_id=str(uuid4()), purchase_order_id=po,
                expected_version=order['version'], kind='CORRECTION', confirmed=True,
                reason='Explicit synthetic receipt count correction', lines=[dict(line_id=line_id,
                    from_account='CENTRAL_AVAILABLE', store_id=2, units=1,
                    disposition='RECEIPT_COUNT_CORRECTION', physical_evidence='Synthetic fixture count verified')])
    else:
        with Session(db) as session:
            allocation_id = session.execute(text("""SELECT id FROM purchase_order_receipt_allocations
                WHERE purchase_order_line_id=:id AND event_kind='DELIVERY'"""), {'id': line_id}).scalar_one()
            legacy.correct_purchase_order_receiving(session, purchase_order_id=po,
                original_allocation_id=allocation_id, corrected_quantity=1,
                actor_principal_id=1, request_key=str(uuid4()))
            session.commit()
    previous = fingerprint(db)
    after = get_dashboard(client, q=str(po))['rows'][0]
    assert after['received_units'] == 1
    assert after['outstanding_units'] == before['outstanding_units'] + 1
    assert after['last_received_at'] == before['last_received_at']
    assert after['original_order_value'] == before['original_order_value']
    assert after['receiving_status'] == 'Partially Received'
    assert fingerprint(db) == previous


def test_filters_and_pagination_apply_to_all_records_and_preserve_summary(worksheet):
    client, headers, db, _ = worksheet
    # More than 100 persisted orders proves search does not inspect only the first page.
    for index in range(100, 203):
        legacy_order(db, index, order_date=index != 100)
    cancelled = legacy_order(db, 203, status='CANCELLED')
    unknown = legacy_order(db, 204, cost=None)
    overage = legacy_order(db, 205)
    send(client, headers, overage, 11)
    previous = fingerprint(db)
    first = get_dashboard(client, vendor_id=2, limit=2, sort='id', direction='asc')
    second = get_dashboard(client, vendor_id=2, limit=2, offset=first['next_offset'], sort='id', direction='asc')
    assert first['total_count'] == 104  # 103 ordinary open orders and one unpriced order.
    assert first['next_offset'] == 2
    assert {row['id'] for row in first['rows']}.isdisjoint(row['id'] for row in second['rows'])
    target = get_dashboard(client, q='20100')
    assert [row['id'] for row in target['rows']] == [20100]
    assert target['rows'][0]['order_date'] is None
    assert target['rows'][0]['last_received_at'] is None
    assert get_dashboard(client, q='Fixture Two', vendor_id=2, limit=200)['total_count'] == 104
    assert get_dashboard(client, q='Fixture Two', vendor_id=1)['total_count'] == 0
    over = get_dashboard(client, q=str(overage), include_closed=True)['rows'][0]
    assert over['received_units'] == 11 and over['received_value'] is None
    assert over['original_order_value'] == '25.00'
    cancelled_row = get_dashboard(client, q=str(cancelled), include_closed=True)['rows'][0]
    assert cancelled_row['receiving_status'] == 'Cancelled' and cancelled_row['is_closed']
    assert get_dashboard(client, q=str(cancelled))['total_count'] == 0
    assert get_dashboard(client, q=str(unknown))['rows'][0]['original_order_value'] is None
    for params in (dict(q='Fixture Two'), dict(status='Not Received'), dict(include_closed=True),
            dict(sort='original_order_value', direction='asc'), dict(sort='last_received_at')):
        assert get_dashboard(client, **params)['summary'] == first['summary']
    assert client.get(URL, params={'sort': 'not_a_column'}).status_code == 422
    assert client.get(URL, params={'direction': 'sideways'}).status_code == 422
    assert client.get(URL, params={'limit': 201}).status_code == 422
    assert client.get(URL, params={'offset': -1}).status_code == 422
    assert fingerprint(db) == previous


def test_known_summary_sums_line_shortages_without_netting_overages(worksheet):
    client, headers, db, _ = worksheet
    # This is the isolated historical fixture, never an actual PO or live cost.
    with db.begin() as c:
        c.execute(text('UPDATE purchase_order_lines SET unit_cost=3.25 WHERE id=1'))
    po = legacy_order(db, 300)
    with db.begin() as c:
        c.execute(text("""INSERT INTO purchase_order_lines(id,purchase_order_id,variation_id,item_name,
            variation_name,ordered_qty,received_qty_total,unit_cost)
            VALUES (50300,:po,'SYNTHETIC_DASHBOARD_EXTRA','Z synthetic second product','Only',10,0,3.25)"""), {'po': po})
        c.execute(text("""INSERT INTO purchase_order_store_allocations(id,purchase_order_line_id,store_id,
            allocated_qty,expected_qty,store_received_qty) VALUES (50300,50300,2,10,10,0)"""))
    send(client, headers, po, 12)
    closed_po = legacy_order(db, 301)
    detail = send(client, headers, closed_po, 2)
    closed = client.post(URL + '/' + str(closed_po) + '/close', headers=headers,
        json=dict(request_key=str(uuid4()), version=detail['version']))
    assert closed.status_code == 200, closed.text
    legacy_order(db, 302, cost=None, status='CANCELLED')
    previous = fingerprint(db)
    response = get_dashboard(client, include_closed=True)
    row = next(row for row in response['rows'] if row['id'] == po)
    assert row['ordered_units'] == 20 and row['received_units'] == 12
    assert row['outstanding_units'] == 10  # An extra two of one product cannot fill the other product.
    assert row['receiving_status'] == 'Partially Received'
    assert row['received_value'] is None  # The over-received units have no invented price.
    assert row['original_order_value'] == '57.50'
    with db.connect() as c:
        expected = c.execute(text("""SELECT sum(greatest(l.ordered_qty-l.received_qty_total-
                coalesce(closed.quantity,0),0)*l.unit_cost)
            FROM purchase_order_lines l JOIN purchase_orders p ON p.id=l.purchase_order_id
            LEFT JOIN (SELECT purchase_order_line_id,sum(closed_quantity) AS quantity
                FROM purchase_order_receipt_allocations GROUP BY purchase_order_line_id) closed
              ON closed.purchase_order_line_id=l.id
            WHERE NOT l.removed AND p.status NOT IN ('DRAFT','CANCELLED')""")).scalar_one()
    assert response['summary']['unknown_cost_orders'] == 0
    assert Decimal(response['summary']['outstanding_merchandise_cost']) == expected.quantize(Decimal('.01'))
    assert fingerprint(db) == previous


def test_native_overage_has_no_inferred_received_valuation(worksheet):
    client, headers, db, native_po = worksheet
    before = get_sheet(client, native_po)
    send(client, headers, native_po, before['ordered'] + 1)
    previous = fingerprint(db)
    row = get_dashboard(client, q=str(native_po), include_closed=True)['rows'][0]
    assert row['received_units'] == before['ordered'] + 1
    assert row['outstanding_units'] == 0 and row['receiving_status'] == 'Fully Received'
    assert row['received_value'] is None
    with db.connect() as c:
        original = c.execute(text('SELECT sum(ordered_qty*unit_cost) FROM purchase_order_lines WHERE purchase_order_id=:id'), {'id': native_po}).scalar_one()
    assert Decimal(row['original_order_value']) == original
    assert fingerprint(db) == previous


def test_quote_total_preserves_approved_charges_and_historical_unit_adapter(quote):
    from decimal import ROUND_HALF_UP
    from app.purchasing_v4.receiving_dashboard import dashboard
    from app.purchasing_v4.schema import migrate_receiving
    from test_purchasing_v4_quotations import upload, select, final, commit
    response = upload(quote, components=('1.25', '0.03', '0.02'))
    select(quote, response)
    reviewed = final(quote, response=response)
    assert not reviewed['blocking']
    po = commit(quote, reviewed)['children'][0]['purchase_order_id']
    db = quote[0]
    with db.begin() as c:
        migrate_receiving(c)
        snapshot = native_receiving.capture_order(c, po)
        line_id = snapshot['lines'][0]['id']
        native_receiving.command(c, actor_id=1, command_id=str(uuid4()), purchase_order_id=po,
            expected_version=0, kind='RECEIPT', confirmed=True,
            reason='Synthetic partial receipt verifies the exact approved unit accounting adapter',
            lines=[dict(line_id=line_id, good=1, damaged=0)])
    with readonly_snapshot(db) as c:
        previous = public_fingerprints(c)
        row = next(row for row in dashboard(c, include_closed=True)['rows'] if row['id'] == po)
        unit, ordered = c.execute(text('SELECT unit_cost,ordered_qty FROM purchase_order_lines WHERE id=:id'), {'id': line_id}).one()
        assert row['original_order_value'] == '302.00'  # Includes explicitly approved charges.
        assert row['received_value'] == format(unit.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')
        assert row['received_units'] == 1 and row['outstanding_units'] == ordered - 1
        assert public_fingerprints(c) == previous



def test_date_sort_compares_persisted_instants_across_dst_fallback(worksheet, monkeypatch):
    from app.purchasing_v4.receiving_dashboard import dashboard
    client, headers, db, _ = worksheet
    # The second local clock time is earlier on the clock but later in UTC.
    instants = (datetime.fromisoformat('2026-11-01T01:30:00-07:00'),
                datetime.fromisoformat('2026-11-01T01:15:00-08:00'))
    orders = [legacy_order(db, index) for index in (600, 601)]
    for po, instant in zip(orders, instants):
        with db.begin() as c:
            c.execute(text('UPDATE purchase_orders SET ordered_at=:instant WHERE id=:id'),
                {'instant': instant, 'id': po})
        monkeypatch.setattr(legacy, '_now', lambda instant=instant: instant)
        send(client, headers, po, 1)
    previous = fingerprint(db)
    with readonly_snapshot(db) as c:
        c.execute(text("SET LOCAL TIME ZONE 'America/Los_Angeles'"))
        for key in ('order_date', 'last_received_at'):
            for direction, expected in (('asc', orders), ('desc', list(reversed(orders)))):
                result = dashboard(c, vendor_id=2, sort=key, direction=direction)
                assert [row['id'] for row in result['rows']] == expected
                dates = {row['id']: row[key] for row in result['rows']}
                assert dates[orders[0]].endswith('-07:00')
                assert dates[orders[1]].endswith('-08:00')
    assert fingerprint(db) == previous
