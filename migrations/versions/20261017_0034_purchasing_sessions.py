"""Persistent owner purchasing decisions and immutable source revisions; no backfill."""
from alembic import op
import sqlalchemy as sa
revision='20261017_0034'
down_revision='20261017_0033'
branch_labels=None
depends_on=None


def upgrade():
    op.create_table('purchasing_sessions',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('owner_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('request_key',sa.String(128),nullable=False,unique=True),
        sa.Column('version',sa.Integer(),nullable=False,server_default='1'),
        sa.Column('settings',sa.JSON(),nullable=False),
        sa.Column('status',sa.String(16),nullable=False,server_default='ACTIVE'),
        sa.Column('revision',sa.Integer(),nullable=False,server_default='1'),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('ACTIVE','COMPLETE')",name='purchasing_session_status_ck'),
        sa.CheckConstraint('revision >= 1',name='purchasing_session_revision_ck'))
    op.create_index('idx_purchasing_session_owner_status','purchasing_sessions',['owner_id','status','updated_at'])
    op.create_table('purchasing_session_revisions',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('session_id',sa.BigInteger(),sa.ForeignKey('purchasing_sessions.id'),nullable=False),
        sa.Column('number',sa.Integer(),nullable=False),
        sa.Column('request_key',sa.String(128),nullable=False,unique=True),
        sa.Column('settings',sa.JSON(),nullable=False),
        sa.Column('evidence',sa.JSON(),nullable=False),
        sa.Column('created_by',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('session_id','number',name='purchasing_revision_number_uniq'),
        sa.CheckConstraint('number >= 1',name='purchasing_revision_number_ck'))
    op.create_table('purchasing_session_lines',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('session_id',sa.BigInteger(),sa.ForeignKey('purchasing_sessions.id'),nullable=False),
        sa.Column('variation_id',sa.Text(),nullable=False),
        sa.Column('canonical_vendor_id',sa.BigInteger(),sa.ForeignKey('vendors.id')),
        sa.Column('supplier_id',sa.BigInteger(),sa.ForeignKey('vendors.id')),
        sa.Column('accepted',sa.JSON(),nullable=False),sa.Column('proposed',sa.JSON()),
        sa.Column('quantity',sa.Integer(),nullable=False),sa.Column('allocations',sa.JSON(),nullable=False),
        sa.Column('unit_cost',sa.Numeric(14,4)),sa.Column('cost_source',sa.String(24),nullable=False),
        sa.Column('decisions',sa.JSON(),nullable=False),
        sa.Column('removed',sa.Boolean(),nullable=False,server_default='false'),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('session_id','variation_id',name='purchasing_session_variation_uniq'),
        sa.CheckConstraint('quantity >= 0 AND quantity <= 1000000',name='purchasing_line_quantity_ck'),
        sa.CheckConstraint('unit_cost IS NULL OR unit_cost >= 0',name='purchasing_line_cost_ck'))
    op.create_index('idx_purchasing_line_session_supplier','purchasing_session_lines',['session_id','supplier_id'])
    op.create_table('purchasing_session_groups',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('session_id',sa.BigInteger(),sa.ForeignKey('purchasing_sessions.id'),nullable=False),
        sa.Column('supplier_id',sa.BigInteger(),sa.ForeignKey('vendors.id'),nullable=False),
        sa.Column('status',sa.String(16),nullable=False,server_default='OPEN'),
        sa.Column('purchase_order_id',sa.BigInteger(),sa.ForeignKey('purchase_orders.id'),unique=True),
        sa.Column('intended_account_id',sa.BigInteger(),sa.ForeignKey('funding_accounts.id')),
        sa.Column('updated_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('session_id','supplier_id',name='purchasing_session_supplier_uniq'),
        sa.CheckConstraint("status IN ('OPEN','CONVERTED','DISCARDED')",name='purchasing_group_status_ck'),
        sa.CheckConstraint("(status = 'CONVERTED') = (purchase_order_id IS NOT NULL)",name='purchasing_group_po_ck'))
    op.create_index('idx_purchasing_group_session_status','purchasing_session_groups',['session_id','status'])
    op.execute("""
    CREATE FUNCTION purchasing_revision_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Purchasing source revisions are immutable'; END $$;
    CREATE TRIGGER purchasing_revision_guard BEFORE UPDATE OR DELETE ON purchasing_session_revisions
      FOR EACH ROW EXECUTE FUNCTION purchasing_revision_immutable();
    CREATE FUNCTION purchasing_conversion_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.purchase_order_id IS NOT NULL THEN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Purchasing conversion lineage is immutable'; END IF;
        IF ROW(NEW.session_id,NEW.supplier_id,NEW.purchase_order_id,NEW.status)
          IS DISTINCT FROM ROW(OLD.session_id,OLD.supplier_id,OLD.purchase_order_id,OLD.status) THEN
          RAISE EXCEPTION 'Purchasing conversion lineage is immutable';
        END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
    END $$;
    CREATE TRIGGER purchasing_conversion_guard BEFORE UPDATE OR DELETE ON purchasing_session_groups
      FOR EACH ROW EXECUTE FUNCTION purchasing_conversion_immutable();
    """)


def downgrade():
    if op.get_bind().execute(sa.text('SELECT EXISTS(SELECT 1 FROM purchasing_sessions)')).scalar():
        raise RuntimeError('Refusing to discard purchasing session history; retain the additive schema')
    op.execute('DROP TRIGGER purchasing_conversion_guard ON purchasing_session_groups')
    op.execute('DROP FUNCTION purchasing_conversion_immutable()')
    op.execute('DROP TRIGGER purchasing_revision_guard ON purchasing_session_revisions')
    op.execute('DROP FUNCTION purchasing_revision_immutable()')
    for table in ('purchasing_session_groups','purchasing_session_lines','purchasing_session_revisions','purchasing_sessions'):op.drop_table(table)
