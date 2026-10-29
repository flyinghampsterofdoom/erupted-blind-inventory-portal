"""Owner-reviewed store misallocation corrections preserve original PO facts."""
from alembic import op
revision='p7_0006'
down_revision='p7_0005'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
ALTER TABLE purchasing_v4.inventory_movement ADD COLUMN source_store_id bigint REFERENCES public.stores(id);
ALTER TABLE purchasing_v4.inventory_movement DROP CONSTRAINT inventory_movement_check;
ALTER TABLE purchasing_v4.inventory_movement ADD CHECK(from_account<>to_account OR (source_store_id IS NOT NULL AND source_store_id IS DISTINCT FROM destination_store_id));
CREATE OR REPLACE VIEW purchasing_v4.inventory_balance AS
 SELECT purchase_order_line_id,destination_store_id,account,sum(units)::bigint AS units FROM (
 SELECT m.purchase_order_line_id,CASE WHEN m.from_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_DAMAGE_PENDING','CENTRAL_QUARANTINE') THEN NULL ELSE coalesce(m.source_store_id,m.destination_store_id) END AS destination_store_id,m.from_account AS account,-m.units AS units
 FROM purchasing_v4.inventory_movement m LEFT JOIN purchasing_v4.inventory_command c ON c.movement_id=m.id
 WHERE c.id IS NULL OR m.from_account='SUPPLIER_EXTERNAL' OR EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification v WHERE v.command_id=c.id)
 UNION ALL
 SELECT m.purchase_order_line_id,CASE WHEN m.to_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_DAMAGE_PENDING','CENTRAL_QUARANTINE') THEN NULL ELSE m.destination_store_id END,m.to_account,m.units
 FROM purchasing_v4.inventory_movement m LEFT JOIN purchasing_v4.inventory_command c ON c.movement_id=m.id
 WHERE c.id IS NULL OR m.from_account='SUPPLIER_EXTERNAL' OR EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification v WHERE v.command_id=c.id)
 ) legs GROUP BY purchase_order_line_id,destination_store_id,account;
CREATE OR REPLACE FUNCTION purchasing_v4.inventory_command_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE m purchasing_v4.inventory_movement; variation text; central text; location text; a jsonb; BEGIN
 SELECT * INTO m FROM purchasing_v4.inventory_movement WHERE id=NEW.movement_id;
 SELECT l.variation_id,o.snapshot->'central'->>'square_location_id' INTO variation,central FROM public.purchase_order_lines l JOIN purchasing_v4.receiving_order o ON o.purchase_order_id=l.purchase_order_id WHERE l.id=m.purchase_order_line_id;
 a=NEW.payload->'changes'->0->'adjustment';
 IF m.event_id<>NEW.event_id OR NEW.api_version<>'2026-09-16' OR NEW.payload->>'idempotency_key'<>NEW.id::text OR jsonb_array_length(NEW.payload->'changes')<>1 OR NEW.payload->'changes'->0->>'type'<>'ADJUSTMENT' OR a->>'reference_id'<>NEW.id::text OR a->>'catalog_object_id'<>variation OR (a->>'quantity')::numeric<>m.units THEN RAISE EXCEPTION 'Exact immutable movement and Square command identities required';END IF;
 IF m.to_account='DESTINATION_AVAILABLE' OR m.from_account='DESTINATION_AVAILABLE' OR m.source_store_id IS NOT NULL THEN
 SELECT s->>'square_location_id' INTO location FROM purchasing_v4.receiving_order o,jsonb_array_elements(o.snapshot->'stores')s WHERE o.purchase_order_id=(SELECT purchase_order_id FROM public.purchase_order_lines WHERE id=m.purchase_order_line_id) AND (s->>'id')::bigint=m.destination_store_id;
 ELSE location=central;END IF;
 IF m.source_store_id IS NOT NULL AND m.from_account IN ('CENTRAL_AVAILABLE','DESTINATION_AVAILABLE') AND m.to_account IN ('CENTRAL_AVAILABLE','DESTINATION_AVAILABLE') THEN
  SELECT s->>'square_location_id' INTO central FROM purchasing_v4.receiving_order o,jsonb_array_elements(o.snapshot->'stores')s WHERE o.purchase_order_id=(SELECT purchase_order_id FROM public.purchase_order_lines WHERE id=m.purchase_order_line_id) AND (s->>'id')::bigint=m.source_store_id;
  IF a->>'from_location_id' IS DISTINCT FROM central OR a->>'to_location_id' IS DISTINCT FROM location OR a->>'from_state'<>'IN_STOCK' OR a->>'to_state'<>'IN_STOCK' THEN RAISE EXCEPTION 'Corrective transfer must match both reviewed actual stores';END IF;
 ELSIF m.from_account='CENTRAL_RESERVED' AND m.to_account='DESTINATION_AVAILABLE' THEN
  IF a->>'from_location_id' IS DISTINCT FROM central OR a->>'to_location_id' IS DISTINCT FROM location OR a->>'from_state'<>'IN_STOCK' OR a->>'to_state'<>'IN_STOCK' THEN RAISE EXCEPTION 'Native transfer must preserve exact central and destination inventory identities';END IF;
 ELSE IF a->>'from_location_id' IS DISTINCT FROM location OR a->>'to_location_id' IS DISTINCT FROM location THEN RAISE EXCEPTION 'Local quantity adjustment requires the pinned actual location';END IF; END IF;
 IF NEW.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_command p JOIN purchasing_v4.inventory_movement x ON x.id=p.movement_id WHERE p.id=NEW.predecessor_id AND x.purchase_order_line_id=m.purchase_order_line_id) THEN RAISE EXCEPTION 'Posting dependency must preserve the exact product custody chain';END IF;
 RETURN NEW;END $$;

CREATE OR REPLACE VIEW purchasing_v4.receiving_supply AS
 WITH decisions AS (
 SELECT e.kind,(j->>'line_id')::bigint AS line_id,k.key::bigint AS store_id,sum(k.value::bigint)::bigint AS units
 FROM purchasing_v4.receiving_event e CROSS JOIN LATERAL jsonb_array_elements(e.payload->'lines')j
 CROSS JOIN LATERAL jsonb_each_text(CASE WHEN e.kind='CORRECTION' THEN j->'approved_split_delta' ELSE j->'stores' END)k WHERE e.kind IN ('CLOSE','OVERAGE','CORRECTION')
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
 (a.allocated_qty+coalesce(extra.units,0)+coalesce(amended.units,0))::bigint AS allocated_qty,
 CASE WHEN coalesce(pending.unresolved,false) OR coalesce(held.unresolved,false) THEN NULL ELSE coalesce(posted.units,0) END::bigint AS store_received_qty,
 coalesce(closed.units,0)::bigint AS closed,pending.updated_at AS posting_updated_at
 FROM purchasing_v4.receiving_order ro JOIN public.purchase_order_lines l ON l.purchase_order_id=ro.purchase_order_id
 JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id
 LEFT JOIN decisions extra ON extra.kind='OVERAGE' AND extra.line_id=l.id AND extra.store_id=a.store_id
 LEFT JOIN decisions amended ON amended.kind='CORRECTION' AND amended.line_id=l.id AND amended.store_id=a.store_id
 LEFT JOIN decisions closed ON closed.kind='CLOSE' AND closed.line_id=l.id AND closed.store_id=a.store_id
 LEFT JOIN posted ON posted.purchase_order_line_id=l.id AND posted.destination_store_id=a.store_id
 LEFT JOIN held ON held.purchase_order_line_id=l.id LEFT JOIN pending ON pending.purchase_order_line_id=l.id
 ) SELECT id,purchase_order_id,variation_id,sku,sum(allocated_qty) OVER(PARTITION BY id)::bigint AS ordered_qty,
 CASE WHEN count(store_received_qty) OVER(PARTITION BY id)=count(*) OVER(PARTITION BY id) THEN sum(store_received_qty) OVER(PARTITION BY id)::bigint ELSE NULL END AS received_qty_total,
 store_id,allocated_qty,store_received_qty,closed,posting_updated_at FROM amounts;
''')

def downgrade():raise RuntimeError('Retain original purchasing and corrective movement facts; recover forward')
