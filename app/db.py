from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings


engine = create_engine(settings.database_url_normalized, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def ensure_runtime_schema(db: Session | None = None) -> None:
    """Apply lightweight additive columns needed by the current app build."""
    statements = [
        'ALTER TABLE vendor_sku_configs ADD COLUMN IF NOT EXISTS gtin TEXT',
        'ALTER TABLE purchase_order_lines ADD COLUMN IF NOT EXISTS gtin TEXT',
    ]
    if db is not None:
        for statement in statements:
            db.execute(text(statement))
        return
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
