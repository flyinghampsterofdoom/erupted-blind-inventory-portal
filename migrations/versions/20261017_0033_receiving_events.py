"""Add immutable purchasing receiving evidence; leave all legacy rows untouched."""
from alembic import op
import sqlalchemy as sa

revision = '20261017_0033'
down_revision = '20261017_0032'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('purchase_order_receipts', sa.Column('event_kind', sa.String(16), nullable=True))
    op.add_column('purchase_order_receipts', sa.Column('request_key', sa.String(128), nullable=True))
    op.add_column('purchase_order_receipts', sa.Column('request_payload', sa.JSON(), nullable=True))
    op.create_unique_constraint('purchase_order_receipts_request_key_key', 'purchase_order_receipts', ['request_key'])
    op.create_table('purchase_order_receipt_allocations',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('receipt_line_id',sa.BigInteger(),sa.ForeignKey('purchase_order_receipt_lines.id'),nullable=False),
        sa.Column('purchase_order_line_id',sa.BigInteger(),sa.ForeignKey('purchase_order_lines.id'),nullable=False),
        sa.Column('store_id',sa.BigInteger(),sa.ForeignKey('stores.id'),nullable=False),
        sa.Column('square_variation_id',sa.Text(),nullable=False),
        sa.Column('event_kind',sa.String(16),nullable=False),
        sa.Column('quantity_delta',sa.Integer(),nullable=False),
        sa.Column('closed_quantity',sa.Integer(),nullable=False,server_default='0'),
        sa.Column('corrected_quantity',sa.Integer()),
        sa.Column('original_allocation_id',sa.BigInteger(),sa.ForeignKey('purchase_order_receipt_allocations.id')),
        sa.Column('square_sync_event_id',sa.BigInteger(),sa.ForeignKey('square_sync_events.id')),
        sa.Column('posting_not_required',sa.Boolean(),nullable=False,server_default='false'),
        sa.Column('created_by_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint('receipt_line_id','store_id',name='po_receipt_alloc_store_uniq'),
        sa.CheckConstraint("event_kind IN ('DELIVERY', 'CORRECTION', 'CLOSURE')",name='po_receipt_alloc_kind_ck'),
        sa.CheckConstraint("(event_kind = 'DELIVERY' AND quantity_delta > 0 AND closed_quantity = 0 AND original_allocation_id IS NULL) OR "
                           "(event_kind = 'CORRECTION' AND closed_quantity = 0 AND corrected_quantity IS NOT NULL AND corrected_quantity >= 0 AND original_allocation_id IS NOT NULL) OR "
                           "(event_kind = 'CLOSURE' AND quantity_delta = 0 AND closed_quantity > 0 AND original_allocation_id IS NULL)",name='po_receipt_alloc_quantities_ck'))
    for name,cols in [('idx_po_receipt_alloc_line_store',['purchase_order_line_id','store_id']),
                      ('idx_po_receipt_alloc_operation',['square_sync_event_id']),
                      ('idx_po_receipt_alloc_original',['original_allocation_id'])]:
        op.create_index(name,'purchase_order_receipt_allocations',cols)
    op.execute("""
    CREATE FUNCTION purchasing_receipt_evidence_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_TABLE_NAME = 'purchase_order_receipt_allocations' THEN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Purchasing receiving evidence is immutable'; END IF;
        IF (to_jsonb(NEW) - 'square_sync_event_id' - 'posting_not_required') IS DISTINCT FROM
           (to_jsonb(OLD) - 'square_sync_event_id' - 'posting_not_required') OR
           (OLD.square_sync_event_id IS NOT NULL AND NEW.square_sync_event_id IS DISTINCT FROM OLD.square_sync_event_id) OR
           (OLD.posting_not_required AND NOT NEW.posting_not_required) THEN
          RAISE EXCEPTION 'Purchasing receiving evidence is immutable';
        END IF;
      ELSIF TG_TABLE_NAME = 'purchase_order_receipts' THEN
        IF OLD.event_kind IS NOT NULL THEN RAISE EXCEPTION 'Purchasing receiving evidence is immutable'; END IF;
      ELSIF TG_TABLE_NAME = 'purchase_order_receipt_lines' THEN
        IF EXISTS (SELECT 1 FROM purchase_order_receipts WHERE id=OLD.receipt_id AND event_kind IS NOT NULL) THEN
          RAISE EXCEPTION 'Purchasing receiving evidence is immutable';
        END IF;
      ELSE
        IF OLD.sync_type='PURCHASING_RECEIPT_V3' THEN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Purchasing posting evidence is immutable'; END IF;
          IF NEW.request_payload::jsonb IS DISTINCT FROM OLD.request_payload::jsonb OR
             ROW(NEW.idempotency_key,NEW.purchase_order_id,NEW.purchase_order_line_id,NEW.store_id,NEW.sync_type)
               IS DISTINCT FROM ROW(OLD.idempotency_key,OLD.purchase_order_id,OLD.purchase_order_line_id,OLD.store_id,OLD.sync_type) OR
             (OLD.status='SUCCESS' AND NEW.status <> 'SUCCESS') THEN
            RAISE EXCEPTION 'Purchasing posting identity and payload are immutable';
          END IF;
        END IF;
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER purchasing_receipt_alloc_guard BEFORE UPDATE OR DELETE ON purchase_order_receipt_allocations
      FOR EACH ROW EXECUTE FUNCTION purchasing_receipt_evidence_guard();
    CREATE TRIGGER purchasing_receipt_header_guard BEFORE UPDATE OR DELETE ON purchase_order_receipts
      FOR EACH ROW EXECUTE FUNCTION purchasing_receipt_evidence_guard();
    CREATE TRIGGER purchasing_receipt_line_guard BEFORE UPDATE OR DELETE ON purchase_order_receipt_lines
      FOR EACH ROW EXECUTE FUNCTION purchasing_receipt_evidence_guard();
    CREATE TRIGGER purchasing_receipt_operation_guard BEFORE UPDATE OR DELETE ON square_sync_events
      FOR EACH ROW EXECUTE FUNCTION purchasing_receipt_evidence_guard();
    """)


def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS(SELECT 1 FROM purchase_order_receipt_allocations) OR EXISTS(SELECT 1 FROM purchase_order_receipts WHERE event_kind IS NOT NULL) OR EXISTS(SELECT 1 FROM square_sync_events WHERE sync_type='PURCHASING_RECEIPT_V3')")).scalar():
        raise RuntimeError('Refusing to discard purchasing receiving evidence; retain the additive schema.')
    for table,name in [('purchase_order_receipt_allocations','purchasing_receipt_alloc_guard'),
                       ('purchase_order_receipts','purchasing_receipt_header_guard'),
                       ('purchase_order_receipt_lines','purchasing_receipt_line_guard'),
                       ('square_sync_events','purchasing_receipt_operation_guard')]:
        op.execute(f'DROP TRIGGER {name} ON {table}')
    op.execute('DROP FUNCTION purchasing_receipt_evidence_guard()')
    op.drop_table('purchase_order_receipt_allocations')
    op.drop_constraint('purchase_order_receipts_request_key_key','purchase_order_receipts',type_='unique')
    for name in ('request_payload','request_key','event_kind'): op.drop_column('purchase_order_receipts',name)
