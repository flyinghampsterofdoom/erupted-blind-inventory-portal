import pytest
from alembic import command
from sqlalchemy import text,select
from sqlalchemy.exc import IntegrityError
from app import models as m
from app.auth import Principal,Role
from app.schema_contract import _alembic_config,current_revision,HEAD_REVISION
from app.services.employee_profile_service import save_employee_account
from app.services.inventory_working_store_service import assert_inventory_store,WorkingStoreConflict
from app.services.session_service import get_session_for_principal
from test_v2_scheduling_foundation import scheduling_db
from test_v2_employee_profiles import _admin


def test_0034_upgrade_preserves_every_existing_row_and_refuses_lossy_downgrade(scheduling_db):
    Session,actor,ids,engine=scheduling_db
    cfg=_alembic_config(engine.url.render_as_string(hide_password=False))
    command.downgrade(cfg,'20261014_0034')
    with engine.begin() as c:
        c.execute(text("INSERT INTO principals (username,role,store_id,active) VALUES ('legacy-shared','STORE',:sid,true)"),{'sid':ids['north']})
        tables=list(c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename<>'alembic_version' ORDER BY tablename")).scalars())
        before={t:list(c.execute(text(f'SELECT row_to_json(r)::text FROM "{t}" r ORDER BY row_to_json(r)::text')).scalars()) for t in tables}
    command.upgrade(cfg,'head')
    with engine.connect() as c:
        after={t:list(c.execute(text(f'SELECT row_to_json(r)::text FROM "{t}" r ORDER BY row_to_json(r)::text')).scalars()) for t in tables}
        assert after==before
    assert current_revision(engine)==HEAD_REVISION
    with Session() as db:
        admin=_admin(db)
        account=save_employee_account(db,actor=admin,employee_id=ids['alex'],action='create',login='scheduled-only@example.test',role='STORE',store_id=None,confirmed=True)
        assert account.store_id is None and db.get(m.Employee,ids['alex']).principal_id==account.id
        db.commit()
    with pytest.raises(RuntimeError,match='forward recovery'):command.downgrade(cfg,'20261014_0034')
    assert current_revision(engine)==HEAD_REVISION
    with engine.begin() as c:
        with pytest.raises(IntegrityError):
            with c.begin_nested():c.execute(text("INSERT INTO principals (username,role,store_id,active) VALUES ('invalid-manager','MANAGER',:sid,true)"),{'sid':ids['north']})


def test_explicit_management_oversight_preserved_without_employee_schedule(scheduling_db):
    Session,actor,ids,_=scheduling_db
    with Session() as db:
        db.get(m.Employee,ids['alex']).principal_id=actor.id
        campaign=m.Campaign(label='Synthetic');db.add(campaign);db.flush()
        count=m.CountSession(store_id=ids['south'],campaign_id=campaign.id,employee_name='Original store',created_by_principal_id=actor.id);db.add(count);db.flush()
        assert get_session_for_principal(db,principal=actor,session_id=count.id).store_id==ids['south']
        db.add(m.PrincipalPermissionOverride(principal_id=actor.id,permission_key='management.access',allowed=False));db.flush()
        with pytest.raises(WorkingStoreConflict):assert_inventory_store(db,principal=actor,store_id=ids['south'])
