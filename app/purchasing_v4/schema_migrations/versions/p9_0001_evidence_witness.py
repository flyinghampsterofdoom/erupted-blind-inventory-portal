"""Retain observed inventory source rows without rewriting the legacy projection."""
from alembic import op
revision='p9_0001'
down_revision='p8_0007'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE purchasing_v4.inventory_witness (
      id uuid PRIMARY KEY,source_namespace text NOT NULL,source_digest char(64) NOT NULL,
      square_variation_id text NOT NULL,store_id bigint NOT NULL,observed_at timestamptz NOT NULL,
      source_calculated_at timestamptz,payload jsonb NOT NULL,captured_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE(source_namespace,source_digest));
    CREATE INDEX v4_inventory_witness_scope ON purchasing_v4.inventory_witness(source_namespace,square_variation_id,store_id,observed_at DESC);
    CREATE TRIGGER v4_witness_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.inventory_witness FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
    CREATE TRIGGER v4_witness_no_truncate BEFORE TRUNCATE ON purchasing_v4.inventory_witness FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();''')

def downgrade():
    raise RuntimeError('Retained source evidence cannot be erased by rollback')
