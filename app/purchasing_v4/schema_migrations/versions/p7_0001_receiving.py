"""Additive isolated physical custody, inventory intent and reconciliation facts."""
from alembic import op
revision='p7_0001'
down_revision='p6_0001'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
CREATE TABLE purchasing_v4.receiving_order(
 purchase_order_id bigint PRIMARY KEY REFERENCES public.purchase_orders(id),
 vendor_authorization_id uuid NOT NULL UNIQUE REFERENCES purchasing_v4.authorized_vendor(id),
 central_store_id bigint NOT NULL REFERENCES public.stores(id),snapshot jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.receiving_event(
 id uuid PRIMARY KEY,purchase_order_id bigint NOT NULL REFERENCES purchasing_v4.receiving_order(purchase_order_id),
 version bigint NOT NULL CHECK(version>0),actor_id bigint NOT NULL REFERENCES public.principals(id),
 kind text NOT NULL CHECK(kind IN ('RECEIPT','ALLOCATE','PREPARE','HANDOFF','ARRIVAL','DISCREPANCY','CORRECTION','CLOSE','OVERAGE','RESOLVE')),
 payload jsonb NOT NULL,payload_digest char(64) NOT NULL,result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(purchase_order_id,version)
);
CREATE INDEX v4_receiving_event_history ON purchasing_v4.receiving_event(purchase_order_id,created_at);
CREATE TABLE purchasing_v4.receiving_head(
 purchase_order_id bigint PRIMARY KEY REFERENCES purchasing_v4.receiving_order(purchase_order_id),
 version bigint NOT NULL DEFAULT 0 CHECK(version>=0),event_id uuid REFERENCES purchasing_v4.receiving_event(id),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.receiving_draft(
 purchase_order_id bigint NOT NULL REFERENCES purchasing_v4.receiving_order(purchase_order_id),
 actor_id bigint NOT NULL REFERENCES public.principals(id),version bigint NOT NULL DEFAULT 0 CHECK(version>=0),
 base_version bigint NOT NULL CHECK(base_version>=0),payload jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(purchase_order_id,actor_id)
);
CREATE TABLE purchasing_v4.receiving_grant(
 command_id uuid PRIMARY KEY,principal_id bigint NOT NULL REFERENCES public.principals(id),
 capability text NOT NULL CHECK(capability IN ('VIEW','ENTER','CONFIRM','PREPARE','HANDOFF','ARRIVAL','DISCREPANCY')),
 store_id bigint REFERENCES public.stores(id),enabled boolean NOT NULL,owner_id bigint NOT NULL REFERENCES public.principals(id),
 reason text NOT NULL CHECK(length(trim(reason))>0),created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_receiving_capability ON purchasing_v4.receiving_grant(principal_id,capability,created_at DESC);
CREATE TABLE purchasing_v4.inventory_movement(
 id uuid PRIMARY KEY,event_id uuid NOT NULL REFERENCES purchasing_v4.receiving_event(id),
 purchase_order_line_id bigint NOT NULL REFERENCES public.purchase_order_lines(id),
 destination_store_id bigint REFERENCES public.stores(id),from_account text NOT NULL,to_account text NOT NULL,
 units bigint NOT NULL CHECK(units>0),CHECK(from_account<>to_account),
 CHECK(from_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_RESERVED','CENTRAL_AVAILABLE','CENTRAL_QUARANTINE','TRANSIT','DESTINATION_AVAILABLE','DESTINATION_QUARANTINE','DISCREPANCY','RETURNED_EXTERNAL')),
 CHECK(to_account IN ('SUPPLIER_EXTERNAL','CENTRAL_UNASSIGNED','CENTRAL_RESERVED','CENTRAL_AVAILABLE','CENTRAL_QUARANTINE','TRANSIT','DESTINATION_AVAILABLE','DESTINATION_QUARANTINE','DISCREPANCY','RETURNED_EXTERNAL'))
);
CREATE INDEX v4_inventory_custody ON purchasing_v4.inventory_movement(purchase_order_line_id,destination_store_id,event_id);
CREATE TABLE purchasing_v4.inventory_command(
 id uuid PRIMARY KEY,event_id uuid NOT NULL REFERENCES purchasing_v4.receiving_event(id),
 movement_id uuid NOT NULL REFERENCES purchasing_v4.inventory_movement(id),
 api_version text NOT NULL,payload jsonb NOT NULL,payload_digest char(64) NOT NULL,
 predecessor_id uuid REFERENCES purchasing_v4.inventory_command(id),
 created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(movement_id)
);
CREATE INDEX v4_inventory_command_predecessor ON purchasing_v4.inventory_command(predecessor_id);
CREATE TABLE purchasing_v4.inventory_operation(
 command_id uuid PRIMARY KEY REFERENCES purchasing_v4.inventory_command(id),
 state text NOT NULL CHECK(state IN ('PENDING','IN_FLIGHT','AMBIGUOUS','VERIFIED','REJECTED')),
 version bigint NOT NULL DEFAULT 0 CHECK(version>=0),attempt_id uuid,
 evidence jsonb NOT NULL DEFAULT '{}'::jsonb,updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_inventory_pending ON purchasing_v4.inventory_operation(state,updated_at) WHERE state<>'VERIFIED';
CREATE TABLE purchasing_v4.inventory_attempt(
 id uuid PRIMARY KEY,command_id uuid NOT NULL REFERENCES purchasing_v4.inventory_command(id),
 kind text NOT NULL CHECK(kind IN ('SEND','RECONCILE','INTERRUPTED')),result text NOT NULL,
 evidence jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_inventory_attempt_command ON purchasing_v4.inventory_attempt(command_id,created_at);
CREATE TABLE purchasing_v4.inventory_verification(
 command_id uuid PRIMARY KEY REFERENCES purchasing_v4.inventory_command(id),
 remote_adjustment_id text NOT NULL UNIQUE,evidence jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE VIEW purchasing_v4.inventory_balance AS
 SELECT purchase_order_line_id,destination_store_id,account,sum(units)::bigint AS units FROM (
 SELECT purchase_order_line_id,destination_store_id,from_account AS account,-units AS units FROM purchasing_v4.inventory_movement
 UNION ALL SELECT purchase_order_line_id,destination_store_id,to_account,units FROM purchasing_v4.inventory_movement
 ) legs GROUP BY purchase_order_line_id,destination_store_id,account;
CREATE FUNCTION purchasing_v4.receiving_snapshot_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.po_ownership o JOIN purchasing_v4.authorized_vendor v ON v.id=o.vendor_authorization_id
 WHERE o.purchase_order_id=NEW.purchase_order_id AND o.origin='V4' AND v.id=NEW.vendor_authorization_id AND v.kind='PO') THEN RAISE EXCEPTION 'Only the existing canonical V4 PO may enter central receiving';END IF;
 IF NOT EXISTS(SELECT 1 FROM public.stores WHERE id=NEW.central_store_id AND active AND name='HWY 99' AND square_location_id IS NOT NULL)
 OR (NEW.snapshot->'central'->>'id')::bigint<>NEW.central_store_id THEN RAISE EXCEPTION 'HWY 99 is the fixed actual receiving store';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_receiving_fixed_central BEFORE INSERT ON purchasing_v4.receiving_order FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_snapshot_guard();
CREATE FUNCTION purchasing_v4.receiving_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='UPDATE' AND (NEW.purchase_order_id<>OLD.purchase_order_id OR NEW.version<>OLD.version+1) THEN RAISE EXCEPTION 'Successive physical-event version required';END IF;
 IF NEW.version>0 AND NOT EXISTS(SELECT 1 FROM purchasing_v4.receiving_event WHERE id=NEW.event_id AND purchase_order_id=NEW.purchase_order_id AND version=NEW.version) THEN RAISE EXCEPTION 'Exact immutable physical event required';END IF;
 RETURN NEW;END $$;
CREATE TRIGGER v4_receiving_head_transition BEFORE INSERT OR UPDATE ON purchasing_v4.receiving_head FOR EACH ROW EXECUTE FUNCTION purchasing_v4.receiving_head_guard();
DO $$ DECLARE t text; BEGIN FOREACH t IN ARRAY ARRAY['receiving_order','receiving_event','receiving_grant','inventory_movement','inventory_command','inventory_attempt','inventory_verification'] LOOP
 EXECUTE format('CREATE TRIGGER v4_fact_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 EXECUTE format('CREATE TRIGGER v4_fact_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['receiving_head','inventory_operation'] LOOP
 EXECUTE format('CREATE TRIGGER v4_state_no_delete BEFORE DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 EXECUTE format('CREATE TRIGGER v4_state_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 END LOOP; END $$;
GRANT SELECT ON purchasing_v4.receiving_order TO erupted_v4_po_writer;
GRANT INSERT ON purchasing_v4.receiving_order,purchasing_v4.receiving_head TO erupted_v4_po_writer;
''')


def downgrade():
 raise RuntimeError('Physical custody and inventory evidence must be retained; disable new actions and recover forward')
