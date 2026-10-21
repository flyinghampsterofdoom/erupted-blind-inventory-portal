"""Immutable Monday payment batches linked to existing financial records; no backfill."""
from alembic import op
import sqlalchemy as sa
revision='20261017_0035'
down_revision='20261017_0034'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('purchasing_payment_batches',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('sales_start',sa.Date(),nullable=False),sa.Column('sales_end',sa.Date(),nullable=False),
        sa.Column('monday',sa.Date(),nullable=False),sa.Column('payment_date',sa.Date(),nullable=False),
        sa.Column('revision',sa.Integer(),nullable=False),sa.Column('request_key',sa.String(128),nullable=False,unique=True),
        sa.Column('supersedes_id',sa.BigInteger(),sa.ForeignKey('purchasing_payment_batches.id'),unique=True),
        sa.Column('request_payload',sa.JSON(),nullable=False),sa.Column('recommendations',sa.JSON(),nullable=False),
        sa.Column('note',sa.Text()),sa.Column('actor_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),server_default=sa.func.now(),nullable=False),
        sa.UniqueConstraint('sales_start','revision',name='purchasing_payment_week_revision_uniq'),
        sa.CheckConstraint('revision > 0',name='purchasing_payment_revision_ck'),
        sa.CheckConstraint('sales_end = sales_start + 6',name='purchasing_payment_week_ck'),
        sa.CheckConstraint('monday = sales_end + 2 AND extract(dow FROM sales_start)=0',name='purchasing_payment_calendar_ck'))
    op.create_table('purchasing_payment_batch_entries',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('batch_id',sa.BigInteger(),sa.ForeignKey('purchasing_payment_batches.id'),nullable=False),
        sa.Column('role',sa.String(12),nullable=False),
        sa.Column('account_id',sa.BigInteger(),sa.ForeignKey('funding_accounts.id'),nullable=False),
        sa.Column('actual',sa.Numeric(14,2),nullable=False),
        sa.Column('report_id',sa.BigInteger(),sa.ForeignKey('funding_reports.id')),
        sa.Column('payment_id',sa.BigInteger(),sa.ForeignKey('funding_payments.id'),unique=True),
        sa.Column('reversal_id',sa.BigInteger(),sa.ForeignKey('funding_payments.id'),unique=True),
        sa.UniqueConstraint('batch_id','role',name='purchasing_payment_role_uniq'),
        sa.UniqueConstraint('batch_id','account_id',name='purchasing_payment_account_uniq'),
        sa.CheckConstraint("role IN ('big','streamline','card1','card2')",name='purchasing_payment_role_ck'),
        sa.CheckConstraint('actual >= 0',name='purchasing_payment_amount_ck'))
    # A batch is sealed only at transaction end, after the existing writers have
    # supplied their IDs. Later updates/deletes fail even outside application code.
    op.execute('''CREATE FUNCTION purchasing_payment_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN IF OLD.xmin::text::bigint <> txid_current() THEN
      RAISE EXCEPTION 'Weekly payment history is immutable; append an explicit correction';
    END IF; IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW; END $$;
    CREATE TRIGGER purchasing_payment_batch_immutable BEFORE UPDATE OR DELETE ON purchasing_payment_batches
      FOR EACH ROW EXECUTE FUNCTION purchasing_payment_immutable();
    CREATE TRIGGER purchasing_payment_entry_immutable BEFORE UPDATE OR DELETE ON purchasing_payment_batch_entries
      FOR EACH ROW EXECUTE FUNCTION purchasing_payment_immutable();
    CREATE FUNCTION purchasing_payment_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE bid bigint; BEGIN
      bid=NEW.id;
      IF (SELECT count(*) FROM purchasing_payment_batch_entries WHERE batch_id=bid)<>4 THEN
        RAISE EXCEPTION 'A weekly batch requires four account entries'; END IF;
      IF EXISTS(SELECT 1 FROM purchasing_payment_batch_entries e LEFT JOIN funding_payments p ON p.id=e.payment_id
        WHERE e.batch_id=bid AND ((e.actual>0 AND (p.id IS NULL OR p.account_id<>e.account_id OR p.amount<>e.actual OR p.reversed_payment_id IS NOT NULL))
          OR (e.actual=0 AND e.payment_id IS NOT NULL))) THEN RAISE EXCEPTION 'Weekly payment entries must match their existing money records'; END IF;
      IF NEW.supersedes_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_payment_batches b
        WHERE b.id=NEW.supersedes_id AND b.sales_start=NEW.sales_start AND b.revision+1=NEW.revision) THEN
        RAISE EXCEPTION 'Correction must append to its own weekly batch'; END IF;
      RETURN NEW;
    END $$;
    CREATE CONSTRAINT TRIGGER purchasing_payment_batch_complete AFTER INSERT ON purchasing_payment_batches
      DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_payment_complete();''')

def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM purchasing_payment_batches)')).scalar():
        raise RuntimeError('Refusing to discard weekly payment history; retain the additive schema')
    op.execute('DROP FUNCTION purchasing_payment_complete() CASCADE')
    op.execute('DROP FUNCTION purchasing_payment_immutable() CASCADE')
    op.drop_table('purchasing_payment_batch_entries');op.drop_table('purchasing_payment_batches')
