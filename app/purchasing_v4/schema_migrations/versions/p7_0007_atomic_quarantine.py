"""Damaged receipts enter quarantine in one atomic Square operation."""
from alembic import op
revision='p7_0007'
down_revision='p7_0006'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
CREATE OR REPLACE FUNCTION purchasing_v4.inventory_command_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE m purchasing_v4.inventory_movement; variation text; central text; location text; a jsonb; BEGIN
 SELECT * INTO m FROM purchasing_v4.inventory_movement WHERE id=NEW.movement_id;
 SELECT l.variation_id,o.snapshot->'central'->>'square_location_id' INTO variation,central FROM public.purchase_order_lines l JOIN purchasing_v4.receiving_order o ON o.purchase_order_id=l.purchase_order_id WHERE l.id=m.purchase_order_line_id;
 a=NEW.payload->'changes'->0->'adjustment';
 IF m.event_id<>NEW.event_id OR NEW.api_version<>'2026-09-16' OR NEW.payload->>'idempotency_key'<>NEW.id::text OR jsonb_array_length(NEW.payload->'changes') NOT IN (1,2) OR NEW.payload->'changes'->0->>'type'<>'ADJUSTMENT' OR a->>'reference_id'<>NEW.id::text OR a->>'catalog_object_id'<>variation OR (a->>'quantity')::numeric<>m.units THEN RAISE EXCEPTION 'Exact immutable movement and Square command identities required';END IF;
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
 IF jsonb_array_length(NEW.payload->'changes')=2 THEN
  IF m.from_account<>'SUPPLIER_EXTERNAL' OR m.to_account<>'CENTRAL_QUARANTINE' OR a->>'from_state'<>'NONE' OR a->>'to_state'<>'IN_STOCK'
   OR NEW.payload->'changes'->1->>'type'<>'ADJUSTMENT'
   OR NEW.payload->'changes'->1->'adjustment'->>'catalog_object_id' IS DISTINCT FROM variation
   OR NEW.payload->'changes'->1->'adjustment'->>'from_location_id' IS DISTINCT FROM central
   OR NEW.payload->'changes'->1->'adjustment'->>'to_location_id' IS DISTINCT FROM central
   OR NEW.payload->'changes'->1->'adjustment'->>'from_state'<>'IN_STOCK'
   OR NEW.payload->'changes'->1->'adjustment'->>'to_state'<>'WASTE'
   OR (NEW.payload->'changes'->1->'adjustment'->>'quantity')::numeric IS DISTINCT FROM m.units
   OR coalesce(NEW.payload->'changes'->1->'adjustment'->>'reference_id','') IN ('',NEW.id::text)
   THEN RAISE EXCEPTION 'Damaged receipt requires an atomic receipt-plus-quarantine inventory operation';END IF;
 END IF;
 RETURN NEW;END $$;


CREATE OR REPLACE FUNCTION purchasing_v4.inventory_proof_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE p purchasing_v4.inventory_command;expected jsonb;actual jsonb;actuals jsonb;i integer;n integer; BEGIN
 SELECT * INTO p FROM purchasing_v4.inventory_command WHERE id=NEW.command_id;n=jsonb_array_length(p.payload->'changes');
 actuals=CASE WHEN n=1 THEN jsonb_build_array(NEW.evidence) ELSE NEW.evidence->'changes' END;
 IF actuals IS NULL OR jsonb_typeof(actuals)<>'array' OR jsonb_array_length(actuals)<>n THEN RAISE EXCEPTION 'Every atomic inventory change requires exact remote proof';END IF;
 IF actuals->0->'adjustment'->>'id' IS DISTINCT FROM NEW.remote_adjustment_id THEN RAISE EXCEPTION 'Retain the original remote adjustment identity';END IF;
 FOR i IN 0..n-1 LOOP
  expected=p.payload->'changes'->i->'adjustment';actual=actuals->i->'adjustment';
  IF actuals->i->>'type' IS DISTINCT FROM 'ADJUSTMENT' OR coalesce(actual->>'id','')='' OR actual->>'reference_id' IS DISTINCT FROM expected->>'reference_id'
   OR actual->>'catalog_object_id' IS DISTINCT FROM expected->>'catalog_object_id'
   OR actual->>'from_location_id' IS DISTINCT FROM expected->>'from_location_id' OR actual->>'to_location_id' IS DISTINCT FROM expected->>'to_location_id'
   OR actual->>'from_state' IS DISTINCT FROM expected->>'from_state' OR actual->>'to_state' IS DISTINCT FROM expected->>'to_state'
   OR (actual->>'quantity')::numeric IS DISTINCT FROM (expected->>'quantity')::numeric
   OR (actual->>'occurred_at')::timestamptz IS DISTINCT FROM (expected->>'occurred_at')::timestamptz
   THEN RAISE EXCEPTION 'Exact remote adjustment evidence required for each atomic inventory change';END IF;
 END LOOP;
 IF n=2 AND actuals->0->'adjustment'->>'id'=actuals->1->'adjustment'->>'id' THEN RAISE EXCEPTION 'Atomic changes must retain distinct original remote identities';END IF;
 IF p.predecessor_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification WHERE command_id=p.predecessor_id) THEN RAISE EXCEPTION 'Prior inventory operation remains unresolved';END IF;
 RETURN NEW;END $$;
''')

def downgrade():raise RuntimeError('Retain original atomic inventory payloads and remote proofs; recover forward')
