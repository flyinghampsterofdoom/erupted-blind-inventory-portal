"""SELECT-only receiving dashboard over each purchase order's authoritative writer.

The worksheet's open balance excludes explicitly closed units. Physical shortfall
is retained separately so closing a shortage can never imply physical receipt.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import cmp_to_key

from sqlalchemy import text

NOT_RECEIVED = 'Not Received'
PARTIAL = 'Partially Received'
FULL = 'Fully Received'
UNFULFILLED = 'Closed — Unfulfilled'
CANCELLED = 'Cancelled'
STATUS_LABELS = (NOT_RECEIVED, PARTIAL, FULL, UNFULFILLED, CANCELLED)
SORT_KEYS = frozenset(('id', 'vendor', 'order_date', 'last_received_at', 'receiving_status',
    'partial', 'ordered_units', 'received_units', 'outstanding_units', 'original_order_value', 'received_value'))
CENT = Decimal('.01')


def _money(value):
    return None if value is None else format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def _iso(value):
    return value.isoformat() if value is not None else None


def _read_orders(c):
    """Bulk reads; never initialize a receiving snapshot or invoke a writer."""
    available = c.execute(text("""SELECT
        to_regclass('purchasing_v4.po_ownership') IS NOT NULL AS ownership,
        to_regclass('purchasing_v4.receiving_event') IS NOT NULL AS native_receiving,
        to_regclass('purchasing_v4.quotation_financial') IS NOT NULL AS quotations""")).mappings().one()
    ownership = ('LEFT JOIN purchasing_v4.po_ownership o ON o.purchase_order_id=p.id'
        if available['ownership'] else '')
    quotation = ('LEFT JOIN purchasing_v4.quotation_financial f ON f.purchase_order_id=p.id'
        if available['quotations'] else '')
    writer = "coalesce(o.writer,'LEGACY')" if available['ownership'] else "'LEGACY'"
    quoted_value = 'f.total' if available['quotations'] else 'NULL::numeric'
    orders = [dict(row) for row in c.execute(text(f"""SELECT p.id,p.vendor_id,v.name AS vendor,
        p.status::text AS lifecycle_status,coalesce(p.ordered_at,p.submitted_at) AS order_date,
        {writer} AS writer,{quoted_value} AS quoted_order_value
        FROM public.purchase_orders p JOIN public.vendors v ON v.id=p.vendor_id
        {ownership} {quotation} WHERE p.status<>'DRAFT'""")).mappings()]
    if not orders:
        return []
    ids = [row['id'] for row in orders]
    lines = c.execute(text("""SELECT l.id,l.purchase_order_id,l.ordered_qty,l.received_qty_total,l.unit_cost,
        coalesce(closed.units,0) AS closed_units
        FROM public.purchase_order_lines l LEFT JOIN (
            SELECT a.purchase_order_line_id,sum(a.closed_quantity) AS units
            FROM public.purchase_order_receipt_allocations a GROUP BY a.purchase_order_line_id
        ) closed ON closed.purchase_order_line_id=l.id
        WHERE l.purchase_order_id=ANY(:ids) AND NOT l.removed ORDER BY l.id"""), {'ids': ids}).mappings().all()
    legacy_dates = dict(c.execute(text("""SELECT r.purchase_order_id,max(r.received_at)
        FROM public.purchase_order_receipts r
        WHERE r.purchase_order_id=ANY(:ids) AND r.status='SUBMITTED'
          AND (r.event_kind='DELIVERY' OR r.event_kind IS NULL)
          AND EXISTS (SELECT 1 FROM public.purchase_order_receipt_lines rl
            JOIN public.purchase_order_lines l ON l.id=rl.purchase_order_line_id
            WHERE rl.receipt_id=r.id AND rl.received_qty>0 AND NOT l.removed)
        GROUP BY r.purchase_order_id"""), {'ids': ids}).all())
    native_ids = [row['id'] for row in orders if row['writer'] == 'V4']
    native = {}
    if native_ids:
        if not available['native_receiving']:
            raise ValueError('Authoritative V4 receiving records are unavailable')
        native = {row['line_id']: dict(row) for row in c.execute(text("""WITH entries AS (
            SELECT e.purchase_order_id,e.kind,e.created_at,j.value AS line
            FROM purchasing_v4.receiving_event e
            CROSS JOIN LATERAL jsonb_array_elements(e.payload->'lines') j
            WHERE e.purchase_order_id=ANY(:ids) AND e.kind IN ('RECEIPT','CORRECTION','CLOSE','OVERAGE')
        ) SELECT (line->>'line_id')::bigint AS line_id,
            sum(CASE WHEN kind='RECEIPT' THEN coalesce((line->>'good')::bigint,0)
                WHEN kind='CORRECTION' THEN coalesce((line->>'good_delta')::bigint,0) ELSE 0 END) AS received_units,
            sum(CASE WHEN kind='CLOSE' THEN
                (SELECT coalesce(sum(value::bigint),0) FROM jsonb_each_text(line->'stores')) ELSE 0 END) AS closed_units,
            max(created_at) FILTER(WHERE kind='RECEIPT' AND
                coalesce((line->>'good')::bigint,0)+coalesce((line->>'damaged')::bigint,0)>0) AS last_received_at
            FROM entries GROUP BY (line->>'line_id')::bigint"""), {'ids': native_ids}).mappings()}
    by_order = defaultdict(list)
    for line in lines:
        by_order[line['purchase_order_id']].append(line)
    output = []
    for po in orders:
        current = by_order[po['id']]
        ordered = received = outstanding = missing = closed = 0
        original_value = received_value = outstanding_value = Decimal(0)
        original_known = bool(current)
        received_known = outstanding_known = True
        last_received = None if po['writer'] == 'V4' else legacy_dates.get(po['id'])
        for line in current:
            quantity = line['ordered_qty']
            if po['writer'] == 'V4':
                event = native.get(line['id'], {})
                previous = int(event.get('received_units', 0))
                released = int(event.get('closed_units', 0))
                receipt_at = event.get('last_received_at')
                if receipt_at is not None and (last_received is None or receipt_at > last_received):
                    last_received = receipt_at
            else:
                previous = line['received_qty_total']
                released = int(line['closed_units'])
            physical_shortfall = max(quantity - previous, 0)
            open_units = max(quantity - previous - released, 0)
            ordered += quantity
            received += previous
            missing += physical_shortfall
            outstanding += open_units
            closed += released
            cost = line['unit_cost']
            if cost is None:
                original_known &= quantity == 0
                received_known &= previous == 0
                outstanding_known &= open_units == 0
            else:
                # Legacy invoice amounts round each saved line. Native committed
                # valuations round the exact sum after multiplying historical units.
                line_original = cost * quantity
                original_value += (line_original.quantize(CENT) if po['writer'] == 'LEGACY' else line_original)
                received_value += cost * previous
                outstanding_value += cost * open_units
            # Physical over-receipt alone does not establish an agreed price for
            # extra merchandise. A separate OVERAGE approval is a distinct ledger;
            # never infer its partial consumption or overwrite the original cost.
            if previous > quantity:
                received_known = False
        cancelled = po['lifecycle_status'] == 'CANCELLED'
        status = (CANCELLED if cancelled else UNFULFILLED if closed and not outstanding
            else FULL if ordered and not missing else PARTIAL if received else NOT_RECEIVED)
        is_closed = cancelled or bool(ordered and not outstanding)
        output.append(dict(id=po['id'], vendor_id=po['vendor_id'], vendor=po['vendor'],
            order_date=_iso(po['order_date']), last_received_at=_iso(last_received),
            receiving_status=status, lifecycle_status=po['lifecycle_status'],
            partial=bool(received and missing), ordered_units=ordered, received_units=received,
            outstanding_units=outstanding, missing_units=missing, closed_unfulfilled_units=closed,
            original_order_value=_money(po['quoted_order_value'] if po['quoted_order_value'] is not None
                else original_value if original_known else None),
            received_value=_money(received_value if received_known else None), is_closed=is_closed,
            _outstanding_value=outstanding_value if outstanding_known else None))
    return output


def dashboard(c, *, q='', vendor_id=None, status='', include_closed=False,
        sort='order_date', direction='desc', offset=0, limit=100, before=None):
    """Filter and sort the complete snapshot before applying bounded pagination.

    Summary figures always describe all open obligations, independent of filters.
    The caller supplies a read-only REPEATABLE READ connection.
    """
    if sort not in SORT_KEYS or direction not in ('asc', 'desc'):
        raise ValueError('Unsupported receiving sort')
    if not 0 <= offset or not 1 <= limit <= 200:
        raise ValueError('Invalid receiving page bounds')
    rows = _read_orders(c)
    open_rows = [row for row in rows if not row['is_closed'] and row['outstanding_units'] > 0]
    unknown = sum(row['_outstanding_value'] is None for row in open_rows)
    known_value = sum((row['_outstanding_value'] for row in open_rows
        if row['_outstanding_value'] is not None), Decimal(0))
    summary = dict(open_orders=len(open_rows),
        partial_orders=sum(row['receiving_status'] == PARTIAL for row in open_rows),
        outstanding_units=sum(row['outstanding_units'] for row in open_rows),
        outstanding_merchandise_cost=None if unknown else _money(known_value),
        unknown_cost_orders=unknown,
        scope='all_open_orders')
    vendors = sorted({row['vendor_id']: row['vendor'] for row in rows}.items(), key=lambda item: (item[1].casefold(), item[0]))
    query = q.strip().casefold()
    visible = [row for row in rows
        if (include_closed or not row['is_closed'])
        and (vendor_id is None or row['vendor_id'] == vendor_id)
        and (not status or row['receiving_status'] == status)
        and (before is None or row['id'] < before)
        and (not query or query in str(row['id']) or query in ('po #' + str(row['id']))
             or query in row['vendor'].casefold())]

    def compare(left, right):
        a, b = left[sort], right[sort]
        # Unknown dates and values stay last in both directions.
        if a is None or b is None:
            return (a is None) - (b is None) or right['id'] - left['id']
        if sort in ('original_order_value', 'received_value'):
            a, b = Decimal(a), Decimal(b)
        elif sort in ('order_date', 'last_received_at'):
            # Compare instants, including the repeated local hour at DST end.
            a, b = datetime.fromisoformat(a), datetime.fromisoformat(b)
        elif isinstance(a, str):
            a, b = a.casefold(), b.casefold()
        result = (a > b) - (a < b)
        return result * (1 if direction == 'asc' else -1) or right['id'] - left['id']

    visible.sort(key=cmp_to_key(compare))
    total = len(visible)
    page = visible[offset:offset + limit]
    return dict(rows=[{key: value for key, value in row.items() if not key.startswith('_')} for row in page],
        summary=summary, vendors=[dict(id=key, name=value) for key, value in vendors],
        statuses=list(STATUS_LABELS), total_count=total,
        next_offset=offset + limit if offset + limit < total else None,
        next_cursor=page[-1]['id'] if page and sort == 'id' and direction == 'desc' and offset + limit < total else None)
