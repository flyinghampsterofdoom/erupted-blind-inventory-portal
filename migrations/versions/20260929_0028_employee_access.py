"""Employee password setup, bounded throttling and encrypted integration settings."""
from alembic import op
import sqlalchemy as sa
revision = '20260929_0028'
down_revision = '20260927_0027'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('principals', 'password_hash', existing_type=sa.Text(), nullable=True)
    op.add_column('principals', sa.Column('recovery_email_confirmed', sa.Boolean(), nullable=False, server_default='false'))
    op.create_table('application_settings', sa.Column('key', sa.String(100), primary_key=True),
        sa.Column('values', sa.JSON(), nullable=False), sa.Column('encrypted_secrets', sa.Text()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_table('password_reset_tokens', sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('principal_id', sa.BigInteger(), sa.ForeignKey('principals.id', ondelete='CASCADE'), nullable=False),
        sa.Column('employee_id', sa.BigInteger(), sa.ForeignKey('employees.id', ondelete='CASCADE'), nullable=False),
        sa.Column('login_email', sa.Text(), nullable=False), sa.Column('digest', sa.String(64), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True)), sa.Column('revoked_at', sa.DateTime(timezone=True)))
    op.create_index('ix_password_reset_tokens_principal_id', 'password_reset_tokens', ['principal_id'])
    op.create_table('auth_throttles', sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('window_start', sa.DateTime(timezone=True), nullable=False), sa.Column('attempts', sa.Integer(), nullable=False))
    op.create_index('ix_auth_throttles_window_start', 'auth_throttles', ['window_start'])


def downgrade():
    # Refuse to discard pending accounts or invent passwords during rollback.
    if op.get_bind().execute(sa.text('SELECT 1 FROM principals WHERE password_hash IS NULL LIMIT 1')).first():
        raise RuntimeError('Pending passwordless accounts must be resolved before downgrade.')
    op.drop_table('auth_throttles')
    op.drop_table('password_reset_tokens')
    op.drop_table('application_settings')
    op.drop_column('principals', 'recovery_email_confirmed')
    op.alter_column('principals', 'password_hash', existing_type=sa.Text(), nullable=False)
