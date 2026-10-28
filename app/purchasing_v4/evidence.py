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
from .receiving_supply import incoming_sql,posting_membership,qualify_inventory
from .contracts import Availability, QuantityEvidence, EvidenceState, classify_interval
from .identity import build_plan, valid_reference
from .isolation import require_read_only
from .evidence_rules import coverage_status, inventory_observation, incoming_quantity, INCOMING_SQL, availability_projection


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
        failed=self._rows('SELECT last_result FROM public.consignment_sales_sync_state WHERE id=1')
        rows=self._rows('SELECT start_at,through_at,verified_at FROM public.purchasing_history_coverage WHERE source=:source AND location_id=:location',source=source,location=location)
        return coverage_status(rows[0] if rows else None,bool(failed and failed[0]['last_result']=='FAILED'),start=start,end=end,as_of=as_of,max_age=max_age)

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
        rows=self._rows('SELECT i.*,r.result,r.started_at,r.completed_at FROM public.ordering_current_inventory i\n JOIN public.ordering_inventory_refresh_runs r ON r.id=i.refresh_run_id\n WHERE i.square_variation_id=:vid AND i.store_id=:store',vid=variation_id,store=store_id)
        observed=inventory_observation(rows[0] if rows else None,self._location(store_id),variation_id=variation_id,store_id=store_id,as_of=as_of,max_age=max_age)
        return qualify_inventory(observed,posting_membership(self.connection,[variation_id])[(variation_id,store_id)],dict(rows[0]) if rows else None,as_of=as_of)

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
        events = self._rows('''SELECT * FROM public.purchasing_inventory_events
            WHERE location_id=:location AND variation_id=:vid AND occurred_at>=:start
            ORDER BY occurred_at, event_type, event_id''', location=location, vid=variation_id, start=start)
        boundary = self._rows('''SELECT event_id FROM public.purchasing_inventory_events
           WHERE location_id=:location AND variation_id=:vid AND occurred_at<:start
           AND calculated_before_at>=:start''', location=location, vid=variation_id, start=start)
        sales=self.sales(variation_id,store_id,start_day,end_day,as_of=as_of,max_age=max_age)
        rows=availability_projection(location=location,inventory=inventory,anchor_row=anchors[0] if anchors else None,
            coverage=(state,reason,None),events=events,boundary=boundary,sales=sales,
            start_day=start_day,end_day=end_day,as_of=as_of)
        return tuple(AvailabilityEvidence(*row) for row in rows)

    def incoming(self, variation_id, store_id):
        source='legacy.po_allocations'
        if not self._location(store_id):return QuantityEvidence(EvidenceState.UNKNOWN,None,source,'UNRESOLVED_STORE_LOCATION')
        if not self._variation_known(variation_id):return QuantityEvidence(EvidenceState.UNKNOWN,None,source,'UNRESOLVED_VARIATION_REFERENCE')
        if self._incoming_rows is None:self._incoming_rows=self._rows(incoming_sql(self.connection,INCOMING_SQL))
        values,reason,references=incoming_quantity(self._incoming_rows,variation_id)
        return QuantityEvidence(EvidenceState.UNKNOWN if reason else EvidenceState.KNOWN,
            None if reason else Decimal(values.get(store_id,0)),source,reason,references=references)
