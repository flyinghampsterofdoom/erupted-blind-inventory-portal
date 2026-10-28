"""Bounded SELECT-only adapters over legacy evidence, never legacy services.

No default creation, refresh, HTTP, reconciliation or ORM autoflush. Every public
method rechecks transaction read-only enforcement and snapshot isolation.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from zoneinfo import ZoneInfo
from sqlalchemy import text
from .contracts import Availability, QuantityEvidence, EvidenceState, classify_interval
from .identity import build_plan, valid_reference
from .isolation import require_read_only


BUSINESS_ZONE = ZoneInfo('America/Los_Angeles')


def _instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError('A timezone-aware evidence cutoff is required')


def _quantity(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except Exception:
        return None


@dataclass(frozen=True)
class AvailabilityEvidence:
    day: date
    availability: Availability
    state: EvidenceState
    reason: str
    event_ids: tuple[str, ...] = ()


class ReadOnlyEvidence:
    def __init__(self, connection):
        require_read_only(connection)
        self.connection = connection
        self._transaction = connection.get_transaction()
        self._incoming_rows = None

    def _rows(self, sql, **params):
        if self.connection.get_transaction() is not self._transaction:
            raise PermissionError('An evidence repository cannot cross snapshot transactions')
        require_read_only(self.connection)
        return self.connection.execute(text(sql), params).mappings().all()

    def identity_plan(self, source_namespace):
        def rows(table):
            # Table names below are static, not request parameters.
            return [json.loads(r['row'], parse_float=Decimal) for r in
                    self._rows(f'SELECT to_jsonb(t)::text AS row FROM public.{table} t')]
        return build_plan(source_namespace=source_namespace,
                          catalog=rows('ordering_catalog_identity'), vendors=rows('vendors'),
                          configs=rows('vendor_sku_configs'), purchase_orders=rows('purchase_orders'))

    def _location(self, store_id):
        rows = self._rows('''SELECT s.square_location_id,
            (SELECT count(*) FROM public.stores x WHERE x.active
             AND x.square_location_id=s.square_location_id) AS claims
            FROM public.stores s WHERE s.id=:store AND s.active''', store=store_id)
        if len(rows) != 1 or not rows[0]['square_location_id'] or rows[0]['claims'] != 1:
            return None
        return rows[0]['square_location_id']

    def _variation_known(self, variation_id):
        return valid_reference(variation_id) and bool(self._rows(
            'SELECT square_variation_id FROM public.ordering_catalog_identity WHERE square_variation_id=:vid',
            vid=variation_id))

    def _coverage(self, source, location, start, end, as_of, max_age):
        _instant(as_of)
        if not isinstance(max_age, timedelta) or max_age < timedelta(0):
            raise ValueError('An explicit nonnegative evidence age limit is required')
        failed = self._rows('SELECT last_result FROM public.consignment_sales_sync_state WHERE id=1')
        if failed and failed[0]['last_result'] == 'FAILED':
            return EvidenceState.UNAVAILABLE, 'SOURCE_REFRESH_FAILED', None
        rows = self._rows('''SELECT start_at, through_at, verified_at
            FROM public.purchasing_history_coverage WHERE source=:source AND location_id=:location''',
            source=source, location=location)
        if not rows:
            return EvidenceState.UNAVAILABLE, 'NO_COVERAGE', None
        row = rows[0]
        if row['verified_at'] < row['through_at']:
            return EvidenceState.UNKNOWN, 'INVALID_COVERAGE_CERTIFICATE', row['verified_at']
        if row['verified_at'] > as_of:
            return EvidenceState.UNKNOWN, 'COVERAGE_AFTER_CUTOFF', row['verified_at']
        if as_of-row['verified_at'] > max_age:
            return EvidenceState.UNAVAILABLE, 'STALE_COVERAGE', row['verified_at']
        if row['start_at'] > start or row['through_at'] < end:
            return EvidenceState.UNKNOWN, 'INCOMPLETE_COVERAGE', row['verified_at']
        return EvidenceState.KNOWN, '', row['verified_at']

    def sales(self, variation_id, store_id, start_day, end_day, *, as_of, max_age):
        _instant(as_of)
        if type(start_day) is not date or type(end_day) is not date or not start_day < end_day:
            raise ValueError('Sales scope uses half-open business dates')
        start = datetime.combine(start_day, time(), BUSINESS_ZONE)
        end = datetime.combine(end_day, time(), BUSINESS_ZONE)
        if end > datetime.combine(as_of.astimezone(BUSINESS_ZONE).date(), time(), BUSINESS_ZONE):
            raise ValueError('Only completed local selling days are supported')
        location = self._location(store_id)
        state, reason, verified_at = self._coverage('SALES', location, start, end, as_of, max_age)
        if not location:
            state, reason = EvidenceState.UNKNOWN, 'UNRESOLVED_STORE_LOCATION'
        if not self._variation_known(variation_id):
            state, reason = EvidenceState.UNKNOWN, 'UNRESOLVED_VARIATION_REFERENCE'
        params = dict(vid=variation_id, location=location, start=start_day, end=end_day, cutoff=as_of)
        # A correction replaces a source sale line; never changes financial facts.
        future = self._rows('''SELECT EXISTS (
          SELECT 1 FROM public.consignment_sale_facts f WHERE f.square_variation_id=:vid
            AND f.square_location_id=:location AND f.business_date>=:start AND f.business_date<:end
            AND f.source_synchronized_at>:cutoff
          UNION ALL SELECT 1 FROM public.purchasing_sale_corrections c WHERE c.kind='LINE'
            AND c.synchronized_at>:cutoff AND ((c.location_id=:location AND c.business_date>=:start
              AND c.business_date<:end AND c.variation_id=:vid) OR EXISTS (
              SELECT 1 FROM public.consignment_sale_facts f WHERE f.square_order_id=c.square_order_id
              AND f.square_line_item_uid=c.square_line_item_uid AND f.square_variation_id=:vid
              AND f.square_location_id=:location AND f.business_date>=:start AND f.business_date<:end))
          ) AS present''', **params)
        if future[0]['present']:
            state, reason = EvidenceState.UNKNOWN, 'SOURCE_CHANGED_AFTER_CUTOFF'
        values = {}
        if state == EvidenceState.KNOWN:
            rows = self._rows('''SELECT business_date, sum(qty) AS qty FROM (
              SELECT f.business_date, f.quantity_sold AS qty FROM public.consignment_sale_facts f
              WHERE f.square_location_id=:location AND f.square_variation_id=:vid
              AND f.business_date>=:start AND f.business_date<:end AND NOT EXISTS (
                SELECT 1 FROM public.purchasing_sale_corrections c WHERE c.kind='LINE'
                AND c.square_order_id=f.square_order_id AND c.square_line_item_uid=f.square_line_item_uid)
              UNION ALL SELECT c.business_date, c.quantity FROM public.purchasing_sale_corrections c
              WHERE c.kind='LINE' AND c.location_id=:location AND c.variation_id=:vid
              AND c.business_date>=:start AND c.business_date<:end
            ) corrected GROUP BY business_date''', **params)
            values = {r['business_date']: _quantity(r['qty']) for r in rows}
            if any(v is None or v < 0 for v in values.values()):
                state, reason = EvidenceState.UNKNOWN, 'INVALID_SALES_QUANTITY'
        return {start_day+timedelta(days=n): QuantityEvidence(state,
                values.get(start_day+timedelta(days=n), Decimal(0)) if state == EvidenceState.KNOWN else None,
                'legacy.corrected_gross_sales', reason, verified_at,
                ('purchasing_history_coverage:SALES:'+location,) if location else ())
                for n in range((end_day-start_day).days)}

    def inventory(self, variation_id, store_id, *, as_of, max_age):
        _instant(as_of)
        if not isinstance(max_age, timedelta) or max_age < timedelta(0):
            raise ValueError('Explicit nonnegative inventory age limit required')
        rows = self._rows('''SELECT i.*, r.result, r.started_at, r.completed_at
          FROM public.ordering_current_inventory i
          JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id
          WHERE i.square_variation_id=:vid AND i.store_id=:store''', vid=variation_id, store=store_id)
        source = 'legacy.square_inventory_observation'
        if not rows:
            return QuantityEvidence(EvidenceState.UNAVAILABLE, None, source, 'NO_OBSERVATION')
        row = rows[0]
        location = self._location(store_id)
        reason = ''
        if not location or row['square_location_id'] != location:
            reason = 'UNRESOLVED_STORE_LOCATION'
        elif row['refreshed_at'] > as_of or row['completed_at'] > as_of:
            reason = 'OBSERVATION_AFTER_CUTOFF'
        elif not row['started_at'] <= row['refreshed_at'] <= row['completed_at']:
            reason = 'INVALID_REFRESH_MEMBERSHIP'
        elif row['source_calculated_at'] is not None and row['source_calculated_at'] > row['refreshed_at']:
            reason = 'SOURCE_CALCULATION_AFTER_OBSERVATION'
        elif row['result'] != 'COMPLETE' or row['freshness_state'] != 'FRESH':
            reason = 'INCOMPLETE_OR_STALE_SOURCE'
        elif as_of-row['refreshed_at'] > max_age:
            reason = 'STALE_OBSERVATION'
        value = _quantity(row['counted_quantity'])
        if value is None:
            reason = 'INVALID_INVENTORY_QUANTITY'
        return QuantityEvidence(EvidenceState.UNKNOWN if reason else EvidenceState.KNOWN,
                                None if reason else value, source, reason, row['refreshed_at'],
                                (f'ordering_current_inventory:{variation_id}/{store_id}',
                                 f'ordering_inventory_refresh_runs:{row["refresh_run_id"]}'))

    def availability(self, variation_id, store_id, start_day, end_day, *, as_of, max_age):
        """Classify only if an anchor and an exhaustive dated trajectory are proved.

        Daily endpoints alone do not establish availability. Physical-count
        corrections or ambiguous calculated-at membership fail closed.
        """
        _instant(as_of)
        if type(start_day) is not date or type(end_day) is not date or start_day >= end_day:
            raise ValueError('Invalid availability date scope')
        start = datetime.combine(start_day, time(), BUSINESS_ZONE)
        end = datetime.combine(end_day, time(), BUSINESS_ZONE)
        location = self._location(store_id)
        inventory = self.inventory(variation_id, store_id, as_of=as_of, max_age=max_age)
        anchors = self._rows('''SELECT i.source_calculated_at, r.started_at FROM public.ordering_current_inventory i
            JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id
            WHERE i.square_variation_id=:vid AND i.store_id=:store''', vid=variation_id, store=store_id)
        anchor = inventory.observed_at
        state, reason, _ = self._coverage('MOVEMENTS', location, start, max(end, anchor or end), as_of, max_age)
        if end > datetime.combine(as_of.astimezone(BUSINESS_ZONE).date(), time(), BUSINESS_ZONE):
            raise ValueError('Only completed availability days are supported')
        if not location:
            state, reason = EvidenceState.UNKNOWN, 'UNRESOLVED_STORE_LOCATION'
        if inventory.state != EvidenceState.KNOWN or anchor is None or anchor < end:
            if state != EvidenceState.UNAVAILABLE:
                state, reason = EvidenceState.UNKNOWN, 'NO_QUALIFIED_INVENTORY_ANCHOR'
        if anchors and (anchors[0]['source_calculated_at'] is None or
                        anchors[0]['source_calculated_at'] > anchor):
            state, reason = EvidenceState.UNKNOWN, 'UNQUALIFIED_COUNT_TIMESTAMP'
        events = self._rows('''SELECT * FROM public.purchasing_inventory_events
            WHERE location_id=:location AND variation_id=:vid AND occurred_at>=:start
            ORDER BY occurred_at, event_type, event_id''', location=location, vid=variation_id, start=start)
        if anchor and any(e['physical_count'] or e['synchronized_at'] > as_of or
                          e['calculated_before_at'] > anchor or e['occurred_at'] > anchor
                          for e in events):
            state, reason = EvidenceState.UNKNOWN, 'UNQUALIFIED_MOVEMENT_MEMBERSHIP'
        if anchor and anchors and any(e['occurred_at'] >= anchors[0]['started_at'] for e in events):
            state, reason = EvidenceState.UNKNOWN, 'MOVEMENT_DURING_COUNT_READ'
        # An event spanning the start boundary prevents claiming a complete reconstruction.
        boundary = self._rows('''SELECT event_id FROM public.purchasing_inventory_events
           WHERE location_id=:location AND variation_id=:vid AND occurred_at<:start
           AND calculated_before_at>=:start''', location=location, vid=variation_id, start=start)
        if boundary:
            state, reason = EvidenceState.UNKNOWN, 'UNCERTAIN_BOUNDARY_MOVEMENT'
        sales = self.sales(variation_id, store_id, start_day, end_day, as_of=as_of, max_age=max_age)
        output = []
        for n in range((end_day-start_day).days):
            day = start_day+timedelta(days=n)
            status = Availability.UNKNOWN
            used = ()
            day_reason = reason
            if state == EvidenceState.KNOWN:
                begin = datetime.combine(day, time(), BUSINESS_ZONE)
                finish = datetime.combine(day+timedelta(days=1), time(), BUSINESS_ZONE)
                later = [e for e in events if begin <= e['occurred_at'] <= anchor]
                opening = inventory.value-sum((e['quantity_delta'] for e in later), Decimal())
                within = [e for e in later if e['occurred_at'] < finish]
                # Equal timestamps lack a proved event order; mixed movement is unknown.
                if len({e['occurred_at'] for e in within}) != len(within):
                    status = Availability.UNKNOWN
                else:
                    status = classify_interval(QuantityEvidence(EvidenceState.KNOWN, opening, 'trajectory'),
                                               tuple(e['quantity_delta'] for e in within), complete=True)
                used = tuple(e['event_id'] for e in later)
                if (status == Availability.STOCKOUT and sales[day].state == EvidenceState.KNOWN
                        and sales[day].value > 0):
                    status = Availability.UNKNOWN
                    day_reason = 'CONTRADICTORY_SALES_DURING_STOCKOUT'
            output.append(AvailabilityEvidence(day, status,
                EvidenceState.KNOWN if status != Availability.UNKNOWN else state if state != EvidenceState.KNOWN else EvidenceState.UNKNOWN,
                day_reason or ('MIXED_OR_AMBIGUOUS_INTERVAL' if status == Availability.UNKNOWN else ''), used))
        return tuple(output)

    def incoming(self, variation_id, store_id):
        """Legacy commitments only; no cap allocator or invented zero receipts.

        An exhaustive valid empty obligation set is confirmed zero. Missing
        allocations/receipts or inconsistent store totals stay unknown.
        """
        source = 'legacy.po_allocations'
        if not self._location(store_id):
            return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'UNRESOLVED_STORE_LOCATION')
        if not self._variation_known(variation_id):
            return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'UNRESOLVED_VARIATION_REFERENCE')
        if self._incoming_rows is None:
            self._incoming_rows = self._rows('''SELECT l.id, p.id AS purchase_order_id, l.variation_id, l.sku,
            l.ordered_qty, l.received_qty_total,
            a.store_id, a.allocated_qty, a.store_received_qty,
            COALESCE((SELECT sum(c.closed_quantity) FROM public.purchase_order_receipt_allocations c
                      WHERE c.purchase_order_line_id=l.id AND c.store_id=a.store_id),0) AS closed
            FROM public.purchase_order_lines l JOIN public.purchase_orders p ON p.id=l.purchase_order_id
            LEFT JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id
            WHERE p.status IN ('IN_TRANSIT','RECEIVED_SPLIT_PENDING') AND NOT l.removed
            ORDER BY l.id, a.store_id''')
        rows = self._incoming_rows
        lines = {}
        for row in rows:
            if not valid_reference(row['variation_id']):
                # A proved fully received/zero obligation cannot add incoming
                # for any product, even if its historical identity is missing.
                if (type(row['ordered_qty']) is int and type(row['received_qty_total']) is int
                        and 0 <= row['ordered_qty'] <= row['received_qty_total']):
                    continue
                return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'UNRESOLVED_OPEN_PO_IDENTITY')
            if row['variation_id'] == variation_id:
                lines.setdefault(row['id'], []).append(row)
        total = Decimal(0)
        references = tuple(f'purchase_order_lines:{line_id}' for line_id in sorted(lines))
        for allocations in lines.values():
            first = allocations[0]
            values = [first['ordered_qty'], first['received_qty_total']]
            values += [a[c] for a in allocations for c in ('allocated_qty','store_received_qty','closed')]
            if any(type(v) is not int or v < 0 for v in values) or any(a['store_id'] is None for a in allocations):
                return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'INCOMPLETE_INCOMING_BALANCES', references=references)
            if len({a['store_id'] for a in allocations}) != len(allocations):
                return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'DUPLICATE_STORE_ALLOCATION', references=references)
            remainders = [max(a['allocated_qty']-a['store_received_qty']-a['closed'],0) for a in allocations]
            if (sum(a['allocated_qty'] for a in allocations) != first['ordered_qty']
                    or sum(a['store_received_qty'] for a in allocations) != first['received_qty_total']):
                return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'CONFLICTING_INCOMING_ALLOCATION', references=references)
            if sum(a['closed'] for a in allocations) > max(first['ordered_qty']-first['received_qty_total'],0):
                return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'INVALID_CLOSURE_BALANCE', references=references)
            line_open = max(first['ordered_qty']-first['received_qty_total']-sum(a['closed'] for a in allocations),0)
            if sum(remainders) != line_open:
                return QuantityEvidence(EvidenceState.UNKNOWN, None, source, 'CONFLICTING_INCOMING_ALLOCATION', references=references)
            total += sum((Decimal(q) for a,q in zip(allocations,remainders) if a['store_id']==store_id), Decimal())
        return QuantityEvidence(EvidenceState.KNOWN, total, source, references=references)
