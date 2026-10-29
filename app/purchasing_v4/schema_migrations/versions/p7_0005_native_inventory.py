"""Pinned 2026-09-16 native cross-location adjustments; single-screen split."""
from alembic import op
revision='p7_0005'
down_revision='p7_0004'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
ALTER TABLE purchasing_v4.inventory_movement DROP CONSTRAINT inventory_movement_from_account_check;
ALTER TABLE purchasing_v4.inventory_movement DROP CONSTRAINT inventory_movement_to_account_check;
ALTER TABLE purchasing_v4.inventory_movement ADD CHECK(from_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_RESERVED','CENTRAL_AVAILABLE','CENTRAL_DAMAGE_PENDING','CENTRAL_QUARANTINE','TRANSIT','DESTINATION_AVAILABLE','DESTINATION_QUARANTINE','DISCREPANCY','RETURNED_EXTERNAL','CORRECTED_EXTERNAL'));
ALTER TABLE purchasing_v4.inventory_movement ADD CHECK(to_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_RESERVED','CENTRAL_AVAILABLE','CENTRAL_DAMAGE_PENDING','CENTRAL_QUARANTINE','TRANSIT','DESTINATION_AVAILABLE','DESTINATION_QUARANTINE','DISCREPANCY','RETURNED_EXTERNAL','CORRECTED_EXTERNAL'));
CREATE OR REPLACE VIEW purchasing_v4.inventory_balance AS
 SELECT purchase_order_line_id,destination_store_id,account,sum(units)::bigint AS units FROM (
 SELECT m.purchase_order_line_id,CASE WHEN m.from_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_DAMAGE_PENDING','CENTRAL_QUARANTINE') THEN NULL ELSE m.destination_store_id END AS destination_store_id,m.from_account AS account,-m.units AS units
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
 IF m.to_account='DESTINATION_AVAILABLE' OR m.from_account='DESTINATION_AVAILABLE' THEN
 SELECT s->>'square_location_id' INTO location FROM purchasing_v4.receiving_order o,jsonb_array_elements(o.snapshot->'stores')s WHERE o.purchase_order_id=(SELECT purchase_order_id FROM public.purchase_order_lines WHERE id=m.purchase_order_line_id) AND (s->>'id')::bigint=m.destination_store_id;
 ELSE location=central;END IF;
 IF m.from_account='CENTRAL_RESERVED' AND m.to_account='DESTINATION_AVAILABLE' THEN
  IF a->>'from_location_id' IS DISTINCT FROM central OR a->>'to_location_id' IS DISTINCT FROM location OR a->>'from_state'<>'IN_STOCK' OR a->>'to_state'<>'IN_STOCK' THEN RAISE EXCEPTION 'Native transfer must preserve exact central and destination inventory identities';END IF;
 ELSE IF a->>'from_location_id' IS DISTINCT FROM location OR a->>'to_location_id' IS DISTINCT FROM location THEN RAISE EXCEPTION 'Local quantity adjustment requires the pinned actual location';END IF; END IF;
 IF NEW.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_command p JOIN purchasing_v4.inventory_movement x ON x.id=p.movement_id WHERE p.id=NEW.predecessor_id AND x.purchase_order_line_id=m.purchase_order_line_id) THEN RAISE EXCEPTION 'Posting dependency must preserve the exact product custody chain';END IF;
 RETURN NEW;END $$;

CREATE OR REPLACE FUNCTION purchasing_v4.inventory_proof_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE p purchasing_v4.inventory_command; expected jsonb; actual jsonb; BEGIN
 SELECT * INTO p FROM purchasing_v4.inventory_command WHERE id=NEW.command_id;expected=p.payload->'changes'->0->'adjustment';actual=NEW.evidence->'adjustment';
 IF NEW.evidence->>'type'<>'ADJUSTMENT' OR actual->>'id' IS DISTINCT FROM NEW.remote_adjustment_id OR actual->>'reference_id' IS DISTINCT FROM p.id::text OR actual->>'catalog_object_id' IS DISTINCT FROM expected->>'catalog_object_id' OR actual->>'from_location_id' IS DISTINCT FROM expected->>'from_location_id' OR actual->>'to_location_id' IS DISTINCT FROM expected->>'to_location_id' OR actual->>'from_state' IS DISTINCT FROM expected->>'from_state' OR actual->>'to_state' IS DISTINCT FROM expected->>'to_state' OR (actual->>'quantity')::numeric IS DISTINCT FROM (expected->>'quantity')::numeric OR (actual->>'occurred_at')::timestamptz IS DISTINCT FROM (expected->>'occurred_at')::timestamptz THEN RAISE EXCEPTION 'Inventory outcome requires an exact independently identifiable remote adjustment';END IF;
 IF p.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification WHERE command_id=p.predecessor_id) THEN RAISE EXCEPTION 'Prior inventory leg remains unverified';END IF;
 RETURN NEW;END $$;

CREATE OR REPLACE VIEW purchasing_v4.receiving_posting_membership AS
 SELECT l.variation_id,s.id AS store_id,x.id::text AS command_id,o.state,o.updated_at,
 s.square_location_id AS square_location_id
 FROM purchasing_v4.inventory_command x JOIN purchasing_v4.inventory_operation o ON o.command_id=x.id
 JOIN purchasing_v4.inventory_movement m ON m.id=x.movement_id JOIN public.purchase_order_lines l ON l.id=m.purchase_order_line_id
 JOIN public.stores s ON (s.square_location_id=x.payload->'changes'->0->'adjustment'->>'from_location_id' OR s.square_location_id=x.payload->'changes'->0->'adjustment'->>'to_location_id');

CREATE INDEX v4_inventory_movement_event ON purchasing_v4.inventory_movement(event_id);
CREATE INDEX v4_inventory_command_event ON purchasing_v4.inventory_command(event_id);
''')

def downgrade():raise RuntimeError('Retain original inventory command identities; recover forward')
