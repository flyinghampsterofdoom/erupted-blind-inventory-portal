"""Persist narrow current/prospective Purchasing owner inputs; no history backfill."""
from alembic import op
import sqlalchemy as sa
revision = '20261018_0038'
down_revision = '20261017_0035'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('purchasing_owner_decisions',
        sa.Column('kind',sa.String(16),primary_key=True),
        sa.Column('subject_key',sa.Text(),primary_key=True),
        sa.Column('value',sa.JSON(),nullable=False),
        sa.Column('version',sa.Integer(),nullable=False),
        sa.Column('actor_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('updated_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.CheckConstraint("kind IN ('FINANCING','COST','CARD')",name='purchasing_owner_decision_kind_ck'),
        sa.CheckConstraint('version > 0',name='purchasing_owner_decision_version_ck'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM purchasing_owner_decisions)')).scalar():
        raise RuntimeError('Refusing to discard owner decisions; retain additive schema')
    op.drop_table('purchasing_owner_decisions')
