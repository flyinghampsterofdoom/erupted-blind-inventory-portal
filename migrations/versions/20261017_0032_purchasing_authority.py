"""Add Purchasing V3 Phase 1 evidence and actual-card authority, without backfill."""
from alembic import op
import sqlalchemy as sa

revision = '20261017_0032'
down_revision = '20261016_0037'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('purchasing_evidence_runs',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('source', sa.String(16), nullable=False), sa.Column('status', sa.String(16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scope', sa.JSON(), nullable=False), sa.Column('evidence', sa.JSON(), nullable=False),
        sa.CheckConstraint("source IN ('INVENTORY', 'SALES', 'CATALOG')", name='purchasing_evidence_source_ck'),
        sa.CheckConstraint("status IN ('COMPLETE', 'PARTIAL', 'FAILED')", name='purchasing_evidence_status_ck'),
        sa.CheckConstraint('completed_at >= started_at', name='purchasing_evidence_time_ck'))
    op.create_index('idx_purchasing_evidence_source_completed', 'purchasing_evidence_runs', ['source', 'completed_at'])
    op.create_table('purchasing_product_authority',
        sa.Column('square_variation_id', sa.Text(), primary_key=True),
        sa.Column('square_vendor_ids', sa.JSON(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source_complete', sa.Boolean(), nullable=False),
        sa.Column('cash_payment_method_id', sa.BigInteger(), sa.ForeignKey('payment_methods.id')))
    op.create_table('funding_balance_checkpoints',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_id', sa.BigInteger(), sa.ForeignKey('funding_accounts.id'), nullable=False),
        sa.Column('balance', sa.Numeric(14, 2), nullable=False),
        sa.Column('previous_balance', sa.Numeric(14, 2)),
        sa.Column('covered_sequence', sa.BigInteger(), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('account_id', 'idempotency_key', name='funding_checkpoint_request_uniq'),
        sa.CheckConstraint('covered_sequence >= 0', name='funding_checkpoint_sequence_ck'))
    op.create_table('funding_actual_activity',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('account_id', sa.BigInteger(), sa.ForeignKey('funding_accounts.id'), nullable=False),
        sa.Column('sequence', sa.BigInteger(), nullable=False), sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('signed_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('payment_id', sa.BigInteger(), sa.ForeignKey('funding_payments.id')),
        sa.Column('ledger_entry_id', sa.BigInteger(), sa.ForeignKey('funding_ledger_entries.id'), unique=True),
        sa.Column('original_activity_id', sa.BigInteger(), sa.ForeignKey('funding_actual_activity.id'), unique=True),
        sa.Column('created_by_principal_id', sa.BigInteger(), sa.ForeignKey('principals.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('account_id', 'sequence', name='funding_actual_sequence_uniq'),
        sa.UniqueConstraint('account_id', 'idempotency_key', name='funding_actual_request_uniq'),
        sa.UniqueConstraint('payment_id', name='funding_actual_payment_uniq'),
        sa.CheckConstraint("kind IN ('CHARGE', 'PAYMENT', 'CREDIT', 'FEE', 'INTEREST', 'REVERSAL')", name='funding_actual_kind_ck'),
        sa.CheckConstraint('sequence > 0', name='funding_actual_positive_sequence_ck'))


def downgrade():
    bind = op.get_bind()
    for name in ('funding_actual_activity', 'funding_balance_checkpoints', 'purchasing_product_authority', 'purchasing_evidence_runs'):
        if bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM {name} LIMIT 1)')).scalar():
            raise RuntimeError('Refusing to discard Purchasing V3 evidence; retain the additive schema.')
    for name in ('funding_actual_activity', 'funding_balance_checkpoints', 'purchasing_product_authority', 'purchasing_evidence_runs'):
        op.drop_table(name)
