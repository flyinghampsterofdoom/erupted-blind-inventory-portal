"""One read-only V4 supply projection; original public PO facts stay immutable."""
from alembic import op
revision='p7_0004'
down_revision='p7_0003'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
CREATE VIEW purchasing_v4.receiving_supply AS
 WITH decisions AS (
 SELECT e.kind,(j->>'line_id')::bigint AS line_id,k.key::bigint AS store_id,sum(k.value::bigint)::bigint AS units
 FROM purchasing_v4.receiving_event e CROSS JOIN LATERAL jsonb_array_elements(e.payload->'lines')j
 CROSS JOIN LATERAL jsonb_each_text(j->'stores')k WHERE e.kind IN ('CLOSE','OVERAGE')
 GROUP BY e.kind,(j->>'line_id')::bigint,k.key::bigint
 ), posted AS (
 SELECT purchase_order_line_id,destination_store_id,sum(units)::bigint AS units FROM purchasing_v4.inventory_balance
 WHERE account IN ('CENTRAL_AVAILABLE','DESTINATION_AVAILABLE') GROUP BY purchase_order_line_id,destination_store_id
 ), held AS (
 SELECT purchase_order_line_id,bool_or(account IN ('CENTRAL_UNASSIGNED','DISCREPANCY') AND units>0) AS unresolved
 FROM purchasing_v4.inventory_balance GROUP BY purchase_order_line_id
 ), pending AS (
 SELECT m.purchase_order_line_id,bool_or(o.state<>'VERIFIED') AS unresolved,max(o.updated_at) AS updated_at
 FROM purchasing_v4.inventory_command x JOIN purchasing_v4.inventory_movement m ON m.id=x.movement_id
 JOIN purchasing_v4.inventory_operation o ON o.command_id=x.id GROUP BY m.purchase_order_line_id
 ), amounts AS (
 SELECT l.id,l.purchase_order_id,l.variation_id,l.sku,a.store_id,
 (a.allocated_qty+coalesce(extra.units,0))::bigint AS allocated_qty,
 CASE WHEN coalesce(pending.unresolved,false) OR coalesce(held.unresolved,false) THEN NULL ELSE coalesce(posted.units,0) END::bigint AS store_received_qty,
 coalesce(closed.units,0)::bigint AS closed,pending.updated_at AS posting_updated_at
 FROM purchasing_v4.receiving_order ro JOIN public.purchase_order_lines l ON l.purchase_order_id=ro.purchase_order_id
 JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id
 LEFT JOIN decisions extra ON extra.kind='OVERAGE' AND extra.line_id=l.id AND extra.store_id=a.store_id
 LEFT JOIN decisions closed ON closed.kind='CLOSE' AND closed.line_id=l.id AND closed.store_id=a.store_id
 LEFT JOIN posted ON posted.purchase_order_line_id=l.id AND posted.destination_store_id=a.store_id
 LEFT JOIN held ON held.purchase_order_line_id=l.id LEFT JOIN pending ON pending.purchase_order_line_id=l.id
 ) SELECT id,purchase_order_id,variation_id,sku,sum(allocated_qty) OVER(PARTITION BY id)::bigint AS ordered_qty,
 CASE WHEN count(store_received_qty) OVER(PARTITION BY id)=count(*) OVER(PARTITION BY id) THEN sum(store_received_qty) OVER(PARTITION BY id)::bigint ELSE NULL END AS received_qty_total,
 store_id,allocated_qty,store_received_qty,closed,posting_updated_at FROM amounts;
CREATE VIEW purchasing_v4.receiving_posting_membership AS
 SELECT l.variation_id,s.id AS store_id,x.id::text AS command_id,o.state,o.updated_at,
 x.payload->'changes'->0->'adjustment'->>'location_id' AS square_location_id
 FROM purchasing_v4.inventory_command x JOIN purchasing_v4.inventory_operation o ON o.command_id=x.id
 JOIN purchasing_v4.inventory_movement m ON m.id=x.movement_id JOIN public.purchase_order_lines l ON l.id=m.purchase_order_line_id
 JOIN public.stores s ON s.square_location_id=x.payload->'changes'->0->'adjustment'->>'location_id';
GRANT SELECT ON purchasing_v4.receiving_supply,purchasing_v4.receiving_posting_membership TO erupted_v4_po_writer;
''')


def downgrade():raise RuntimeError('Retain authoritative receiving stages and inventory proof; recover forward')
