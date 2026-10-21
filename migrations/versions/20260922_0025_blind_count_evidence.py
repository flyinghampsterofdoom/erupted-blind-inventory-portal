"""Front/back drafts, immutable rounds, durable corrections and independent review."""
from alembic import op
import sqlalchemy as sa

revision = '20260922_0025'
down_revision = '20260915_0025'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('count_sessions', sa.Column('draft_revision', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('count_sessions', sa.Column('observation_closed', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('entries', sa.Column('front_qty', sa.Numeric(14,3)))
    op.add_column('entries', sa.Column('back_qty', sa.Numeric(14,3)))
    op.alter_column('entries', 'counted_qty', existing_type=sa.Numeric(14,3), nullable=True)
    op.alter_column('snapshot_lines', 'expected_on_hand', existing_type=sa.Numeric(14,3), nullable=True)
    op.alter_column('store_recount_items', 'last_variance', existing_type=sa.Numeric(14,3), nullable=True)
    op.create_check_constraint('entries_front_back_ck', 'entries', '(front_qty IS NULL OR front_qty >= 0) AND (back_qty IS NULL OR back_qty >= 0)')
    op.execute("UPDATE count_sessions SET observation_closed=true, status='SUBMITTED' WHERE status='SUBMITTED' OR submitted_at IS NOT NULL")
    op.execute("UPDATE snapshot_lines SET expected_on_hand=NULL WHERE session_id IN (SELECT id FROM count_sessions WHERE status='DRAFT')")
    for column in ('consecutive_match_count', 'total_count_attempts'):
        op.drop_constraint(f'store_recount_items_{column}_ck', 'store_recount_items', type_='check')
        op.create_check_constraint(f'store_recount_items_{column}_ck', 'store_recount_items', f'{column} >= 0')
    # Preserve queue membership, but old mutable streaks are not independent evidence.
    op.execute('UPDATE store_recount_items SET consecutive_match_count=0, total_count_attempts=0')
    # Explicit schemas, independent of future ORM changes.
    op.create_table('count_observations',
        sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('session_id',sa.BigInteger(),sa.ForeignKey('count_sessions.id'),nullable=False),
        sa.Column('store_id',sa.BigInteger(),sa.ForeignKey('stores.id'),nullable=False),
        sa.Column('variation_id',sa.Text(),nullable=False),sa.Column('sku',sa.Text()),
        sa.Column('item_name',sa.Text(),nullable=False),sa.Column('variation_name',sa.Text(),nullable=False),
        *[sa.Column(n,sa.Numeric(14,3),nullable=False) for n in ('front_qty','back_qty','total_qty')],
        sa.Column('expected_qty',sa.Numeric(14,3)),sa.Column('variance',sa.Numeric(14,3)),
        sa.Column('expected_provenance',sa.JSON(),nullable=False),
        sa.Column('counted_by_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('submitted_by_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('observed_at',sa.DateTime(timezone=True),nullable=False),sa.Column('submitted_at',sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint('session_id','variation_id',name='count_observation_round_product_uniq'),
        sa.CheckConstraint('front_qty >= 0 AND back_qty >= 0 AND total_qty = front_qty + back_qty',name='count_observation_total_ck'))
    op.create_index('count_observation_product_history','count_observations',['store_id','variation_id','id'])
    op.create_table('count_corrections',sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('trigger_observation_id',sa.BigInteger(),sa.ForeignKey('count_observations.id'),nullable=False,unique=True),
        sa.Column('observation_ids',sa.JSON(),nullable=False),
        sa.Column('store_id',sa.BigInteger(),sa.ForeignKey('stores.id'),nullable=False),sa.Column('variation_id',sa.Text(),nullable=False),
        sa.Column('operation_id',sa.String(64),nullable=False,unique=True),sa.Column('request_payload',sa.JSON(),nullable=False),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('response_payload',sa.JSON()),sa.Column('error_text',sa.Text()),
        sa.Column('attempts',sa.Integer(),nullable=False),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),sa.Column('succeeded_at',sa.DateTime(timezone=True)))
    op.create_table('count_correction_attempts',sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('correction_id',sa.BigInteger(),sa.ForeignKey('count_corrections.id'),nullable=False),
        sa.Column('actor_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id'),nullable=False),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('response_payload',sa.JSON()),sa.Column('error_text',sa.Text()),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()))
    op.create_table('count_reviews',sa.Column('id',sa.BigInteger(),primary_key=True),
        sa.Column('correction_id',sa.BigInteger(),sa.ForeignKey('count_corrections.id'),nullable=False,unique=True),
        sa.Column('status',sa.String(24),nullable=False),sa.Column('explanation',sa.Text()),sa.Column('notes',sa.Text()),
        sa.Column('related_correction_ids',sa.JSON(),nullable=False),
        sa.Column('reviewed_by_principal_id',sa.BigInteger(),sa.ForeignKey('principals.id')),sa.Column('reviewed_at',sa.DateTime(timezone=True)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()))
    op.execute("""CREATE FUNCTION protect_count_observation() RETURNS trigger AS $$
      BEGIN RAISE EXCEPTION 'Count observations are immutable'; END; $$ LANGUAGE plpgsql""")
    op.execute('CREATE TRIGGER count_observation_immutable BEFORE UPDATE OR DELETE ON count_observations FOR EACH ROW EXECUTE FUNCTION protect_count_observation()')


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM count_observations) OR EXISTS (SELECT 1 FROM entries WHERE front_qty IS NOT NULL OR back_qty IS NOT NULL OR counted_qty IS NULL) OR EXISTS (SELECT 1 FROM snapshot_lines WHERE expected_on_hand IS NULL) OR EXISTS (SELECT 1 FROM store_recount_items WHERE consecutive_match_count=0 OR total_count_attempts=0 OR last_variance IS NULL)
      THEN RAISE EXCEPTION 'Refusing lossy count evidence downgrade'; END IF;
    END $$""")
    for column in ('consecutive_match_count', 'total_count_attempts'):
        op.drop_constraint(f'store_recount_items_{column}_ck', 'store_recount_items', type_='check')
        op.create_check_constraint(f'store_recount_items_{column}_ck', 'store_recount_items', f'{column} >= 1')
    for name in ('count_reviews','count_correction_attempts','count_corrections','count_observations'):
        op.drop_table(name)
    op.execute('DROP FUNCTION protect_count_observation()')
    op.drop_constraint('entries_front_back_ck','entries',type_='check')
    op.drop_column('entries','front_qty'); op.drop_column('entries','back_qty')
    op.alter_column('entries','counted_qty',existing_type=sa.Numeric(14,3),nullable=False)
    op.alter_column('snapshot_lines','expected_on_hand',existing_type=sa.Numeric(14,3),nullable=False)
    op.alter_column('store_recount_items', 'last_variance', existing_type=sa.Numeric(14,3), nullable=False)
    op.drop_column('count_sessions','draft_revision'); op.drop_column('count_sessions','observation_closed')
