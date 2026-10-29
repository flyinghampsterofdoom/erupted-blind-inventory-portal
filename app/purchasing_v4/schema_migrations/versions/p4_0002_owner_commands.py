"""Immutable owner lifecycle receipts for fresh/replayed and atomic multi-draft commands."""
from alembic import op
revision='p4_0002'
down_revision='p4_0001'
branch_labels=None
depends_on=None

def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''CREATE TABLE purchasing_v4.worksheet_owner_command(id uuid PRIMARY KEY,owner_id bigint NOT NULL REFERENCES public.principals(id),action text NOT NULL,request_digest char(64) NOT NULL,result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
 CREATE TRIGGER v4_worksheet_owner_command_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.worksheet_owner_command FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
 CREATE TRIGGER v4_worksheet_owner_command_no_truncate BEFORE TRUNCATE ON purchasing_v4.worksheet_owner_command FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();''')

def downgrade():raise RuntimeError('Retain lifecycle receipts; recover forward')
