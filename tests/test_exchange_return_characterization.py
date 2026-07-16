from datetime import date, datetime, timezone

import pytest

from app.services.exchange_return_form_service import create_exchange_return_form


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Db:
    def __init__(self, store_exists=True):
        self.store_exists = store_exists
        self.added = []
        self.flushed = False

    def execute(self, _query):
        return _Scalar(10 if self.store_exists else None)

    def add(self, row):
        self.added.append(row)

    def flush(self):
        self.flushed = True


VALID = {
    'store_id': 10,
    'principal_id': 44,
    'employee_name': '  Jamie Person  ',
    'original_purchase_date': date(2026, 7, 1),
    'generated_at': datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc),
    'original_ticket_number': '  OLD-1 ',
    'exchange_ticket_number': ' NEW-2 ',
    'items_text': ' item details ',
    'reason_text': ' damaged ',
    'refund_given': False,
    'refund_approved_by': ' manager ',
}


def test_exchange_return_creation_requires_active_store_and_attributes_principal():
    with pytest.raises(ValueError, match='Store not found'):
        create_exchange_return_form(_Db(store_exists=False), **VALID)
    db = _Db()
    row = create_exchange_return_form(db, **VALID)
    assert db.flushed is True
    assert row.created_by_principal_id == 44
    assert row.store_id == 10
    assert row.employee_name == 'Jamie Person'
    assert row.original_ticket_number == 'OLD-1'
    assert row.exchange_ticket_number == 'NEW-2'


@pytest.mark.parametrize(
    ('field', 'message'),
    [
        ('employee_name', 'Employee name is required'),
        ('original_ticket_number', 'Original ticket number is required'),
        ('exchange_ticket_number', 'Exchange ticket number is required'),
        ('items_text', r'Item\(s\) is required'),
        ('reason_text', 'Reason is required'),
        ('refund_approved_by', 'Refund approval name is required'),
    ],
)
def test_exchange_return_required_text_fields(field, message):
    values = {**VALID, field: '   '}
    with pytest.raises(ValueError, match=message):
        create_exchange_return_form(_Db(), **values)
