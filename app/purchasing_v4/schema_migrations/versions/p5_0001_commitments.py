"""Isolated authorization, one-writer PO bridge, immutable artifacts and outbox."""
from alembic import op
revision='p5_0001'
down_revision='p4_0003'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''
ALTER TABLE public.vendors ALTER COLUMN square_vendor_id DROP NOT NULL;
ALTER TABLE public.vendor_assignment_changes ALTER COLUMN source_square_vendor_id DROP NOT NULL;
ALTER TABLE public.vendor_assignment_changes ALTER COLUMN prior_square_vendor_id DROP NOT NULL;
ALTER TABLE public.vendor_assignment_changes ALTER COLUMN new_square_vendor_id DROP NOT NULL;
ALTER TABLE public.purchase_orders ALTER COLUMN reorder_weeks DROP NOT NULL;
ALTER TABLE public.purchase_orders ALTER COLUMN stock_up_weeks DROP NOT NULL;
ALTER TABLE public.purchase_orders ALTER COLUMN history_lookback_days DROP NOT NULL;
CREATE TABLE purchasing_v4.commitment_owner (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), principal_id bigint NOT NULL UNIQUE REFERENCES public.principals(id)
);
CREATE TABLE purchasing_v4.submission_review (
 id uuid PRIMARY KEY, owner_id bigint NOT NULL REFERENCES public.principals(id),
 session_id uuid NOT NULL REFERENCES purchasing_v4.worksheet(id), worksheet_version bigint NOT NULL,
 request_digest char(64) NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_review_session ON purchasing_v4.submission_review(session_id,created_at);
CREATE TABLE purchasing_v4.purchase_authorization (
 id uuid PRIMARY KEY, review_id uuid NOT NULL UNIQUE REFERENCES purchasing_v4.submission_review(id),
 owner_id bigint NOT NULL REFERENCES public.principals(id), request_digest char(64) NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.authorization_command (
 id uuid PRIMARY KEY, owner_id bigint NOT NULL REFERENCES public.principals(id), request_digest char(64) NOT NULL,
 authorization_id uuid NOT NULL REFERENCES purchasing_v4.purchase_authorization(id), result jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.authorized_vendor (
 id uuid PRIMARY KEY, authorization_id uuid NOT NULL REFERENCES purchasing_v4.purchase_authorization(id) DEFERRABLE INITIALLY DEFERRED,
 supplier_id uuid NOT NULL REFERENCES purchasing_v4.authority_supplier(id), kind text NOT NULL CHECK(kind IN ('PO','REQUEST')),
 purchase_order_id bigint UNIQUE REFERENCES public.purchase_orders(id) DEFERRABLE INITIALLY DEFERRED,
 snapshot jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(authorization_id,supplier_id), CHECK((kind='PO')=(purchase_order_id IS NOT NULL))
);
CREATE TABLE purchasing_v4.financial_line_bridge (
 purchase_order_line_id bigint PRIMARY KEY REFERENCES public.purchase_order_lines(id) DEFERRABLE INITIALLY DEFERRED,
 vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id), row_id uuid NOT NULL,
 approved_unit_value numeric(14,4) NOT NULL CHECK(approved_unit_value>=0), UNIQUE(vendor_authorization_id,row_id)
);
CREATE TABLE purchasing_v4.purchasing_intent (
 id uuid PRIMARY KEY, session_id uuid NOT NULL, row_id uuid NOT NULL, store_id bigint NOT NULL REFERENCES public.stores(id),
 vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id), units bigint NOT NULL CHECK(units>=0),
 reviewed_supply_digest char(64) NOT NULL, incremental_reason text, prior_supply jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(session_id,row_id,store_id),
 FOREIGN KEY(session_id,row_id) REFERENCES purchasing_v4.worksheet_row(session_id,id)
);
CREATE TABLE purchasing_v4.intent_consumption (
 intent_id uuid PRIMARY KEY REFERENCES purchasing_v4.purchasing_intent(id), vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.request_reservation (
 intent_id uuid PRIMARY KEY REFERENCES purchasing_v4.purchasing_intent(id), vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_intent_variation_scope ON purchasing_v4.purchasing_intent(session_id,row_id);
CREATE TABLE purchasing_v4.worksheet_seal (
 session_id uuid NOT NULL, row_id uuid NOT NULL, vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 PRIMARY KEY(session_id,row_id), FOREIGN KEY(session_id,row_id) REFERENCES purchasing_v4.worksheet_row(session_id,id)
);
CREATE TABLE purchasing_v4.supplier_financial_bridge (
 supplier_id uuid PRIMARY KEY REFERENCES purchasing_v4.authority_supplier(id), legacy_vendor_id bigint NOT NULL UNIQUE REFERENCES public.vendors(id),
 origin text NOT NULL CHECK(origin IN ('LOCAL','SQUARE')), provenance jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.po_ownership (
 purchase_order_id bigint PRIMARY KEY, origin text NOT NULL CHECK(origin IN ('LEGACY','V4')),
 writer text NOT NULL CHECK(writer IN ('LEGACY','V4')), vendor_authorization_id uuid UNIQUE REFERENCES purchasing_v4.authorized_vendor(id),
 created_at timestamptz NOT NULL DEFAULT now(), CHECK((origin='V4')=(vendor_authorization_id IS NOT NULL)),CHECK(origin=writer)
);
INSERT INTO purchasing_v4.po_ownership(purchase_order_id,origin,writer) SELECT id,'LEGACY','LEGACY' FROM public.purchase_orders;
CREATE TABLE purchasing_v4.integration_command (
 id uuid PRIMARY KEY, vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 kind text NOT NULL CHECK(kind IN ('GENERATE_DOCUMENTS','DELIVER_DOCUMENTS')),
 payload jsonb NOT NULL, payload_digest char(64) NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(vendor_authorization_id,kind)
);
CREATE TABLE purchasing_v4.integration_state (
 command_id uuid PRIMARY KEY REFERENCES purchasing_v4.integration_command(id), version bigint NOT NULL DEFAULT 0,
 state text NOT NULL CHECK(state IN ('PENDING','LEASED','ACKNOWLEDGED','AMBIGUOUS','FAILED')),
 lease_id uuid, lease_until timestamptz, evidence jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.integration_attempt (
 id uuid PRIMARY KEY, command_id uuid NOT NULL REFERENCES purchasing_v4.integration_command(id),
 outcome text NOT NULL, evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.vendor_artifact (
 id uuid PRIMARY KEY, vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 command_id uuid NOT NULL REFERENCES purchasing_v4.integration_command(id), format text NOT NULL CHECK(format IN ('PDF','XLSX')),
 sha256 char(64) NOT NULL, content bytea NOT NULL, public_projection jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(vendor_authorization_id,format)
);
CREATE FUNCTION purchasing_v4.authorization_owner_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.commitment_owner WHERE principal_id=NEW.owner_id) THEN RAISE EXCEPTION 'Only the explicitly configured owner may authorize purchasing'; END IF; RETURN NEW; END $$;
CREATE TRIGGER v4_authorization_owner BEFORE INSERT ON purchasing_v4.purchase_authorization FOR EACH ROW EXECUTE FUNCTION purchasing_v4.authorization_owner_guard();
CREATE TRIGGER v4_review_owner BEFORE INSERT ON purchasing_v4.submission_review FOR EACH ROW EXECUTE FUNCTION purchasing_v4.authorization_owner_guard();
CREATE FUNCTION purchasing_v4.sealed_worksheet_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.working<>OLD.working AND EXISTS(SELECT 1 FROM purchasing_v4.worksheet_seal s WHERE s.session_id=OLD.session_id AND s.row_id=OLD.id)
 THEN RAISE EXCEPTION 'Submitted purchasing intent is sealed; create a separately reviewed incremental worksheet'; END IF; RETURN NEW; END $$;
CREATE TRIGGER v4_sealed_worksheet BEFORE UPDATE ON purchasing_v4.worksheet_row FOR EACH ROW EXECUTE FUNCTION purchasing_v4.sealed_worksheet_guard();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['financial_line_bridge','commitment_owner','intent_consumption','request_reservation','submission_review','purchase_authorization','authorization_command','authorized_vendor','purchasing_intent','worksheet_seal','supplier_financial_bridge','po_ownership','integration_command','integration_attempt','vendor_artifact'] LOOP
  EXECUTE format('CREATE TRIGGER v4_fact_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
  EXECUTE format('CREATE TRIGGER v4_fact_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 END LOOP;
END $$;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='erupted_v4_po_writer') THEN CREATE ROLE erupted_v4_po_writer NOLOGIN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='erupted_v4_outbox_worker') THEN CREATE ROLE erupted_v4_outbox_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION; END IF;
 ALTER ROLE erupted_v4_outbox_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
END $$;
GRANT USAGE ON SCHEMA public,purchasing_v4 TO erupted_v4_po_writer,erupted_v4_outbox_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA public,purchasing_v4 TO erupted_v4_po_writer;
GRANT INSERT ON public.purchase_orders,public.purchase_order_lines,public.purchase_order_store_allocations,public.vendors TO erupted_v4_po_writer;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO erupted_v4_po_writer;
GRANT INSERT ON purchasing_v4.financial_line_bridge,purchasing_v4.intent_consumption,purchasing_v4.request_reservation,purchasing_v4.authorized_vendor,purchasing_v4.supplier_financial_bridge,purchasing_v4.integration_command,purchasing_v4.integration_state,purchasing_v4.purchasing_intent,purchasing_v4.worksheet_seal,purchasing_v4.purchase_authorization,purchasing_v4.authorization_command TO erupted_v4_po_writer;
GRANT UPDATE(version,updated_at) ON purchasing_v4.worksheet TO erupted_v4_po_writer;
GRANT SELECT ON purchasing_v4.integration_command,purchasing_v4.integration_state,purchasing_v4.vendor_artifact TO erupted_v4_outbox_worker;
GRANT INSERT ON purchasing_v4.integration_command,purchasing_v4.integration_state,purchasing_v4.integration_attempt,purchasing_v4.vendor_artifact TO erupted_v4_outbox_worker;
GRANT UPDATE ON purchasing_v4.integration_state TO erupted_v4_outbox_worker;
CREATE FUNCTION purchasing_v4.register_po_writer() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE a uuid; BEGIN
 IF current_setting('role',true)='erupted_v4_po_writer' THEN
  a=nullif(current_setting('purchasing_v4.vendor_authorization',true),'')::uuid;
  IF NOT EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor v WHERE v.id=a AND v.purchase_order_id=NEW.id AND v.kind='PO') THEN RAISE EXCEPTION 'V4 PO requires its exact owner authorization'; END IF;
  INSERT INTO purchasing_v4.po_ownership(purchase_order_id,origin,writer,vendor_authorization_id) VALUES(NEW.id,'V4','V4',a);
 ELSE INSERT INTO purchasing_v4.po_ownership(purchase_order_id,origin,writer) VALUES(NEW.id,'LEGACY','LEGACY'); END IF;
 RETURN NEW; END $$;
CREATE TRIGGER v4_register_po_writer AFTER INSERT ON public.purchase_orders FOR EACH ROW EXECUTE FUNCTION purchasing_v4.register_po_writer();
CREATE FUNCTION purchasing_v4.supply_write_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE p bigint; oldp bigint; l bigint; protected boolean; purchase_change boolean; BEGIN
 PERFORM pg_advisory_xact_lock(73520405);
 IF TG_TABLE_NAME='purchase_orders' THEN p=CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
 ELSIF TG_TABLE_NAME='purchase_order_lines' THEN p=CASE WHEN TG_OP='DELETE' THEN OLD.purchase_order_id ELSE NEW.purchase_order_id END;
 ELSIF TG_TABLE_NAME='purchase_order_receipts' THEN p=CASE WHEN TG_OP='DELETE' THEN OLD.purchase_order_id ELSE NEW.purchase_order_id END;
 ELSE
  l=CASE WHEN TG_OP='DELETE' THEN OLD.purchase_order_line_id ELSE NEW.purchase_order_line_id END;
  SELECT purchase_order_id INTO p FROM public.purchase_order_lines WHERE id=l;
 END IF;
 IF TG_OP='UPDATE' THEN
  IF TG_TABLE_NAME='purchase_orders' THEN oldp=OLD.id;
  ELSIF TG_TABLE_NAME IN ('purchase_order_lines','purchase_order_receipts') THEN oldp=OLD.purchase_order_id;
  ELSE SELECT purchase_order_id INTO oldp FROM public.purchase_order_lines WHERE id=OLD.purchase_order_line_id; END IF;
 END IF;
 SELECT bool_or(origin='V4') INTO protected FROM purchasing_v4.po_ownership WHERE purchase_order_id IN (p,oldp);
 IF protected AND TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Authorized V4 purchasing facts are immutable'; END IF;
 IF protected AND EXISTS(SELECT 1 FROM purchasing_v4.po_ownership o JOIN purchasing_v4.authorized_vendor v ON v.id=o.vendor_authorization_id JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id WHERE o.purchase_order_id=p) THEN RAISE EXCEPTION 'Issued V4 PO cannot gain unreviewed lines or receipts'; END IF;
 IF protected AND current_setting('role',true)<>'erupted_v4_po_writer' THEN RAISE EXCEPTION 'This PO has one authoritative V4 writer; legacy receiving and editing are prohibited'; END IF;
 IF TG_TABLE_NAME='purchase_order_lines' THEN IF TG_OP='UPDATE' THEN IF coalesce(protected,false) AND NEW.purchase_order_id<>OLD.purchase_order_id THEN RAISE EXCEPTION 'PO writer ownership cannot move'; END IF; END IF; END IF;

 IF TG_TABLE_NAME='purchase_order_lines' THEN
 IF TG_OP='INSERT' THEN purchase_change=NEW.ordered_qty>0; ELSIF TG_OP='UPDATE' THEN purchase_change=NEW.ordered_qty>OLD.ordered_qty OR (NEW.variation_id<>OLD.variation_id AND NEW.ordered_qty>0); ELSE purchase_change=false; END IF;
 IF purchase_change THEN IF NOT coalesce(protected,false) THEN
 IF current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'Retry legacy purchasing under read committed for supply serialization'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchase_order_lines l JOIN public.purchase_orders q ON q.id=l.purchase_order_id JOIN purchasing_v4.po_ownership o ON o.purchase_order_id=q.id
  WHERE o.origin='V4' AND l.variation_id=NEW.variation_id AND q.status IN ('IN_TRANSIT','RECEIVED_SPLIT_PENDING') AND NOT l.removed
  AND l.ordered_qty>l.received_qty_total+coalesce((SELECT sum(a.closed_quantity) FROM public.purchase_order_receipt_allocations a WHERE a.purchase_order_line_id=l.id),0))
 OR EXISTS(SELECT 1 FROM purchasing_v4.request_reservation r JOIN purchasing_v4.purchasing_intent i ON i.id=r.intent_id JOIN purchasing_v4.worksheet_row w ON w.session_id=i.session_id AND w.id=i.row_id
  JOIN purchasing_v4.variation_reference ref ON ref.variation_id=w.variation_id WHERE ref.square_variation_id=NEW.variation_id AND i.units>0 AND NOT EXISTS(SELECT 1 FROM purchasing_v4.intent_consumption x WHERE x.intent_id=i.id))
 THEN RAISE EXCEPTION 'Overlapping V4 supply or pending request requires separately reviewed incremental V4 authorization'; END IF;
 END IF; END IF; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END $$;
CREATE FUNCTION purchasing_v4.supply_truncate_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$ BEGIN
 PERFORM pg_advisory_xact_lock(73520405);
 IF EXISTS(SELECT 1 FROM purchasing_v4.po_ownership WHERE origin='V4') THEN RAISE EXCEPTION 'Authorized V4 PO facts cannot be truncated'; END IF; RETURN NULL; END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['purchase_orders','purchase_order_lines','purchase_order_store_allocations','purchase_order_receipts','purchase_order_receipt_lines','purchase_order_receipt_allocations'] LOOP
  EXECUTE format('CREATE TRIGGER v4_supply_write_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.supply_write_guard()',t);
  EXECUTE format('CREATE TRIGGER v4_supply_no_truncate BEFORE TRUNCATE ON public.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.supply_truncate_guard()',t);
 END LOOP;
END $$;
''')
 op.execute("CREATE FUNCTION purchasing_v4.store_supply_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN\n PERFORM pg_advisory_xact_lock(73520405); IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END $$;\nCREATE TRIGGER v4_store_supply_guard BEFORE INSERT OR UPDATE OR DELETE ON public.stores FOR EACH ROW EXECUTE FUNCTION purchasing_v4.store_supply_guard();\nCREATE FUNCTION purchasing_v4.validate_financial_bridge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$\nDECLARE v purchasing_v4.authorized_vendor; count_lines integer; BEGIN\n SELECT a.* INTO v FROM purchasing_v4.authorized_vendor a WHERE a.purchase_order_id=NEW.id;\n IF NOT FOUND THEN RETURN NULL; END IF;\n IF NOT EXISTS(SELECT 1 FROM purchasing_v4.supplier_financial_bridge b WHERE b.supplier_id=v.supplier_id AND b.legacy_vendor_id=NEW.vendor_id) THEN RAISE EXCEPTION 'PO financial supplier identity does not match authorization'; END IF;\n SELECT count(*) INTO count_lines FROM public.purchase_order_lines WHERE purchase_order_id=NEW.id;\n IF count_lines<>jsonb_array_length(v.snapshot->'lines') OR count_lines=0 THEN RAISE EXCEPTION 'PO line count does not match authorization'; END IF;\n IF EXISTS(SELECT 1 FROM public.purchase_order_lines l LEFT JOIN purchasing_v4.financial_line_bridge b ON b.purchase_order_line_id=l.id\n LEFT JOIN LATERAL (SELECT j FROM jsonb_array_elements(v.snapshot->'lines') j WHERE j->>'row_id'=b.row_id::text) expected ON true\n WHERE l.purchase_order_id=NEW.id AND (b.vendor_authorization_id IS DISTINCT FROM v.id OR expected.j IS NULL\n OR l.variation_id IS DISTINCT FROM expected.j->>'square_variation_id'\n OR l.unit_cost IS DISTINCT FROM (expected.j->'valuation'->>'unit_value')::numeric\n OR l.unit_cost IS DISTINCT FROM b.approved_unit_value OR l.ordered_qty<>(expected.j->>'total_units')::bigint\n OR l.ordered_qty<>(SELECT coalesce(sum(a.allocated_qty),0) FROM public.purchase_order_store_allocations a WHERE a.purchase_order_line_id=l.id)\n OR 4<>(SELECT count(*) FROM public.purchase_order_store_allocations a WHERE a.purchase_order_line_id=l.id)\n OR EXISTS(SELECT 1 FROM jsonb_array_elements(v.snapshot->'stores') WITH ORDINALITY s(store,n)\n LEFT JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id AND a.store_id=(s.store->>'id')::bigint\n WHERE a.allocated_qty IS DISTINCT FROM (expected.j->'units'->>(s.n-1)::int)::integer))) THEN RAISE EXCEPTION 'Exact approved valuation, identity and store allocation conservation required'; END IF;\n RETURN NULL; END $$;\nCREATE CONSTRAINT TRIGGER v4_financial_bridge AFTER INSERT ON public.purchase_orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.validate_financial_bridge();\n")
 op.execute("CREATE FUNCTION purchasing_v4.integration_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN\n IF NEW.command_id<>OLD.command_id OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'Outbox state requires a successive version and immutable command identity'; END IF;\n IF OLD.state='ACKNOWLEDGED' THEN RAISE EXCEPTION 'Acknowledged integration result is terminal; append reconciliation evidence separately'; END IF;\n IF NEW.state='LEASED' THEN\n  IF NEW.lease_id IS NULL OR NEW.lease_until<=now() OR (OLD.state='LEASED' AND OLD.lease_until>now()) THEN RAISE EXCEPTION 'A valid exclusive worker lease is required'; END IF;\n ELSIF NEW.state IN ('ACKNOWLEDGED','AMBIGUOUS','FAILED') THEN\n  IF OLD.state<>'LEASED' OR NEW.lease_id IS DISTINCT FROM OLD.lease_id OR NOT EXISTS(SELECT 1 FROM purchasing_v4.integration_attempt a WHERE a.command_id=NEW.command_id AND a.outcome=NEW.state AND a.evidence=NEW.evidence AND a.evidence->>'lease_id'=OLD.lease_id::text)\n   THEN RAISE EXCEPTION 'Integration outcome requires exact immutable attempt evidence for its lease'; END IF;\n ELSE RAISE EXCEPTION 'No silent reset of durable integration outcomes'; END IF;\n RETURN NEW; END $$;\nCREATE TRIGGER v4_outbox_transitions BEFORE UPDATE ON purchasing_v4.integration_state FOR EACH ROW EXECUTE FUNCTION purchasing_v4.integration_transition_guard();\nGRANT SELECT ON purchasing_v4.integration_attempt TO erupted_v4_outbox_worker;\n")


def downgrade():raise RuntimeError('Retain authorized POs, intents and outbox evidence; code rollback and forward recovery only')
