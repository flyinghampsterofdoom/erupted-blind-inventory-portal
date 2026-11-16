"""Explicit additive submission ledger overlay. Never called at web startup.

Reuses the reviewed Phase 5 ledger and exact-financial constraint, without its
PO ownership registry, receiving takeover, worker login or global write freeze.
"""
from sqlalchemy import text
from .isolation import require_isolated
from . import controls,submission

CORE_DDL = r"""
ALTER TABLE public.vendors ALTER COLUMN square_vendor_id DROP NOT NULL;
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
 FOREACH t IN ARRAY ARRAY['financial_line_bridge','commitment_owner','intent_consumption','request_reservation','submission_review','purchase_authorization','authorization_command','authorized_vendor','purchasing_intent','worksheet_seal','supplier_financial_bridge','integration_command','integration_attempt','vendor_artifact'] LOOP
  EXECUTE format('CREATE TRIGGER v4_fact_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
  EXECUTE format('CREATE TRIGGER v4_fact_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 END LOOP;
END $$;
"""

FINANCIAL_DDL = r"""CREATE FUNCTION purchasing_v4.validate_financial_bridge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE v purchasing_v4.authorized_vendor; count_lines integer; BEGIN
 SELECT a.* INTO v FROM purchasing_v4.authorized_vendor a WHERE a.purchase_order_id=NEW.id;
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.supplier_financial_bridge b WHERE b.supplier_id=v.supplier_id AND b.legacy_vendor_id=NEW.vendor_id) THEN RAISE EXCEPTION 'PO financial supplier identity does not match authorization'; END IF;
 SELECT count(*) INTO count_lines FROM public.purchase_order_lines WHERE purchase_order_id=NEW.id;
 IF count_lines<>jsonb_array_length(v.snapshot->'lines') OR count_lines=0 THEN RAISE EXCEPTION 'PO line count does not match authorization'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchase_order_lines l LEFT JOIN purchasing_v4.financial_line_bridge b ON b.purchase_order_line_id=l.id
 LEFT JOIN LATERAL (SELECT j FROM jsonb_array_elements(v.snapshot->'lines') j WHERE j->>'row_id'=b.row_id::text) expected ON true
 WHERE l.purchase_order_id=NEW.id AND (b.vendor_authorization_id IS DISTINCT FROM v.id OR expected.j IS NULL
 OR l.variation_id IS DISTINCT FROM expected.j->>'square_variation_id'
 OR l.unit_cost IS DISTINCT FROM (expected.j->'valuation'->>'unit_value')::numeric
 OR l.unit_cost IS DISTINCT FROM b.approved_unit_value OR l.ordered_qty<>(expected.j->>'total_units')::bigint
 OR l.ordered_qty<>(SELECT coalesce(sum(a.allocated_qty),0) FROM public.purchase_order_store_allocations a WHERE a.purchase_order_line_id=l.id)
 OR 4<>(SELECT count(*) FROM public.purchase_order_store_allocations a WHERE a.purchase_order_line_id=l.id)
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(v.snapshot->'stores') WITH ORDINALITY s(store,n)
 LEFT JOIN public.purchase_order_store_allocations a ON a.purchase_order_line_id=l.id AND a.store_id=(s.store->>'id')::bigint
 WHERE a.allocated_qty IS DISTINCT FROM (expected.j->'units'->>(s.n-1)::int)::integer))) THEN RAISE EXCEPTION 'Exact approved valuation, identity and store allocation conservation required'; END IF;
 RETURN NULL; END $$;
CREATE CONSTRAINT TRIGGER v4_financial_bridge AFTER INSERT ON public.purchase_orders DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.validate_financial_bridge();
"""


ROLE_DDL = r"""
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='erupted_v4_po_writer') THEN
  CREATE ROLE erupted_v4_po_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='erupted_v4_po_writer'
   AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls))
 THEN RAISE EXCEPTION 'Submission writer role has unqualified privileges'; END IF;
END $$;
GRANT USAGE ON SCHEMA public,purchasing_v4 TO erupted_v4_po_writer;
GRANT SELECT ON public.vendors,public.purchase_orders,public.purchase_order_lines,
 public.purchase_order_store_allocations,public.stores TO erupted_v4_po_writer;
GRANT SELECT ON purchasing_v4.supplier_reference,purchasing_v4.worksheet,
 purchasing_v4.worksheet_row TO erupted_v4_po_writer;
GRANT INSERT ON public.purchase_orders,public.purchase_order_lines,public.purchase_order_store_allocations,public.vendors TO erupted_v4_po_writer;
GRANT USAGE,SELECT ON SEQUENCE public.purchase_orders_id_seq,public.purchase_order_lines_id_seq,
 public.purchase_order_store_allocations_id_seq,public.vendors_id_seq TO erupted_v4_po_writer;
GRANT SELECT,INSERT ON purchasing_v4.financial_line_bridge,purchasing_v4.intent_consumption,
 purchasing_v4.request_reservation,purchasing_v4.authorized_vendor,purchasing_v4.supplier_financial_bridge,
 purchasing_v4.integration_command,purchasing_v4.integration_state,purchasing_v4.purchasing_intent,
 purchasing_v4.worksheet_seal,purchasing_v4.purchase_authorization,purchasing_v4.authorization_command,
 purchasing_v4.vendor_artifact,purchasing_v4.integration_attempt TO erupted_v4_po_writer;
GRANT SELECT ON purchasing_v4.commitment_owner,purchasing_v4.submission_review TO erupted_v4_po_writer;
GRANT UPDATE(version,updated_at) ON purchasing_v4.worksheet TO erupted_v4_po_writer;
GRANT UPDATE ON purchasing_v4.integration_state TO erupted_v4_po_writer;
CREATE TABLE purchasing_v4.production_submission_contract (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), version integer NOT NULL CHECK(version=1),
 owner_principal_id bigint NOT NULL REFERENCES public.principals(id), approval_reference text NOT NULL CHECK(length(approval_reference)>0),
 installed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER v4_production_contract_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.production_submission_contract
 FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_production_contract_no_truncate BEFORE TRUNCATE ON purchasing_v4.production_submission_contract
 FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
"""

# Serialized supply reads and writes retain the existing advisory-lock identity.
# Only approved purchasing facts are frozen; canonical receipt/balance/status
# changes continue through the current receiving service.
GUARD_DDL = r"""
CREATE FUNCTION purchasing_v4.production_supply_guard() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE po bigint; line bigint; sealed boolean; purchase_change boolean; BEGIN
 PERFORM pg_advisory_xact_lock(73520405);
 IF TG_TABLE_NAME='purchase_orders' THEN
  po=CASE WHEN TG_OP='DELETE' THEN OLD.id ELSE NEW.id END;
  IF TG_OP='INSERT' AND current_setting('role',true)='erupted_v4_po_writer' THEN
   IF NOT EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor v WHERE v.purchase_order_id=NEW.id AND v.kind='PO'
      AND (v.snapshot->>'authorized_by')::bigint=NEW.created_by_principal_id
      AND (v.snapshot->>'authorized_by')::bigint=NEW.submitted_by_principal_id)
    THEN RAISE EXCEPTION 'Production writer requires its exact owner authorization'; END IF;
  END IF;
  IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor WHERE purchase_order_id=po)
   AND (NEW.vendor_id IS DISTINCT FROM OLD.vendor_id OR NEW.created_by_principal_id IS DISTINCT FROM OLD.created_by_principal_id
    OR NEW.submitted_by_principal_id IS DISTINCT FROM OLD.submitted_by_principal_id OR NEW.ordered_at IS DISTINCT FROM OLD.ordered_at
    OR NEW.submitted_at IS DISTINCT FROM OLD.submitted_at)
   THEN RAISE EXCEPTION 'Reviewed purchasing identity and dates are immutable'; END IF;
 ELSIF TG_TABLE_NAME='purchase_order_lines' THEN
  po=CASE WHEN TG_OP='DELETE' THEN OLD.purchase_order_id ELSE NEW.purchase_order_id END;
  line=CASE WHEN TG_OP IN ('DELETE','UPDATE') THEN OLD.id ELSE NEW.id END;
  SELECT EXISTS(SELECT 1 FROM purchasing_v4.financial_line_bridge WHERE purchase_order_line_id=line) INTO sealed;
  IF sealed AND TG_OP='DELETE' THEN RAISE EXCEPTION 'Reviewed purchasing lines cannot be deleted'; END IF;
  IF sealed AND TG_OP='UPDATE' AND (NEW.id IS DISTINCT FROM OLD.id OR NEW.purchase_order_id IS DISTINCT FROM OLD.purchase_order_id
   OR NEW.variation_id IS DISTINCT FROM OLD.variation_id OR NEW.unit_cost IS DISTINCT FROM OLD.unit_cost
   OR NEW.ordered_qty IS DISTINCT FROM OLD.ordered_qty OR NEW.removed IS DISTINCT FROM OLD.removed)
   THEN RAISE EXCEPTION 'Reviewed quantities, identity and valuation are immutable; receiving balances remain separate'; END IF;
  IF TG_OP IN ('INSERT','UPDATE') AND EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor v JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id WHERE v.purchase_order_id=po) AND (TG_OP='INSERT' OR NEW.purchase_order_id IS DISTINCT FROM OLD.purchase_order_id)
   THEN RAISE EXCEPTION 'Issued purchasing order cannot gain unreviewed lines'; END IF;
  purchase_change=CASE WHEN TG_OP='INSERT' THEN NEW.ordered_qty>0
   WHEN TG_OP='UPDATE' THEN NEW.ordered_qty>OLD.ordered_qty OR (NEW.variation_id IS DISTINCT FROM OLD.variation_id AND NEW.ordered_qty>0)
   ELSE false END;
  IF purchase_change AND current_setting('role',true)<>'erupted_v4_po_writer' AND (
   EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor v JOIN public.purchase_orders p ON p.id=v.purchase_order_id
    JOIN public.purchase_order_lines l ON l.purchase_order_id=p.id
    WHERE l.variation_id=NEW.variation_id AND NOT l.removed AND p.status IN ('IN_TRANSIT','RECEIVED_SPLIT_PENDING')
    AND l.ordered_qty>l.received_qty_total+coalesce((SELECT sum(a.closed_quantity) FROM public.purchase_order_receipt_allocations a WHERE a.purchase_order_line_id=l.id),0))
   OR EXISTS(SELECT 1 FROM purchasing_v4.request_reservation r JOIN purchasing_v4.purchasing_intent i ON i.id=r.intent_id
    JOIN purchasing_v4.worksheet_row w ON w.session_id=i.session_id AND w.id=i.row_id
    JOIN purchasing_v4.variation_reference ref ON ref.variation_id=w.variation_id
    WHERE ref.square_variation_id=NEW.variation_id AND i.units>0
     AND NOT EXISTS(SELECT 1 FROM purchasing_v4.intent_consumption x WHERE x.intent_id=i.id)))
   THEN RAISE EXCEPTION 'Existing submitted supply or pending request requires separately reviewed incremental authorization'; END IF;
 ELSIF TG_TABLE_NAME='purchase_order_store_allocations' THEN
  line=CASE WHEN TG_OP IN ('DELETE','UPDATE') THEN OLD.purchase_order_line_id ELSE NEW.purchase_order_line_id END;
  IF TG_OP='INSERT' AND EXISTS(SELECT 1 FROM purchasing_v4.financial_line_bridge b
    JOIN purchasing_v4.authorized_vendor v ON v.id=b.vendor_authorization_id
    JOIN purchasing_v4.purchase_authorization a ON a.id=v.authorization_id WHERE b.purchase_order_line_id=line)
   THEN RAISE EXCEPTION 'Issued purchasing order cannot gain unreviewed allocations'; END IF;
  IF EXISTS(SELECT 1 FROM purchasing_v4.financial_line_bridge WHERE purchase_order_line_id=line)
    OR (TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM purchasing_v4.financial_line_bridge WHERE purchase_order_line_id=NEW.purchase_order_line_id)) THEN
   IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Reviewed store allocations cannot be deleted'; END IF;
   IF TG_OP='UPDATE' AND (NEW.purchase_order_line_id IS DISTINCT FROM OLD.purchase_order_line_id OR NEW.store_id IS DISTINCT FROM OLD.store_id
     OR NEW.expected_qty IS DISTINCT FROM OLD.expected_qty OR NEW.allocated_qty IS DISTINCT FROM OLD.allocated_qty)
    THEN RAISE EXCEPTION 'Reviewed store allocations are immutable; receiving quantities remain separate'; END IF;
  END IF;
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['purchase_orders','purchase_order_lines','purchase_order_store_allocations',
 'purchase_order_receipts','purchase_order_receipt_lines','purchase_order_receipt_allocations','stores'] LOOP
  EXECUTE format('CREATE TRIGGER v4_production_supply_lock BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.production_supply_guard()',t);
 END LOOP;
END $$;
"""

def install(connection, *, owner_principal_id, approval_reference):
    """Explicit operator migration, after independent production approval only.

    In development this must run on the bounded disposable fixture database.
    On a production target the caller must attach the exact verified owner
    target; web startup never invokes this function.
    """
    require_isolated(connection)
    controls.owner(connection, owner_principal_id)
    target = getattr(connection.engine, '_purchasing_v4_owner_target', None)
    if target is not None and target.owner_principal_id != owner_principal_id:
        raise PermissionError('Migration owner must match the explicitly bound target')
    if not isinstance(approval_reference, str) or not approval_reference.strip():
        raise ValueError('Independent owner approval reference is required')
    if connection.scalar(text('SELECT version_num FROM purchasing_v4.alembic_version')) != 'p4_0003':
        raise RuntimeError('Production submission overlay requires the qualified worksheet schema')
    connection.execute(text('SELECT pg_advisory_xact_lock(73520405)'))
    if connection.scalar(text("SELECT to_regclass('purchasing_v4.production_submission_contract')")):
        row=connection.execute(text('SELECT version,owner_principal_id FROM purchasing_v4.production_submission_contract')).one()
        if tuple(row)!=(1,owner_principal_id):
            raise PermissionError('Existing production submission contract mismatch')
        return
    if connection.scalar(text("SELECT to_regclass('purchasing_v4.purchase_authorization')")):
        raise RuntimeError('Cannot silently convert the isolated commitment/receiving schema')
    connection.exec_driver_sql(CORE_DDL,execution_options={'no_parameters':True})
    connection.exec_driver_sql(ROLE_DDL,execution_options={'no_parameters':True})
    connection.exec_driver_sql(FINANCIAL_DDL,execution_options={'no_parameters':True})
    connection.exec_driver_sql(GUARD_DDL,execution_options={'no_parameters':True})
    connection.exec_driver_sql(TRANSITION_DDL,execution_options={'no_parameters':True})
    # Role membership is an explicit migration operation, never a startup grant.
    from psycopg import sql
    user=connection.scalar(text('SELECT current_user'))
    connection.connection.driver_connection.execute(sql.SQL('GRANT erupted_v4_po_writer TO {}').format(sql.Identifier(user)))
    submission.configure_owner(connection,owner_principal_id)
    connection.execute(text('INSERT INTO purchasing_v4.production_submission_contract(singleton,version,owner_principal_id,approval_reference) VALUES (true,1,:owner,:approval)'),
        dict(owner=owner_principal_id,approval=approval_reference.strip()))

TRANSITION_DDL = r"""CREATE FUNCTION purchasing_v4.integration_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.command_id<>OLD.command_id OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'Outbox state requires a successive version and immutable command identity'; END IF;
 IF OLD.state='ACKNOWLEDGED' THEN RAISE EXCEPTION 'Acknowledged integration result is terminal; append reconciliation evidence separately'; END IF;
 IF NEW.state='LEASED' THEN
  IF NEW.lease_id IS NULL OR NEW.lease_until<=now() OR (OLD.state='LEASED' AND OLD.lease_until>now()) THEN RAISE EXCEPTION 'A valid exclusive worker lease is required'; END IF;
 ELSIF NEW.state IN ('ACKNOWLEDGED','AMBIGUOUS','FAILED') THEN
  IF OLD.state<>'LEASED' OR NEW.lease_id IS DISTINCT FROM OLD.lease_id OR NOT EXISTS(SELECT 1 FROM purchasing_v4.integration_attempt a WHERE a.command_id=NEW.command_id AND a.outcome=NEW.state AND a.evidence=NEW.evidence AND a.evidence->>'lease_id'=OLD.lease_id::text)
   THEN RAISE EXCEPTION 'Integration outcome requires exact immutable attempt evidence for its lease'; END IF;
 ELSE RAISE EXCEPTION 'No silent reset of durable integration outcomes'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER v4_outbox_transitions BEFORE UPDATE ON purchasing_v4.integration_state FOR EACH ROW EXECUTE FUNCTION purchasing_v4.integration_transition_guard();
"""
