"""Receiving capability and exact remote-adjustment proof enforcement."""
from alembic import op
revision='p7_0003'
down_revision='p7_0002'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
CREATE FUNCTION purchasing_v4.receiving_authority_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE actor bigint; central bigint; needed text; isowner boolean; BEGIN
 IF TG_TABLE_NAME='receiving_grant' THEN actor=NEW.owner_id; ELSE actor=NEW.actor_id; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.principals p WHERE p.id=actor AND p.active AND NOT EXISTS(SELECT 1 FROM public.employees e WHERE e.principal_id=p.id AND NOT e.active)) THEN RAISE EXCEPTION 'Active receiving principal required';END IF;
 SELECT EXISTS(SELECT 1 FROM purchasing_v4.commitment_owner o JOIN public.principals p ON p.id=o.principal_id WHERE o.principal_id=actor AND p.role::text='ADMIN') INTO isowner;
 IF isowner THEN RETURN NEW;END IF;
 IF TG_TABLE_NAME='receiving_grant' THEN RAISE EXCEPTION 'Only the configured owner grants receiving capabilities';END IF;
 IF NEW.kind NOT IN ('RECEIPT','DISCREPANCY') THEN RAISE EXCEPTION 'Owner-only receiving exception or allocation authority';END IF;
 needed=CASE WHEN NEW.kind='RECEIPT' THEN 'CONFIRM' ELSE 'DISCREPANCY' END;
 SELECT central_store_id INTO central FROM purchasing_v4.receiving_order WHERE purchase_order_id=NEW.purchase_order_id;
 IF NOT coalesce((SELECT enabled AND (store_id IS NULL OR store_id=central) FROM purchasing_v4.receiving_grant WHERE principal_id=actor AND capability=needed ORDER BY created_at DESC,command_id DESC LIMIT 1),false) THEN RAISE EXCEPTION 'Explicit receiving capability required';END IF;
 IF NEW.kind='RECEIPT' AND EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.payload->'lines') l WHERE coalesce((l->>'owner_split_override')::boolean,false)) THEN RAISE EXCEPTION 'Only the configured owner changes shortage allocations';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_receiving_event_authority BEFORE INSERT ON purchasing_v4.receiving_event FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_authority_guard();
CREATE TRIGGER v4_receiving_grant_authority BEFORE INSERT ON purchasing_v4.receiving_grant FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_authority_guard();
CREATE FUNCTION purchasing_v4.inventory_command_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE m purchasing_v4.inventory_movement; variation text; central text; location text; a jsonb; BEGIN
 SELECT * INTO m FROM purchasing_v4.inventory_movement WHERE id=NEW.movement_id;
 SELECT l.variation_id,o.snapshot->'central'->>'square_location_id' INTO variation,central FROM public.purchase_order_lines l JOIN purchasing_v4.receiving_order o ON o.purchase_order_id=l.purchase_order_id WHERE l.id=m.purchase_order_line_id;
 a=NEW.payload->'changes'->0->'adjustment';
 IF m.event_id<>NEW.event_id OR NEW.api_version<>'2026-09-16' OR NEW.payload->>'idempotency_key'<>NEW.id::text OR jsonb_array_length(NEW.payload->'changes')<>1 OR NEW.payload->'changes'->0->>'type'<>'ADJUSTMENT' OR a->>'reference_id'<>NEW.id::text OR a->>'catalog_object_id'<>variation OR (a->>'quantity')::numeric<>m.units THEN RAISE EXCEPTION 'Exact immutable movement and Square command identities required';END IF;
 IF m.to_account='DESTINATION_AVAILABLE' OR m.from_account='DESTINATION_AVAILABLE' THEN
 SELECT s->>'square_location_id' INTO location FROM purchasing_v4.receiving_order o,jsonb_array_elements(o.snapshot->'stores')s WHERE o.purchase_order_id=(SELECT purchase_order_id FROM public.purchase_order_lines WHERE id=m.purchase_order_line_id) AND (s->>'id')::bigint=m.destination_store_id;
 ELSE location=central;END IF;
 IF a->>'location_id' IS DISTINCT FROM location THEN RAISE EXCEPTION 'Inventory location must match the immutable actual store snapshot';END IF;
 IF NEW.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_command p JOIN purchasing_v4.inventory_movement x ON x.id=p.movement_id WHERE p.id=NEW.predecessor_id AND x.purchase_order_line_id=m.purchase_order_line_id) THEN RAISE EXCEPTION 'Posting dependency must preserve the exact product custody chain';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_inventory_command_identity BEFORE INSERT ON purchasing_v4.inventory_command FOR EACH ROW EXECUTE FUNCTION purchasing_v4.inventory_command_guard();
CREATE FUNCTION purchasing_v4.inventory_proof_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE p purchasing_v4.inventory_command; expected jsonb; actual jsonb; BEGIN
 SELECT * INTO p FROM purchasing_v4.inventory_command WHERE id=NEW.command_id;expected=p.payload->'changes'->0->'adjustment';actual=NEW.evidence->'adjustment';
 IF NEW.evidence->>'type'<>'ADJUSTMENT' OR actual->>'id' IS DISTINCT FROM NEW.remote_adjustment_id OR actual->>'reference_id' IS DISTINCT FROM p.id::text OR actual->>'catalog_object_id' IS DISTINCT FROM expected->>'catalog_object_id' OR actual->>'location_id' IS DISTINCT FROM expected->>'location_id' OR actual->>'from_state' IS DISTINCT FROM expected->>'from_state' OR actual->>'to_state' IS DISTINCT FROM expected->>'to_state' OR (actual->>'quantity')::numeric IS DISTINCT FROM (expected->>'quantity')::numeric OR (actual->>'occurred_at')::timestamptz IS DISTINCT FROM (expected->>'occurred_at')::timestamptz THEN RAISE EXCEPTION 'Inventory outcome requires an exact independently identifiable remote adjustment';END IF;
 IF p.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification WHERE command_id=p.predecessor_id) THEN RAISE EXCEPTION 'Prior inventory leg remains unverified';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_inventory_outcome_proof BEFORE INSERT ON purchasing_v4.inventory_verification FOR EACH ROW EXECUTE FUNCTION purchasing_v4.inventory_proof_guard();
''')


def downgrade():
 raise RuntimeError('Retain custody and verified inventory proofs; recover forward')
