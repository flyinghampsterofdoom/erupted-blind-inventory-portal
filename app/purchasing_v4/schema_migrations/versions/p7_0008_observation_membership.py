"""Inventory observation membership uses pinned locations and remote evidence."""
from alembic import op
revision='p7_0008'
down_revision='p7_0007'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''CREATE OR REPLACE VIEW purchasing_v4.receiving_posting_membership AS
 SELECT l.variation_id,(s->>'id')::bigint AS store_id,x.id::text AS command_id,o.state,o.updated_at,
 s->>'square_location_id' AS square_location_id,v.evidence AS remote_proof
 FROM purchasing_v4.inventory_command x JOIN purchasing_v4.inventory_operation o ON o.command_id=x.id
 JOIN purchasing_v4.inventory_movement m ON m.id=x.movement_id JOIN public.purchase_order_lines l ON l.id=m.purchase_order_line_id
 JOIN purchasing_v4.receiving_order ro ON ro.purchase_order_id=l.purchase_order_id
 CROSS JOIN LATERAL jsonb_array_elements(ro.snapshot->'stores') s
 LEFT JOIN purchasing_v4.inventory_verification v ON v.command_id=x.id
 WHERE s->>'square_location_id' IN (x.payload->'changes'->0->'adjustment'->>'from_location_id',x.payload->'changes'->0->'adjustment'->>'to_location_id');''')


def downgrade():raise RuntimeError('Retain original posting identities and observation provenance; recover forward')
