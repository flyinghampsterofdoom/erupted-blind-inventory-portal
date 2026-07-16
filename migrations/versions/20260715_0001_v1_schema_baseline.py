"""V1 deployed SQL schema baseline.

Revision ID: 20260715_0001
Revises: None
Create Date: 2026-07-15

The deployed SQL is intentionally executed verbatim. It contains PostgreSQL-only
extensions, enums, additive compatibility ALTERs, constraints, functions, indexes,
and triggers that ORM autogeneration cannot reproduce faithfully.
"""
import hashlib
from pathlib import Path

from alembic import op


revision = '20260715_0001'
down_revision = None
branch_labels = None
depends_on = None
BASELINE_SQL_SHA256 = 'a6610bc92bbe5199aa404553548e31a40466e1c23c9c7c2a93b20a54779ec40a'


def _baseline_sql() -> str:
    sql = (Path(__file__).resolve().parents[2] / 'sql' / 'schema.sql').read_text(encoding='utf-8')
    digest = hashlib.sha256(sql.encode('utf-8')).hexdigest()
    if digest != BASELINE_SQL_SHA256:
        raise RuntimeError(
            'The immutable V1 baseline SQL changed. Restore sql/schema.sql and add a new Alembic revision.'
        )
    return sql


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_baseline_sql())


def downgrade() -> None:
    raise RuntimeError('The V1 baseline is non-destructive and cannot be downgraded automatically.')
