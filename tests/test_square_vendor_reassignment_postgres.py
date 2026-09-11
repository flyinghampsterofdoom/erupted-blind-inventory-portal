from __future__ import annotations

import os
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Vendor, VendorSkuConfig
from app.services.square_ordering_data_service import sync_vendor_sku_configs_from_square


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')
INDEX_NAME = 'idx_vendor_sku_configs_default_vendor'
INDEX_DDL = f'''
CREATE UNIQUE INDEX {INDEX_NAME}
ON vendor_sku_configs(sku)
WHERE is_default_vendor IS TRUE AND active IS TRUE
'''


@pytest.fixture(scope='module')
def postgres_engine():
    if not ADMIN_URL:
        pytest.skip('set TEST_POSTGRES_ADMIN_URL for vendor reassignment PostgreSQL integration')
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_vendor_reassignment_{uuid.uuid4().hex[:10]}'
    database_url = f"{ADMIN_URL.rsplit('/', 1)[0]}/{database_name}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS citext'))
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text(INDEX_DDL))
        yield engine
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(
                'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                'WHERE datname = :database_name AND pid <> pg_backend_pid()'
            ), {'database_name': database_name})
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()


@pytest.fixture()
def pg_db(postgres_engine):
    connection = postgres_engine.connect()
    transaction = connection.begin()
    session = Session(connection)
    session.add_all([
        Vendor(id=910001, square_vendor_id='SQUARE-A', name='Vendor A', active=True),
        Vendor(id=910002, square_vendor_id='SQUARE-B', name='Vendor B', active=True),
        Vendor(id=910003, square_vendor_id='SQUARE-C', name='Vendor C', active=True),
    ])
    session.flush()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _mapping(
    row_id: int,
    vendor_id: int,
    sku: str,
    *,
    active: bool,
    default: bool,
    cost: str | None = '4.2500',
) -> VendorSkuConfig:
    return VendorSkuConfig(
        id=row_id,
        vendor_id=vendor_id,
        sku=sku,
        square_variation_id=f'VAR-{sku}',
        unit_cost=Decimal(cost) if cost is not None else None,
        pack_size=5,
        min_order_qty=10,
        active=active,
        is_default_vendor=default,
    )


def _square_response(*assignments: tuple[str, str, str]) -> dict:
    return {
        'items': [{
            'item_data': {
                'variations': [{
                    'id': variation_id,
                    'item_variation_data': {
                        'sku': sku,
                        'item_variation_vendor_infos': [{
                            'item_variation_vendor_info_data': {'vendor_id': square_vendor_id}
                        }],
                    },
                } for sku, variation_id, square_vendor_id in assignments]
            }
        }]
    }


def _sync(db: Session, *assignments: tuple[str, str, str]):
    with patch(
        'app.services.square_ordering_data_service._square_post',
        return_value=_square_response(*assignments),
    ):
        return sync_vendor_sku_configs_from_square(db)


def _active_defaults(db: Session, sku: str) -> list[VendorSkuConfig]:
    return db.scalars(select(VendorSkuConfig).where(
        VendorSkuConfig.sku == sku,
        VendorSkuConfig.active.is_(True),
        VendorSkuConfig.is_default_vendor.is_(True),
    )).all()


def test_exact_production_partial_unique_index_is_installed(postgres_engine):
    with postgres_engine.connect() as connection:
        definition = connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :name"
        ), {'name': INDEX_NAME}).scalar_one()
    normalized = ' '.join(definition.lower().split())
    assert 'create unique index idx_vendor_sku_configs_default_vendor' in normalized
    assert 'using btree (sku)' in normalized
    assert 'where ((is_default_vendor is true) and (active is true))' in normalized


def test_postgres_index_rejects_destination_before_old_default_is_demoted(pg_db):
    old = _mapping(920000, 910001, 'UNSAFE-ORDER', active=True, default=True)
    pg_db.add(old)
    pg_db.flush()
    savepoint = pg_db.connection().begin_nested()
    with pytest.raises(IntegrityError, match=INDEX_NAME):
        pg_db.connection().execute(text(
            'INSERT INTO vendor_sku_configs '
            '(id, vendor_id, sku, unit_cost, pack_size, min_order_qty, is_default_vendor, active) '
            "VALUES (920099, 910002, 'UNSAFE-ORDER', 4.25, 5, 10, true, true)"
        ))
    savepoint.rollback()


def test_postgres_new_destination_demotes_before_insert(pg_db):
    old = _mapping(920001, 910001, 'NEW-DEST', active=True, default=True)
    pg_db.add(old)
    pg_db.flush()

    result = _sync(pg_db, ('NEW-DEST', 'VAR-NEW-DEST', 'SQUARE-B'))

    destination = pg_db.scalar(select(VendorSkuConfig).where(
        VendorSkuConfig.vendor_id == 910002,
        VendorSkuConfig.sku == 'NEW-DEST',
    ))
    assert old.active is True
    assert old.is_default_vendor is False
    assert destination is not None
    assert destination.active is True
    assert destination.is_default_vendor is True
    assert destination.unit_cost == Decimal('4.2500')
    assert _active_defaults(pg_db, 'NEW-DEST') == [destination]
    assert result['vendor_reassigned'] == 1


def test_postgres_active_non_default_destination_becomes_default(pg_db):
    old = _mapping(920011, 910001, 'ACTIVE-DEST', active=True, default=True)
    destination = _mapping(920012, 910002, 'ACTIVE-DEST', active=True, default=False, cost='7.5000')
    pg_db.add_all([old, destination])
    pg_db.flush()

    result = _sync(pg_db, ('ACTIVE-DEST', 'VAR-ACTIVE-DEST', 'SQUARE-B'))

    assert old.active is True
    assert old.is_default_vendor is False
    assert destination.active is True
    assert destination.is_default_vendor is True
    assert destination.unit_cost == Decimal('7.5000')
    assert _active_defaults(pg_db, 'ACTIVE-DEST') == [destination]
    assert result['vendor_reassigned'] == 1


def test_postgres_inactive_destination_reactivates_safely(pg_db):
    old = _mapping(920021, 910001, 'INACTIVE-DEST', active=True, default=True)
    destination = _mapping(920022, 910002, 'INACTIVE-DEST', active=False, default=False)
    pg_db.add_all([old, destination])
    pg_db.flush()

    result = _sync(pg_db, ('INACTIVE-DEST', 'VAR-INACTIVE-DEST', 'SQUARE-B'))

    assert old.active is True
    assert old.is_default_vendor is False
    assert destination.active is True
    assert destination.is_default_vendor is True
    assert _active_defaults(pg_db, 'INACTIVE-DEST') == [destination]
    assert result['reactivated'] == 1
    assert result['vendor_reassigned'] == 1


def test_postgres_existing_sole_default_is_not_reassigned(pg_db):
    destination = _mapping(920031, 910002, 'UNCHANGED', active=True, default=True)
    pg_db.add(destination)
    pg_db.flush()

    result = _sync(pg_db, ('UNCHANGED', 'VAR-UNCHANGED', 'SQUARE-B'))

    assert _active_defaults(pg_db, 'UNCHANGED') == [destination]
    assert result['vendor_reassigned'] == 0
    assert result['reassignments'] == []


def test_postgres_reproduces_eightcig_to_vaporbeast_production_case(pg_db):
    old = _mapping(920041, 910001, '6972866503909', active=True, default=True, cost='1.4400')
    pg_db.add(old)
    pg_db.flush()

    result = _sync(pg_db, ('6972866503909', 'H4TODTK5EZWWJESYBVJHM72U', 'SQUARE-B'))

    defaults = _active_defaults(pg_db, '6972866503909')
    assert len(defaults) == 1
    assert defaults[0].vendor_id == 910002
    assert defaults[0].unit_cost == Decimal('1.4400')
    assert old.active is True
    assert old.is_default_vendor is False
    assert result['vendor_reassigned'] == 1


def test_postgres_batch_reassignments_each_preserve_unique_default(pg_db):
    old_a = _mapping(920051, 910001, 'BATCH-A', active=True, default=True)
    old_b = _mapping(920052, 910002, 'BATCH-B', active=True, default=True)
    pg_db.add_all([old_a, old_b])
    pg_db.flush()

    result = _sync(
        pg_db,
        ('BATCH-A', 'VAR-BATCH-A', 'SQUARE-C'),
        ('BATCH-B', 'VAR-BATCH-B', 'SQUARE-C'),
    )

    assert [row.vendor_id for row in _active_defaults(pg_db, 'BATCH-A')] == [910003]
    assert [row.vendor_id for row in _active_defaults(pg_db, 'BATCH-B')] == [910003]
    assert old_a.active is True and old_a.is_default_vendor is False
    assert old_b.active is True and old_b.is_default_vendor is False
    assert result['vendor_reassigned'] == 2


def test_postgres_later_failure_rollback_restores_original_default(postgres_engine):
    sku = f'ROLLBACK-{uuid.uuid4().hex[:8]}'
    with Session(postgres_engine) as setup:
        setup.add_all([
            Vendor(id=930001, square_vendor_id='ROLLBACK-A', name='Rollback A', active=True),
            Vendor(id=930002, square_vendor_id='ROLLBACK-B', name='Rollback B', active=True),
            _mapping(930011, 930001, sku, active=True, default=True),
        ])
        setup.commit()
    try:
        with Session(postgres_engine) as db:
            result = _sync(db, (sku, f'VAR-{sku}', 'ROLLBACK-B'))
            assert result['vendor_reassigned'] == 1
            assert [row.vendor_id for row in _active_defaults(db, sku)] == [930002]
            db.rollback()  # Equivalent to route/session rollback after a later failure.

        with Session(postgres_engine) as verify:
            rows = verify.scalars(select(VendorSkuConfig).where(VendorSkuConfig.sku == sku)).all()
            assert [(row.vendor_id, row.active, row.is_default_vendor) for row in rows] == [
                (930001, True, True)
            ]
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(text('DELETE FROM vendor_sku_configs WHERE sku = :sku'), {'sku': sku})
            connection.execute(text('DELETE FROM vendors WHERE id IN (930001, 930002)'))
