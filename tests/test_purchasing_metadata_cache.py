import pytest
import json
from types import SimpleNamespace
from sqlalchemy import create_engine,text,event
from sqlalchemy.orm import Session
from app.services.purchasing_metadata_cache import revision_metadata


def database(name):
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(text('create table purchasing_session_revisions (session_id bigint, number integer, evidence JSON)'))
        c.execute(text('insert into purchasing_session_revisions values (1,1,:payload)'),{'payload':json.dumps({'stores':{'1':name},'observed_at':'fixed'})})
    return engine


def test_revision_cache_isolated_by_database_revision_and_transaction():
    first=database('One');second=database('Two')
    current=SimpleNamespace(id=1,revision=1)
    calls=[];event.listen(first,'before_cursor_execute',lambda *a:calls.append(1))
    with Session(first) as db:
        assert revision_metadata(db,current)[0]=={'1':'One'}
        assert revision_metadata(db,current)[0]=={'1':'One'}
        assert len(calls)==1
        result=revision_metadata(db,current);result[0]['1']='Must not leak'
        assert revision_metadata(db,current)[0]=={'1':'One'}
        db.execute(text('insert into purchasing_session_revisions values (1,2,:p)'),{'p':json.dumps({'stores':{'1':'Uncommitted'},'observed_at':'new'})})
        assert revision_metadata(db,SimpleNamespace(id=1,revision=2))[0]=={'1':'Uncommitted'}
        db.rollback()
        # Rolled-back revisions must not have entered the process cache.
        import pytest
        from sqlalchemy.exc import NoResultFound
        with pytest.raises(NoResultFound):revision_metadata(db,SimpleNamespace(id=1,revision=2))
    with Session(second) as db:assert revision_metadata(db,current)[0]=={'1':'Two'}
    first.dispose();second.dispose()


@pytest.mark.parametrize('finish', ['commit', 'rollback'])
def test_outer_uncommitted_revision_cannot_publish_after_savepoint(finish):
    from app.services import purchasing_metadata_cache as cache
    engine = database('Committed')
    current = SimpleNamespace(id=1, revision=2)
    with Session(engine) as db:
        db.execute(text('insert into purchasing_session_revisions values (1,2,:p)'),
                   {'p': json.dumps({'stores': {'1': 'Outer uncommitted'}, 'observed_at': 'new'})})
        nested = db.begin_nested()
        db.execute(text('select 1'))
        getattr(nested, finish)()
        assert revision_metadata(db, current)[0] == {'1': 'Outer uncommitted'}
        assert (engine, 1, 2) not in cache._cache
        db.rollback()
        from sqlalchemy.exc import NoResultFound
        with pytest.raises(NoResultFound):
            revision_metadata(db, current)
    engine.dispose()
