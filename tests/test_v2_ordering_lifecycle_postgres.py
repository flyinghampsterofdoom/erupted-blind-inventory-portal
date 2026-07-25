import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.schema_contract import upgrade_database
from app.services.v2_ordering_lifecycle_repository import load_lifecycle_states
from app.services.v2_ordering_lifecycle_service import (
    LifecycleCommand,
    LifecycleSelection,
    LifecycleTransitionError,
    transition_lifecycle,
)


ADMIN_URL = os.getenv('TEST_POSTGRES_ADMIN_URL')


@pytest.mark.skipif(not ADMIN_URL, reason='set TEST_POSTGRES_ADMIN_URL for Ordering lifecycle PostgreSQL integration')
def test_postgres_atomic_batch_audit_and_optimistic_conflict():
    admin_engine = create_engine(ADMIN_URL, isolation_level='AUTOCOMMIT')
    database_name = f'erupted_lifecycle_{uuid.uuid4().hex[:10]}'
    database_url = f"{ADMIN_URL.rsplit('/', 1)[0]}/{database_name}"
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = create_engine(database_url)
    try:
        upgrade_database(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO principals (id, username, password_hash, role, active) "
                    "VALUES (900001, 'lifecycle-owner', 'not-a-login-hash', 'ADMIN', true)"
                )
            )
        with Session(engine) as db:
            first = transition_lifecycle(
                db,
                command=LifecycleCommand.SET_NO_FUTURE_REORDER,
                selections=(LifecycleSelection('VAR-1', 0), LifecycleSelection('VAR-2', 0)),
                actor_principal_id=900001,
            )
            db.commit()
            assert first.changed_count == 2
            assert {state.status for state in load_lifecycle_states(db, {'VAR-1', 'VAR-2'}).values()} == {
                'NO_FUTURE_REORDER'
            }
            assert db.execute(
                text("SELECT count(*) FROM audit_log WHERE action='V2:ordering_lifecycle:lifecycle_status_changed'")
            ).scalar_one() == 2

        with Session(engine) as first_session, Session(engine) as stale_session:
            first_version = load_lifecycle_states(first_session, {'VAR-1'})['VAR-1'].row_version
            stale_version = load_lifecycle_states(stale_session, {'VAR-1'})['VAR-1'].row_version
            transition_lifecycle(
                first_session,
                command=LifecycleCommand.SET_ACTIVE,
                selections=(LifecycleSelection('VAR-1', first_version),),
                actor_principal_id=900001,
            )
            first_session.commit()
            with pytest.raises(LifecycleTransitionError) as conflict:
                transition_lifecycle(
                    stale_session,
                    command=LifecycleCommand.SET_ACTIVE,
                    selections=(LifecycleSelection('VAR-1', stale_version),),
                    actor_principal_id=900001,
                )
            stale_session.rollback()
            assert conflict.value.code == 'STALE_VERSION'

        with Session(engine) as db:
            before = load_lifecycle_states(db, {'VAR-1', 'VAR-2'})
            with pytest.raises(LifecycleTransitionError):
                transition_lifecycle(
                    db,
                    command=LifecycleCommand.ARCHIVE,
                    selections=(
                        LifecycleSelection('VAR-1', before['VAR-1'].row_version),
                        LifecycleSelection('VAR-2', before['VAR-2'].row_version - 1),
                    ),
                    actor_principal_id=900001,
                )
            db.rollback()
            after = load_lifecycle_states(db, {'VAR-1', 'VAR-2'})
            assert after == before
    finally:
        engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        admin_engine.dispose()
