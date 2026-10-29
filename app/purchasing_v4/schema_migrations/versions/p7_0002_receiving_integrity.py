"""Conservation, capabilities and operation transitions for the corrected split flow."""
from alembic import op
revision='p7_0002'
down_revision='p7_0001'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
CREATE OR REPLACE VIEW purchasing_v4.inventory_balance AS
 SELECT purchase_order_line_id,destination_store_id,account,sum(units)::bigint AS units FROM (
 SELECT m.purchase_order_line_id,CASE WHEN m.from_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_QUARANTINE') THEN NULL ELSE m.destination_store_id END AS destination_store_id,m.from_account AS account,-m.units AS units
 FROM purchasing_v4.inventory_movement m LEFT JOIN purchasing_v4.inventory_command c ON c.movement_id=m.id
 WHERE c.id IS NULL OR m.from_account='SUPPLIER_EXTERNAL' OR EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification v WHERE v.command_id=c.id)
 UNION ALL
 SELECT m.purchase_order_line_id,CASE WHEN m.to_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_QUARANTINE') THEN NULL ELSE m.destination_store_id END,m.to_account,m.units
 FROM purchasing_v4.inventory_movement m LEFT JOIN purchasing_v4.inventory_command c ON c.movement_id=m.id
 WHERE c.id IS NULL OR m.from_account='SUPPLIER_EXTERNAL' OR EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification v WHERE v.command_id=c.id)
 ) legs GROUP BY purchase_order_line_id,destination_store_id,account;
CREATE FUNCTION purchasing_v4.receiving_conservation_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
 DECLARE line bigint; BEGIN
 IF TG_TABLE_NAME='inventory_verification' THEN SELECT m.purchase_order_line_id INTO line FROM purchasing_v4.inventory_command c JOIN purchasing_v4.inventory_movement m ON m.id=c.movement_id WHERE c.id=NEW.command_id;
 ELSE line=NEW.purchase_order_line_id;END IF;
 IF EXISTS(SELECT 1 FROM purchasing_v4.inventory_balance WHERE purchase_order_line_id=line AND account NOT IN ('SUPPLIER_EXTERNAL','RETURNED_EXTERNAL') AND units<0) THEN RAISE EXCEPTION 'Inventory custody cannot become negative';END IF;
 IF (SELECT sum(units) FROM purchasing_v4.inventory_balance WHERE purchase_order_line_id=line)<>0 THEN RAISE EXCEPTION 'Inventory ledger must conserve every unit';END IF;
 RETURN NULL;END $$;
CREATE CONSTRAINT TRIGGER v4_custody_conservation AFTER INSERT ON purchasing_v4.inventory_movement DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_conservation_guard();
CREATE CONSTRAINT TRIGGER v4_verified_conservation AFTER INSERT ON purchasing_v4.inventory_verification DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_conservation_guard();
CREATE FUNCTION purchasing_v4.receiving_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.receiving_event e JOIN public.purchase_order_lines l ON l.purchase_order_id=e.purchase_order_id WHERE e.id=NEW.event_id AND l.id=NEW.purchase_order_line_id) THEN RAISE EXCEPTION 'Inventory movement must belong to the exact receiving PO';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_movement_po_identity BEFORE INSERT ON purchasing_v4.inventory_movement FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_identity_guard();
CREATE FUNCTION purchasing_v4.inventory_state_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='INSERT' THEN IF NEW.state<>'PENDING' OR NEW.version<>0 THEN RAISE EXCEPTION 'Inventory operation begins pending';END IF;RETURN NEW;END IF;
 IF NEW.command_id<>OLD.command_id OR NEW.version<>OLD.version+1 OR OLD.state='VERIFIED' THEN RAISE EXCEPTION 'Immutable command and successive unresolved operation state required';END IF;
 IF NEW.state='VERIFIED' AND NOT EXISTS(SELECT 1 FROM purchasing_v4.inventory_verification WHERE command_id=NEW.command_id) THEN RAISE EXCEPTION 'Exact remote adjustment evidence required';END IF;
 IF OLD.state='IN_FLIGHT' AND NEW.state='PENDING' THEN RAISE EXCEPTION 'Lost worker outcome is ambiguous, never unsent';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_inventory_operation_transition BEFORE INSERT OR UPDATE ON purchasing_v4.inventory_operation FOR EACH ROW EXECUTE FUNCTION purchasing_v4.inventory_state_guard();
DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='erupted_v4_inventory_worker') THEN CREATE ROLE erupted_v4_inventory_worker NOLOGIN; END IF; END $$;
GRANT USAGE ON SCHEMA purchasing_v4 TO erupted_v4_inventory_worker;
GRANT SELECT ON purchasing_v4.inventory_command,purchasing_v4.inventory_operation,purchasing_v4.inventory_attempt,purchasing_v4.inventory_verification,purchasing_v4.inventory_movement,purchasing_v4.inventory_balance TO erupted_v4_inventory_worker;
GRANT INSERT ON purchasing_v4.inventory_attempt,purchasing_v4.inventory_verification TO erupted_v4_inventory_worker;
GRANT UPDATE ON purchasing_v4.inventory_operation TO erupted_v4_inventory_worker;
''')


def downgrade():
 raise RuntimeError('Retain verified inventory and custody facts; recover forward')
