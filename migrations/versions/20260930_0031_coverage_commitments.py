"""Auditable replacement commitments, separate from actual attendance."""
from alembic import op
import sqlalchemy as sa

revision = '20260930_0031'
down_revision = '20260930_0030'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('schedule_coverage_commitments',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('schedule_shift_id', sa.BigInteger(), sa.ForeignKey('schedule_shifts.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('recorded_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('voided_at', sa.DateTime(timezone=True)),
        sa.Column('voided_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT')),
        sa.Column('void_reason', sa.Text()),
        sa.CheckConstraint('char_length(note) <= 2000', name='coverage_commitment_note_ck'))
    op.create_index('coverage_commitment_one_active_shift_uniq', 'schedule_coverage_commitments',
                    ['schedule_shift_id'], unique=True, postgresql_where=sa.text('voided_at IS NULL'))


def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM schedule_coverage_commitments LIMIT 1')).first():
        raise RuntimeError('Coverage commitment audit history must be preserved; downgrade refused.')
    op.drop_table('schedule_coverage_commitments')
