"""Extend immutable evidence and review tracking without rewriting existing records."""
from alembic import op
import sqlalchemy as sa
revision = '20261020_0045'
down_revision = '20261020_0044'
branch_labels = depends_on = None


def upgrade():
    op.add_column('schedule_periods', sa.Column('review_due_at', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint('schedule_generator_snapshots_kind_ck', 'schedule_generator_snapshots', type_='check')
    op.create_check_constraint('schedule_generator_snapshots_kind_ck', 'schedule_generator_snapshots', "generation_kind IN ('INITIAL','REGENERATION','APPROVAL','PUBLICATION')")
    op.execute("""CREATE FUNCTION reject_schedule_approval_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Schedule approvals are immutable audit evidence'; END $$""")
    op.execute("""CREATE TRIGGER schedule_approvals_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON schedule_approvals
      FOR EACH STATEMENT EXECUTE FUNCTION reject_schedule_approval_mutation()""")
    op.execute("""CREATE FUNCTION protect_scheduling_audit() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF TG_OP='TRUNCATE' THEN
          IF EXISTS(SELECT 1 FROM audit_log WHERE action LIKE 'V2:SCHEDULING%:%') THEN
            RAISE EXCEPTION 'Scheduling audit evidence is immutable';
          END IF;
          RETURN NULL;
        END IF;
        IF OLD.action LIKE 'V2:SCHEDULING%:%' THEN
          RAISE EXCEPTION 'Scheduling audit evidence is immutable';
        END IF;
        IF TG_OP='UPDATE' THEN
          IF NEW.action LIKE 'V2:SCHEDULING%:%' THEN RAISE EXCEPTION 'Scheduling audit evidence is immutable'; END IF;
          RETURN NEW;
        END IF;
        RETURN OLD;
      END $$""")
    op.execute('CREATE TRIGGER scheduling_audit_immutable BEFORE UPDATE OR DELETE ON audit_log FOR EACH ROW EXECUTE FUNCTION protect_scheduling_audit()')
    op.execute('CREATE TRIGGER scheduling_audit_no_truncate BEFORE TRUNCATE ON audit_log FOR EACH STATEMENT EXECUTE FUNCTION protect_scheduling_audit()')


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM schedule_generator_snapshots WHERE generation_kind IN ('APPROVAL','PUBLICATION')) OR EXISTS(SELECT 1 FROM schedule_periods WHERE review_due_at IS NOT NULL)")).scalar():
        raise RuntimeError('Preserve lifecycle evidence and review tracking; use a compatible code rollback.')
    op.execute('DROP TRIGGER scheduling_audit_no_truncate ON audit_log')
    op.execute('DROP TRIGGER scheduling_audit_immutable ON audit_log')
    op.execute('DROP FUNCTION protect_scheduling_audit()')
    op.execute('DROP TRIGGER schedule_approvals_immutable ON schedule_approvals')
    op.execute('DROP FUNCTION reject_schedule_approval_mutation()')
    op.drop_constraint('schedule_generator_snapshots_kind_ck', 'schedule_generator_snapshots', type_='check')
    op.create_check_constraint('schedule_generator_snapshots_kind_ck', 'schedule_generator_snapshots', "generation_kind IN ('INITIAL','REGENERATION')")
    op.drop_column('schedule_periods', 'review_due_at')
