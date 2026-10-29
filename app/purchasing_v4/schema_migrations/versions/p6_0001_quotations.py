"""Versioned quotation evidence and owner lifecycle; one Phase5 PO writer."""
from alembic import op
from sqlalchemy import text
import secrets
revision='p6_0001'
down_revision='p5_0001'
branch_labels=None
depends_on=None


def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 c=op.get_bind();require_isolated(c)
 op.execute('''
CREATE TABLE purchasing_v4.quotation_key(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),secret bytea NOT NULL CHECK(octet_length(secret)=32));
CREATE TABLE purchasing_v4.quotation_revision(
 id uuid PRIMARY KEY,request_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),number integer NOT NULL CHECK(number>0),
 owner_id bigint NOT NULL REFERENCES public.principals(id),snapshot jsonb NOT NULL,public_projection jsonb NOT NULL,
 signature char(64) NOT NULL,template bytea NOT NULL,sha256 char(64) NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(request_id,number)
);
CREATE TABLE purchasing_v4.quotation_response(
 id uuid PRIMARY KEY,request_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),revision_id uuid NOT NULL REFERENCES purchasing_v4.quotation_revision(id),
 semantic_digest char(64) NOT NULL,payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(revision_id,semantic_digest)
);
CREATE TABLE purchasing_v4.quotation_upload(
 id uuid PRIMARY KEY,request_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),owner_id bigint NOT NULL REFERENCES public.principals(id),
 sha256 char(64) NOT NULL,content bytea NOT NULL,filename text NOT NULL,validation jsonb NOT NULL,
 response_id uuid REFERENCES purchasing_v4.quotation_response(id),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(request_id,sha256)
);
CREATE TABLE purchasing_v4.quotation_head(
 request_id uuid PRIMARY KEY REFERENCES purchasing_v4.authorized_vendor(id),version bigint NOT NULL DEFAULT 0 CHECK(version>=0),
 revision_id uuid NOT NULL REFERENCES purchasing_v4.quotation_revision(id),selected_response_id uuid REFERENCES purchasing_v4.quotation_response(id),
 state text NOT NULL CHECK(state IN ('OPEN','CANCELED','COMMITTED')),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.quotation_event(
 command_id uuid PRIMARY KEY,request_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),owner_id bigint NOT NULL REFERENCES public.principals(id),
 kind text NOT NULL CHECK(kind IN ('PREPARE','SELECT_RESPONSE','REISSUE','CANCEL','AUTHORIZE')),request_digest char(64) NOT NULL,
 reason text NOT NULL,result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_quote_upload_history ON purchasing_v4.quotation_upload(request_id,created_at);
CREATE INDEX v4_quote_response_history ON purchasing_v4.quotation_response(request_id,created_at);
CREATE TABLE purchasing_v4.quotation_intent_resolution(
 intent_id uuid PRIMARY KEY REFERENCES purchasing_v4.purchasing_intent(id),vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 source_request_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),approved_units bigint NOT NULL CHECK(approved_units>=0),
 requested_units bigint NOT NULL CHECK(requested_units>=0),evidence jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.quotation_financial(
 purchase_order_id bigint PRIMARY KEY REFERENCES public.purchase_orders(id) DEFERRABLE INITIALLY DEFERRED,
 vendor_authorization_id uuid NOT NULL UNIQUE REFERENCES purchasing_v4.authorized_vendor(id),
 merchandise numeric(14,2) NOT NULL CHECK(merchandise>=0),freight numeric(14,2) NOT NULL CHECK(freight>=0),
 tax numeric(14,2) NOT NULL CHECK(tax>=0),discount numeric(14,2) NOT NULL CHECK(discount>=0),
 total numeric(14,2) NOT NULL CHECK(total>=0 AND total=merchandise+freight+tax-discount),
 evidence jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE VIEW purchasing_v4.active_request_reservation AS
 SELECT r.intent_id,r.vendor_authorization_id,CASE WHEN h.request_id IS NULL THEN i.units ELSE
 coalesce((SELECT (line->'units'->>s.n::integer)::bigint FROM jsonb_array_elements(v.snapshot->'lines') line,
 jsonb_array_elements(v.snapshot->'stores') WITH ORDINALITY b(store,n0),LATERAL(SELECT (b.n0-1)::integer n)s
 WHERE line->>'row_id'=i.row_id::text AND (b.store->>'id')::bigint=i.store_id),0) END AS units
 FROM purchasing_v4.request_reservation r JOIN purchasing_v4.purchasing_intent i ON i.id=r.intent_id
 LEFT JOIN purchasing_v4.quotation_head h ON h.request_id=r.vendor_authorization_id
 LEFT JOIN purchasing_v4.quotation_revision v ON v.id=h.revision_id
 WHERE (h.request_id IS NULL OR h.state='OPEN') AND NOT EXISTS(SELECT 1 FROM purchasing_v4.intent_consumption x WHERE x.intent_id=r.intent_id);
CREATE VIEW purchasing_v4.quotation_delivery_eligibility AS SELECT h.request_id,h.state,v.number FROM purchasing_v4.quotation_head h JOIN purchasing_v4.quotation_revision v ON v.id=h.revision_id;
CREATE FUNCTION purchasing_v4.quotation_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF TG_OP='INSERT' THEN
  IF NOT EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor WHERE id=NEW.request_id AND kind='REQUEST') THEN RAISE EXCEPTION 'Only a retained request may enter quotation review'; END IF;
 ELSE
  IF OLD.state<>'OPEN' OR NEW.request_id<>OLD.request_id OR NEW.version<>OLD.version+1 THEN RAISE EXCEPTION 'Quotation lifecycle requires an open request and successive version'; END IF;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.quotation_revision WHERE id=NEW.revision_id AND request_id=NEW.request_id) THEN RAISE EXCEPTION 'Request revision identity mismatch'; END IF;
 IF NEW.selected_response_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM purchasing_v4.quotation_response WHERE id=NEW.selected_response_id AND revision_id=NEW.revision_id AND request_id=NEW.request_id) THEN RAISE EXCEPTION 'Selected response belongs to another revision'; END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.quotation_event e JOIN purchasing_v4.commitment_owner o ON o.principal_id=e.owner_id WHERE e.request_id=NEW.request_id AND (e.result->>'version')::bigint=NEW.version AND e.result->>'state'=NEW.state AND e.result->>'revision_id'=NEW.revision_id::text AND e.result->>'selected_response_id' IS NOT DISTINCT FROM NEW.selected_response_id::text) THEN RAISE EXCEPTION 'Lifecycle transition requires exact immutable owner event'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER v4_quote_head_transition BEFORE INSERT OR UPDATE ON purchasing_v4.quotation_head FOR EACH ROW EXECUTE FUNCTION purchasing_v4.quotation_head_guard();
CREATE TRIGGER v4_quote_head_no_delete BEFORE DELETE ON purchasing_v4.quotation_head FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_quote_head_no_truncate BEFORE TRUNCATE ON purchasing_v4.quotation_head FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
DO $$ DECLARE t text; BEGIN FOREACH t IN ARRAY ARRAY['quotation_key','quotation_revision','quotation_response','quotation_upload','quotation_event','quotation_intent_resolution','quotation_financial'] LOOP
 EXECUTE format('CREATE TRIGGER v4_fact_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 EXECUTE format('CREATE TRIGGER v4_fact_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 END LOOP; END $$;
GRANT SELECT ON purchasing_v4.quotation_head,purchasing_v4.quotation_revision,purchasing_v4.quotation_response,purchasing_v4.quotation_event,purchasing_v4.quotation_financial,purchasing_v4.quotation_intent_resolution,purchasing_v4.active_request_reservation TO erupted_v4_po_writer;
GRANT INSERT ON purchasing_v4.quotation_event,purchasing_v4.quotation_financial,purchasing_v4.quotation_intent_resolution TO erupted_v4_po_writer;
GRANT UPDATE ON purchasing_v4.quotation_head TO erupted_v4_po_writer;
GRANT SELECT ON purchasing_v4.quotation_delivery_eligibility TO erupted_v4_outbox_worker;
CREATE FUNCTION purchasing_v4.quotation_finance_guard() RETURNS trigger LANGUAGE plpgsql AS $$ DECLARE v jsonb; BEGIN
 SELECT snapshot INTO v FROM purchasing_v4.authorized_vendor WHERE id=NEW.vendor_authorization_id AND purchase_order_id=NEW.purchase_order_id AND kind='PO';
 IF v IS NULL OR v->'quotation'->'financial' IS DISTINCT FROM NEW.evidence OR (v->'quotation'->'financial'->>'total')::numeric<>NEW.total OR (v->'quotation'->'financial'->>'merchandise')::numeric<>NEW.merchandise OR (v->'quotation'->'financial'->>'freight')::numeric<>NEW.freight OR (v->'quotation'->'financial'->>'tax')::numeric<>NEW.tax OR (v->'quotation'->'financial'->>'discount')::numeric<>NEW.discount THEN RAISE EXCEPTION 'Quotation financial facts require exact owner-approved PO snapshot'; END IF;RETURN NEW;END $$;
CREATE TRIGGER v4_quote_finance_snapshot BEFORE INSERT ON purchasing_v4.quotation_financial FOR EACH ROW EXECUTE FUNCTION purchasing_v4.quotation_finance_guard();
''')
 op.execute('''CREATE TABLE purchasing_v4.quotation_commitment(
 request_id uuid PRIMARY KEY REFERENCES purchasing_v4.authorized_vendor(id),
 vendor_authorization_id uuid NOT NULL UNIQUE REFERENCES purchasing_v4.authorized_vendor(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION purchasing_v4.quotation_commitment_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.authorized_vendor r JOIN purchasing_v4.authorized_vendor p ON p.id=NEW.vendor_authorization_id WHERE r.id=NEW.request_id AND r.kind='REQUEST' AND p.kind='PO' AND r.supplier_id=p.supplier_id AND p.snapshot->'quotation'->>'request_id'=r.id::text) THEN RAISE EXCEPTION 'Exact original request and canonical quoted PO identity required'; END IF;RETURN NEW;END $$;
CREATE TRIGGER v4_quote_commitment_identity BEFORE INSERT ON purchasing_v4.quotation_commitment FOR EACH ROW EXECUTE FUNCTION purchasing_v4.quotation_commitment_guard();
INSERT INTO purchasing_v4.quotation_commitment(request_id,vendor_authorization_id) SELECT (snapshot->'quotation'->>'request_id')::uuid,id FROM purchasing_v4.authorized_vendor WHERE kind='PO' AND snapshot ? 'quotation';
CREATE TRIGGER v4_fact_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.quotation_commitment FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_fact_no_truncate BEFORE TRUNCATE ON purchasing_v4.quotation_commitment FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
GRANT SELECT,INSERT ON purchasing_v4.quotation_commitment TO erupted_v4_po_writer;
CREATE INDEX v4_quote_event_history ON purchasing_v4.quotation_event(request_id,created_at);
CREATE INDEX v4_quote_resolution_request ON purchasing_v4.quotation_intent_resolution(source_request_id);
''')
 op.execute("""
CREATE TRIGGER v4_quote_event_owner BEFORE INSERT ON purchasing_v4.quotation_event FOR EACH ROW EXECUTE FUNCTION purchasing_v4.authorization_owner_guard();
CREATE TRIGGER v4_quote_revision_owner BEFORE INSERT ON purchasing_v4.quotation_revision FOR EACH ROW EXECUTE FUNCTION purchasing_v4.authorization_owner_guard();
CREATE TRIGGER v4_quote_upload_owner BEFORE INSERT ON purchasing_v4.quotation_upload FOR EACH ROW EXECUTE FUNCTION purchasing_v4.authorization_owner_guard();
CREATE FUNCTION purchasing_v4.quotation_consumption_guard() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE origin uuid; rowid uuid; storeid bigint; child jsonb; approved bigint; BEGIN
 SELECT i.vendor_authorization_id,i.row_id,i.store_id INTO origin,rowid,storeid FROM purchasing_v4.purchasing_intent i JOIN purchasing_v4.authorized_vendor v ON v.id=i.vendor_authorization_id WHERE i.id=NEW.intent_id AND v.kind='REQUEST';
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.quotation_commitment WHERE request_id=origin AND vendor_authorization_id=NEW.vendor_authorization_id) THEN RAISE EXCEPTION 'A request has exactly one canonical quoted commitment'; END IF;
 SELECT snapshot INTO child FROM purchasing_v4.authorized_vendor WHERE id=NEW.vendor_authorization_id AND kind='PO';
 IF child IS NULL OR child->'quotation'->>'request_id' IS DISTINCT FROM origin::text OR NOT EXISTS(SELECT 1 FROM purchasing_v4.quotation_head WHERE request_id=origin AND state='COMMITTED') THEN RAISE EXCEPTION 'Request intent requires exact final quotation authorization'; END IF;
 SELECT (line->'units'->>((s.n-1)::integer))::bigint INTO approved FROM jsonb_array_elements(child->'lines') line,jsonb_array_elements(child->'stores') WITH ORDINALITY s(store,n) WHERE line->>'row_id'=rowid::text AND (s.store->>'id')::bigint=storeid;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.quotation_intent_resolution r WHERE r.intent_id=NEW.intent_id AND r.source_request_id=origin AND r.vendor_authorization_id=NEW.vendor_authorization_id AND r.approved_units=coalesce(approved,0)) THEN RAISE EXCEPTION 'Exact original request resolution and approved store units are required'; END IF;
 RETURN NULL; END $$;
CREATE CONSTRAINT TRIGGER v4_quote_consumed_once AFTER INSERT ON purchasing_v4.intent_consumption DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.quotation_consumption_guard();
""")
 c.execute(text('INSERT INTO purchasing_v4.quotation_key(secret) VALUES (:secret)'),{'secret':secrets.token_bytes(32)})
 old=c.execute(text("SELECT pg_get_functiondef('purchasing_v4.supply_write_guard()'::regprocedure)")).scalar_one()
 op.execute(old.replace('FROM purchasing_v4.request_reservation r','FROM purchasing_v4.active_request_reservation r').replace('AND i.units>0','AND r.units>0'))


def downgrade():raise RuntimeError('Retain quotation evidence and authorizations; disable new authority and recover forward')
