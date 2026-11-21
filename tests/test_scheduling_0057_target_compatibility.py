"""Additive Explorer schema retains exact Purchasing and collector guards."""
from dataclasses import replace

import pytest
from sqlalchemy import select, text

from app import models as m
from app.schema_contract import HEAD_REVISION
from app.purchasing_v4.production_target import OwnerInspectionTarget
from app.services.daily_evidence_migration import READ, install
from app.services.evidence_collector_target import ROLE, PROTECTED
from test_v2_scheduling_foundation import scheduling_db


HEAD = HEAD_REVISION
SUPPORTED = ('20261119_0057', '20261120_0058')


def target(engine, owner_id):
    return OwnerInspectionTarget(engine.url.database, str(engine.url.host), owner_id, engine.url.username)


def installer(engine, **changes):
    return dict(database_name=engine.url.database, hostname=str(engine.url.host),
        owner=engine.url.username, password='isolated-0057-fixture-password', **changes)


@pytest.mark.parametrize('supported', SUPPORTED)
def test_additive_owner_inspection_accepts_real_target_with_read_only_connection(scheduling_db, supported):
    Session, actor, _, engine = scheduling_db
    with Session() as db:
        db.get(m.Principal, actor.id).role = m.PrincipalRole.ADMIN
        db.commit()
    with engine.begin() as c:
        assert c.scalar(text('SELECT version_num FROM public.alembic_version')) == HEAD
        c.execute(text('UPDATE public.alembic_version SET version_num=:head'), {'head': supported})
    try:
        with engine.connect() as c:
            c.execute(text('SET TRANSACTION READ ONLY'))
            assert c.scalar(text('SELECT version_num FROM public.alembic_version')) == supported
            target(engine, actor.id).verify(c)
            assert c.scalar(text('SHOW transaction_read_only')) == 'on'
    finally:
        with engine.begin() as c:
            c.execute(text('UPDATE public.alembic_version SET version_num=:head'), {'head': HEAD})


@pytest.mark.parametrize('unsupported', ['20261026_0052', '20261121_0059'])
def test_unqualified_public_revisions_remain_rejected_by_both_targets(scheduling_db, unsupported):
    _, actor, _, engine = scheduling_db
    with engine.connect() as c:
        c.execute(text('UPDATE public.alembic_version SET version_num=:head'), {'head': unsupported})
        with pytest.raises(RuntimeError, match='qualified public schema'):
            target(engine, actor.id).verify(c)
        with pytest.raises(RuntimeError, match='reviewed public schema'):
            install(c, **installer(engine))
        assert c.scalar(text("SELECT to_regnamespace('purchasing_evidence')")) is None
        assert not c.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)'), {'role': ROLE})
        c.rollback()
    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM public.alembic_version')) == HEAD


@pytest.mark.parametrize('state', ['inactive', 'non_owner', 'missing'])
def test_additive_schema_does_not_weaken_active_owner_requirement(scheduling_db, state):
    Session, actor, _, engine = scheduling_db
    with Session() as db:
        account = db.get(m.Principal, actor.id)
        account.role = m.PrincipalRole.ADMIN if state != 'non_owner' else m.PrincipalRole.MANAGER
        account.active = state != 'inactive'
        db.commit()
    with engine.connect() as c:
        owner_id = actor.id + 10000 if state == 'missing' else actor.id
        with pytest.raises(PermissionError, match='Configured purchasing owner is not active'):
            target(engine, owner_id).verify(c)


def test_additive_schema_keeps_exact_database_hostname_and_user_binding(scheduling_db):
    _, actor, _, engine = scheduling_db
    expected = target(engine, actor.id)
    with engine.connect() as c:
        for altered in (replace(expected, database_name='different_database'),
                        replace(expected, hostname='different.example.test'),
                        replace(expected, database_user='different_user')):
            with pytest.raises(PermissionError, match='database target mismatch'):
                altered.verify(c)
        with pytest.raises(PermissionError, match='database target mismatch'):
            expected.verify_url(engine.url.set(query={'host': 'different.example.test'}))
        with pytest.raises(PermissionError, match='Migration target mismatch'):
            install(c, **{**installer(engine), 'database_name': 'different_database'})
        with pytest.raises(PermissionError, match='Migration target mismatch'):
            install(c, **{**installer(engine), 'hostname': 'different.example.test'})
        with pytest.raises(PermissionError, match='Migration server/owner mismatch'):
            install(c, **{**installer(engine), 'owner': 'different_user'})


@pytest.mark.parametrize('supported', SUPPORTED)
def test_collector_installer_accepts_qualified_revision_and_keeps_restricted_grants_and_business_rows(scheduling_db, supported):
    _, _, _, engine = scheduling_db
    tables = ('principals', 'employees', 'stores', 'schedule_shifts',
              'schedule_coverage_commitments', 'purchase_orders', 'vendor_sku_configs')

    def rows(c):
        return {table: list(c.scalars(text('SELECT row_to_json(t)::text FROM public.' + table + ' t ORDER BY id')))
                for table in tables}

    with engine.connect() as c:
        assert c.scalar(text('SELECT version_num FROM public.alembic_version')) == HEAD
        c.execute(text('UPDATE public.alembic_version SET version_num=:head'), {'head': supported})
        assert not c.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)'), {'role': ROLE})
        before = rows(c)
        try:
            installed = install(c, **installer(engine))
            assert installed == {'status': 'INSTALLED', 'version': 'daily_1',
                                 'worker': ROLE, 'broader_v4_schema_installed': False}
            assert c.scalar(text('SELECT version_num FROM public.alembic_version')) == supported
            assert tuple(c.execute(text('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls '
                                        'FROM pg_roles WHERE rolname=:role'), {'role': ROLE}).one()) == (False,) * 5
            for table in (*PROTECTED, 'schedule_coverage_commitments', 'employee_training'):
                assert not c.scalar(text("SELECT has_table_privilege(:role,:table,'INSERT,UPDATE,DELETE,TRUNCATE')"),
                                    {'role': ROLE, 'table': 'public.' + table})
            for table in READ:
                assert c.scalar(text("SELECT has_table_privilege(:role,:table,'SELECT')"),
                                {'role': ROLE, 'table': 'public.' + table})
            for table in ('fi_flavor_identities', 'fi_product_photo_assets', 'cp_products'):
                if c.scalar(text('SELECT to_regclass(:table)'), {'table': 'public.' + table}) is not None:
                    assert not c.scalar(text("SELECT has_table_privilege(:role,:table,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')"),
                                        {'role': ROLE, 'table': 'public.' + table})
            assert install(c, **installer(engine)) == {'status': 'ALREADY_INSTALLED', 'version': 'daily_1'}
            with pytest.raises(PermissionError, match='Migration target mismatch'):
                install(c, **{**installer(engine), 'database_name': 'different_database'})
            with pytest.raises(PermissionError, match='Migration server/owner mismatch'):
                install(c, **{**installer(engine), 'owner': 'different_user'})
            assert rows(c) == before
        finally:
            # The database and role are disposable fixtures; leave no cluster role behind.
            c.rollback()
    with engine.connect() as c:
        assert not c.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)'), {'role': ROLE})
        assert c.scalar(text("SELECT to_regnamespace('purchasing_evidence')")) is None
        assert c.scalar(text('SELECT version_num FROM public.alembic_version')) == HEAD
