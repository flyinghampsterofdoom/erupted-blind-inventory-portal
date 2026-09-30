"""Immutable generator evidence; no legacy backfill."""
from alembic import op
import sqlalchemy as sa

revision = '20260928_0028'
down_revision = '20260927_0027'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'schedule_generator_snapshots',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('schedule_period_id', sa.BigInteger(), sa.ForeignKey('schedule_periods.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('batch_id', sa.String(36), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('generation_kind', sa.String(20), nullable=False),
        sa.Column('actor_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='RESTRICT')),
        sa.Column('origin', sa.String(80), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('version_before', sa.Integer(), nullable=False),
        sa.Column('version_after', sa.Integer(), nullable=False),
        sa.Column('schema_version', sa.Integer(), nullable=False),
        sa.Column('build_identity', sa.Text()),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('checksum', sa.String(64), nullable=False),
        sa.UniqueConstraint('schedule_period_id', 'sequence', name='schedule_generator_snapshots_sequence_uniq'),
        sa.CheckConstraint('sequence > 0 AND schema_version > 0', name='schedule_generator_snapshots_positive_ck'),
        sa.CheckConstraint("generation_kind IN ('INITIAL', 'REGENERATION')", name='schedule_generator_snapshots_kind_ck'),
    )
    op.execute("""
        CREATE FUNCTION reject_schedule_generator_snapshot_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Generator snapshots are immutable audit evidence';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER schedule_generator_snapshots_immutable
        BEFORE UPDATE OR DELETE OR TRUNCATE ON schedule_generator_snapshots
        FOR EACH STATEMENT EXECUTE FUNCTION reject_schedule_generator_snapshot_mutation()
    """)


def downgrade():
    # Never silently destroy historical evidence as part of rollback.
    connection = op.get_bind()
    if connection.execute(sa.text('SELECT EXISTS (SELECT 1 FROM schedule_generator_snapshots)')).scalar():
        raise RuntimeError('Cannot downgrade: generator snapshot evidence exists. Retain this additive table.')
    op.drop_table('schedule_generator_snapshots')
    op.execute('DROP FUNCTION reject_schedule_generator_snapshot_mutation()')
