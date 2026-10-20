from collections.abc import Generator

from sqlalchemy import create_engine
from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.performance import PerformanceSession, instrument_engine
from app.session_events import install_session_listeners

install_session_listeners()


engine = create_engine(settings.database_url_normalized, pool_pre_ping=True)
instrument_engine(engine)
SessionLocal = sessionmaker(class_=PerformanceSession, bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db(request: Request = None) -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        if request is not None:
            db.info['permission_override_maps'] = getattr(request.state, 'permission_override_maps', None)
        yield db
    finally:
        db.close()
