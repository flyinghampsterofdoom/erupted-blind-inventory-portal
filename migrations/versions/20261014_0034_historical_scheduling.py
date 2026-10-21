"""Attested historical periods use existing revisions, never publication."""
from alembic import op
import sqlalchemy as sa
revision = '20261014_0034'
down_revision = '20261013_0033'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('schedule_periods', sa.Column('is_historical', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('schedule_periods', sa.Column('historical_locked_at', sa.DateTime(timezone=True)))
    op.add_column('schedule_periods', sa.Column('historical_locked_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id')))
    op.add_column('schedule_periods', sa.Column('historical_reason', sa.Text()))
    op.create_check_constraint('schedule_periods_historical_ck', 'schedule_periods',
        "NOT is_historical OR (published_at IS NULL AND published_by_principal_id IS NULL AND status <> 'PUBLISHED' AND generated_at IS NULL AND automatic_publication_at IS NULL)")
    op.create_check_constraint('schedule_periods_historical_lock_ck', 'schedule_periods',
        "historical_locked_at IS NULL OR (is_historical AND status = 'ARCHIVED' AND historical_locked_by_principal_id IS NOT NULL AND length(trim(historical_reason)) > 0)")
    op.execute('''CREATE FUNCTION protect_locked_historical_schedule() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      PERFORM pg_advisory_xact_lock(73120260824);
      IF TG_TABLE_NAME = 'schedule_periods' THEN
        IF OLD.historical_locked_at IS NOT NULL THEN RAISE EXCEPTION 'Locked historical schedule evidence is immutable'; END IF;
      ELSE
        IF (TG_OP <> 'INSERT' AND EXISTS (SELECT 1 FROM schedule_periods WHERE id=OLD.schedule_period_id AND historical_locked_at IS NOT NULL))
          OR (TG_OP <> 'DELETE' AND EXISTS (SELECT 1 FROM schedule_periods WHERE id=NEW.schedule_period_id AND historical_locked_at IS NOT NULL)) THEN
          RAISE EXCEPTION 'Locked historical schedule assignments are immutable';
        END IF;
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$''')
    op.execute('CREATE TRIGGER locked_historical_period BEFORE UPDATE OR DELETE ON schedule_periods FOR EACH ROW EXECUTE FUNCTION protect_locked_historical_schedule()')
    op.execute('CREATE TRIGGER locked_historical_shift BEFORE INSERT OR UPDATE OR DELETE ON schedule_shifts FOR EACH ROW EXECUTE FUNCTION protect_locked_historical_schedule()')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT 1 FROM schedule_periods WHERE is_historical LIMIT 1')).first():
        raise RuntimeError('Historical evidence requires compatible forward recovery; downgrade refused.')
    op.execute('DROP TRIGGER locked_historical_shift ON schedule_shifts')
    op.execute('DROP TRIGGER locked_historical_period ON schedule_periods')
    op.execute('DROP FUNCTION protect_locked_historical_schedule()')
    op.drop_constraint('schedule_periods_historical_lock_ck','schedule_periods')
    op.drop_constraint('schedule_periods_historical_ck','schedule_periods')
    for name in ('historical_reason','historical_locked_by_principal_id','historical_locked_at','is_historical'):
        op.drop_column('schedule_periods',name)
