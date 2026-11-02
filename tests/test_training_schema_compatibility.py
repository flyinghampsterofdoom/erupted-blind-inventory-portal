"""The additive training schema retains exact target and capability boundaries."""
import pytest
from sqlalchemy import text
from app import models as m
from app.purchasing_v4.production_target import OwnerInspectionTarget
from test_v2_scheduling_foundation import scheduling_db


def test_additive_training_schema_is_accepted_but_unknown_revision_is_not(scheduling_db):
    Session,actor,ids,engine=scheduling_db
    with Session() as db:
        owner=m.Principal(username='training-schema-owner',password_hash='unused',role=m.PrincipalRole.ADMIN,active=True)
        db.add(owner);db.commit();owner_id=owner.id
    target=OwnerInspectionTarget(database_name=engine.url.database,hostname=engine.url.host,
        database_user=engine.url.username,owner_principal_id=owner_id)
    with engine.connect() as c:
        target.verify(c)
        tx=c.get_transaction()
        c.execute(text("UPDATE alembic_version SET version_num='unreviewed_future_schema'"))
        with pytest.raises(RuntimeError,match='qualified public schema'):target.verify(c)
        tx.rollback()
    wrong=OwnerInspectionTarget(database_name=engine.url.database,hostname=engine.url.host,
        database_user='different-owner',owner_principal_id=owner_id)
    with engine.connect() as c:
        with pytest.raises(PermissionError,match='target mismatch'):wrong.verify(c)
