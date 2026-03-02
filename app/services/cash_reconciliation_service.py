from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CashReconciliationActual, CashReconciliationVerification, Principal, Store


class SquareNotFoundError(RuntimeError):
    def __init__(self, path: str, detail: str) -> None:
        super().__init__(f'Square resource not found for {path}: {detail}')
        self.path = path
        self.detail = detail


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def _parse_iso_datetime(raw: object) -> datetime | None:
    text = str(raw or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = f'{text[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _money_cents(raw_money: object) -> int:
    if not isinstance(raw_money, dict):
        return 0
    raw_amount = raw_money.get('amount')
    try:
        return int(raw_amount or 0)
    except (TypeError, ValueError):
        return 0


class _SquareClient:
    def __init__(self) -> None:
        if not settings.square_access_token:
            raise RuntimeError('SQUARE_ACCESS_TOKEN is required')
        self.base_url = settings.square_api_base_url.rstrip('/')
        self.timeout_seconds = settings.square_timeout_seconds
        self.headers = {
            'Authorization': f'Bearer {settings.square_access_token}',
            'Content-Type': 'application/json',
        }
        if settings.square_api_version:
            self.headers['Square-Version'] = settings.square_api_version

    def get(self, path: str, *, query: dict[str, object] | None = None) -> dict:
        query_str = ''
        if query:
            query_str = f'?{urlencode(query)}'
        req = Request(
            url=f'{self.base_url}{path}{query_str}',
            headers=self.headers,
            method='GET',
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
            if exc.code == 404:
                raise SquareNotFoundError(path, body) from exc
            raise RuntimeError(f'Square API error {exc.code} for {path}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error for {path}: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors: {data['errors']}")
        return data

    def post(self, path: str, payload: dict) -> dict:
        req = Request(
            url=f'{self.base_url}{path}',
            data=json.dumps(payload).encode('utf-8'),
            headers=self.headers,
            method='POST',
        )
        try:
            with urlopen(req, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            body = exc.read().decode('utf-8', errors='ignore') if exc.fp else ''
            if exc.code == 404:
                raise SquareNotFoundError(path, body) from exc
            raise RuntimeError(f'Square API error {exc.code} for {path}: {body}') from exc
        except URLError as exc:
            raise RuntimeError(f'Square API network error for {path}: {exc.reason}') from exc

        if data.get('errors'):
            raise RuntimeError(f"Square API returned errors: {data['errors']}")
        return data


def list_square_enabled_stores(db: Session) -> list[dict[str, object]]:
    rows = db.execute(
        select(Store.id, Store.name, Store.square_location_id)
        .where(
            Store.active.is_(True),
            Store.square_location_id.is_not(None),
        )
        .order_by(Store.name.asc())
    ).all()
    return [
        {
            'id': int(row.id),
            'name': str(row.name),
            'square_location_id': str(row.square_location_id),
        }
        for row in rows
        if row.square_location_id
    ]


def _store_for_reconciliation(db: Session, *, store_id: int) -> Store:
    row = db.execute(
        select(Store).where(
            Store.id == store_id,
            Store.active.is_(True),
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError('Store not found')
    if not row.square_location_id:
        raise ValueError('Store is missing square_location_id')
    return row


def _location_timezone_and_name(client: _SquareClient, *, square_location_id: str, fallback_name: str) -> tuple[str, str]:
    path = f'/v2/locations/{square_location_id}'
    try:
        response = client.get(path)
    except SquareNotFoundError as exc:
        raise ValueError(
            f'Square location not found for store mapping: {square_location_id}. '
            'Please re-sync stores and update square_location_id.'
        ) from exc
    location = response.get('location') or {}
    timezone_name = str(location.get('timezone') or '').strip() or 'UTC'
    location_name = str(location.get('name') or '').strip() or fallback_name
    return timezone_name, location_name


def _date_bucket(raw_timestamp: object, *, tz: ZoneInfo) -> date | None:
    parsed = _parse_iso_datetime(raw_timestamp)
    if parsed is None:
        return None
    return parsed.astimezone(tz).date()


def _init_range(start_date: date, end_date: date) -> dict[date, int]:
    if end_date < start_date:
        raise ValueError('End date must be on or after start date')
    buckets: dict[date, int] = {}
    current = start_date
    while current <= end_date:
        buckets[current] = 0
        current += timedelta(days=1)
    return buckets


def _apply_cash_payments(
    client: _SquareClient,
    *,
    location_id: str,
    start_utc: datetime,
    end_utc: datetime,
    tz: ZoneInfo,
    buckets: dict[date, int],
) -> None:
    cursor: str | None = None
    while True:
        payload: dict[str, object] = {
            'query': {
                'filter': {
                    'date_time_filter': {
                        'created_at': {
                            'start_at': _to_iso(start_utc),
                            'end_at': _to_iso(end_utc),
                        }
                    },
                    'location_id': {
                        'location_ids': [location_id],
                    },
                    'status': {'values': ['COMPLETED']},
                },
                'sort': {'sort_field': 'CREATED_AT', 'sort_order': 'ASC'},
            },
            'limit': 100,
        }
        if cursor:
            payload['cursor'] = cursor
        response = client.post('/v2/payments/search', payload)
        for payment in response.get('payments', []) or []:
            business_date = _date_bucket(
                payment.get('created_at') or payment.get('updated_at'),
                tz=tz,
            )
            if business_date not in buckets:
                continue
            payment_cash_cents = 0
            for tender in payment.get('tenders', []) or []:
                if str(tender.get('type') or '').strip().upper() != 'CASH':
                    continue
                payment_cash_cents += _money_cents(tender.get('amount_money'))
            if payment_cash_cents == 0 and str(payment.get('source_type') or '').strip().upper() == 'CASH':
                payment_cash_cents = _money_cents(payment.get('amount_money') or payment.get('total_money'))
            buckets[business_date] += payment_cash_cents
        cursor = response.get('cursor')
        if not cursor:
            break


def _apply_cash_refunds(
    client: _SquareClient,
    *,
    location_id: str,
    start_utc: datetime,
    end_utc: datetime,
    tz: ZoneInfo,
    buckets: dict[date, int],
) -> None:
    cursor: str | None = None
    while True:
        query: dict[str, object] = {
            'location_id': location_id,
            'begin_time': _to_iso(start_utc),
            'end_time': _to_iso(end_utc),
            'sort_order': 'ASC',
            'limit': 100,
        }
        if cursor:
            query['cursor'] = cursor
        response = client.get('/v2/refunds', query=query)
        for refund in response.get('refunds', []) or []:
            destination_type = str(refund.get('destination_type') or '').strip().upper()
            tender_type = str(refund.get('tender_type') or '').strip().upper()
            is_cash_refund = destination_type == 'CASH' or tender_type == 'CASH'
            if not is_cash_refund:
                continue
            business_date = _date_bucket(
                refund.get('created_at') or refund.get('updated_at'),
                tz=tz,
            )
            if business_date not in buckets:
                continue
            buckets[business_date] -= _money_cents(refund.get('amount_money'))
        cursor = response.get('cursor')
        if not cursor:
            break


def _apply_cash_drawer_events(
    client: _SquareClient,
    *,
    location_id: str,
    start_utc: datetime,
    end_utc: datetime,
    tz: ZoneInfo,
    buckets: dict[date, int],
) -> None:
    shifts_path = '/v2/cash-drawers/shifts'
    shift_cursor: str | None = None
    while True:
        shift_query: dict[str, object] = {
            'location_id': location_id,
            'begin_time': _to_iso(start_utc),
            'end_time': _to_iso(end_utc),
            'sort_order': 'ASC',
            'limit': 50,
        }
        if shift_cursor:
            shift_query['cursor'] = shift_cursor
        try:
            shifts_response = client.get(shifts_path, query=shift_query)
        except SquareNotFoundError:
            # Some accounts/locations do not expose cash drawer APIs; treat as zero paid-in/out adjustments.
            return
        for shift in shifts_response.get('cash_drawer_shifts', []) or []:
            shift_id = str(shift.get('id') or '').strip()
            if not shift_id:
                continue
            event_cursor: str | None = None
            while True:
                event_query: dict[str, object] = {'limit': 200}
                if event_cursor:
                    event_query['cursor'] = event_cursor
                event_path = f'/v2/cash-drawers/shifts/{shift_id}/events'
                try:
                    events_response = client.get(event_path, query=event_query)
                except SquareNotFoundError:
                    break
                for event in events_response.get('cash_drawer_events', []) or []:
                    event_type = str(event.get('event_type') or '').strip().upper()
                    if event_type not in {'PAID_IN', 'PAID_OUT'}:
                        continue
                    business_date = _date_bucket(
                        event.get('created_at') or event.get('event_at') or shift.get('opened_at'),
                        tz=tz,
                    )
                    if business_date not in buckets:
                        continue
                    event_cents = _money_cents(
                        event.get('event_money') or event.get('paid_in_money') or event.get('paid_out_money')
                    )
                    if event_type == 'PAID_IN':
                        buckets[business_date] += event_cents
                    else:
                        buckets[business_date] -= event_cents
                event_cursor = events_response.get('cursor')
                if not event_cursor:
                    break
        shift_cursor = shifts_response.get('cursor')
        if not shift_cursor:
            break


def get_expected_cash_by_day(
    db: Session,
    *,
    store_id: int,
    start_date: date,
    end_date: date,
) -> dict[str, object]:
    store = _store_for_reconciliation(db, store_id=store_id)
    buckets = _init_range(start_date, end_date)
    client = _SquareClient()
    timezone_name, location_name = _location_timezone_and_name(
        client,
        square_location_id=str(store.square_location_id),
        fallback_name=str(store.name),
    )

    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = 'UTC'
        tz = ZoneInfo('UTC')

    start_utc = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(timezone.utc)
    end_utc = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)

    _apply_cash_payments(
        client,
        location_id=str(store.square_location_id),
        start_utc=start_utc,
        end_utc=end_utc,
        tz=tz,
        buckets=buckets,
    )
    _apply_cash_refunds(
        client,
        location_id=str(store.square_location_id),
        start_utc=start_utc,
        end_utc=end_utc,
        tz=tz,
        buckets=buckets,
    )
    _apply_cash_drawer_events(
        client,
        location_id=str(store.square_location_id),
        start_utc=start_utc,
        end_utc=end_utc,
        tz=tz,
        buckets=buckets,
    )

    return {
        'store_id': int(store.id),
        'location_name': location_name,
        'square_location_id': str(store.square_location_id),
        'timezone': timezone_name,
        'rows': [
            {
                'business_date': business_date.isoformat(),
                'expected_cash_cents': int(cents),
            }
            for business_date, cents in sorted(buckets.items())
        ],
    }


def get_actual_cash_rows(
    db: Session,
    *,
    store_id: int,
    start_date: date,
    end_date: date,
) -> dict[str, object]:
    _store_for_reconciliation(db, store_id=store_id)
    _init_range(start_date, end_date)

    actual_rows = db.execute(
        select(CashReconciliationActual).where(
            and_(
                CashReconciliationActual.store_id == store_id,
                CashReconciliationActual.business_date >= start_date,
                CashReconciliationActual.business_date <= end_date,
            )
        )
    ).scalars().all()

    actual_by_date = {
        row.business_date: {
            'actual_cash_cents': int(row.actual_cash_cents),
            'updated_by_principal_id': int(row.updated_by_principal_id) if row.updated_by_principal_id is not None else None,
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in actual_rows
    }

    current = start_date
    rows: list[dict[str, object]] = []
    while current <= end_date:
        existing = actual_by_date.get(current)
        rows.append(
            {
                'business_date': current.isoformat(),
                'actual_cash_cents': existing['actual_cash_cents'] if existing else None,
                'updated_by_principal_id': existing['updated_by_principal_id'] if existing else None,
                'updated_at': existing['updated_at'] if existing else None,
            }
        )
        current += timedelta(days=1)

    history_rows = db.execute(
        select(
            CashReconciliationVerification,
            Principal.username,
        )
        .join(Principal, Principal.id == CashReconciliationVerification.verified_by_principal_id, isouter=True)
        .where(
            and_(
                CashReconciliationVerification.store_id == store_id,
                CashReconciliationVerification.business_date >= start_date,
                CashReconciliationVerification.business_date <= end_date,
            )
        )
        .order_by(CashReconciliationVerification.created_at.desc(), CashReconciliationVerification.id.desc())
        .limit(200)
    ).all()

    history = [
        {
            'id': int(verification.id),
            'business_date': verification.business_date.isoformat(),
            'previous_actual_cash_cents': verification.previous_actual_cash_cents,
            'actual_cash_cents': int(verification.actual_cash_cents),
            'expected_cash_cents': verification.expected_cash_cents,
            'note': verification.note,
            'verified_by_principal_id': int(verification.verified_by_principal_id),
            'verified_by_username': str(username) if username else None,
            'created_at': verification.created_at.isoformat() if verification.created_at else None,
        }
        for verification, username in history_rows
    ]

    return {
        'store_id': store_id,
        'rows': rows,
        'history': history,
    }


def save_actual_cash_rows(
    db: Session,
    *,
    store_id: int,
    principal_id: int,
    rows: list[dict[str, object]],
    expected_cash_by_date: dict[date, int] | None = None,
    note: str | None = None,
) -> dict[str, object]:
    _store_for_reconciliation(db, store_id=store_id)
    if not rows:
        raise ValueError('No rows provided')

    expected_lookup = expected_cash_by_date or {}
    saved = 0

    for row in rows:
        raw_date = str(row.get('business_date') or '').strip()
        if not raw_date:
            continue
        try:
            business_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(f'Invalid business_date: {raw_date}') from exc

        try:
            actual_cash_cents = int(row.get('actual_cash_cents'))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'Invalid actual_cash_cents for {raw_date}') from exc

        existing = db.get(CashReconciliationActual, (store_id, business_date))
        previous_value = existing.actual_cash_cents if existing else None

        if existing is None:
            existing = CashReconciliationActual(
                store_id=store_id,
                business_date=business_date,
                actual_cash_cents=actual_cash_cents,
                updated_by_principal_id=principal_id,
            )
            db.add(existing)
        else:
            existing.actual_cash_cents = actual_cash_cents
            existing.updated_by_principal_id = principal_id

        verification = CashReconciliationVerification(
            store_id=store_id,
            business_date=business_date,
            previous_actual_cash_cents=previous_value,
            actual_cash_cents=actual_cash_cents,
            expected_cash_cents=expected_lookup.get(business_date),
            note=note,
            verified_by_principal_id=principal_id,
        )
        db.add(verification)
        saved += 1

    if saved == 0:
        raise ValueError('No valid rows provided')

    db.flush()
    return {'saved_rows': saved}
