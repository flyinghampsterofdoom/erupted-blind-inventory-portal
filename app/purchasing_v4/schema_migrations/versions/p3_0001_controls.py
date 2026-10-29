"""Isolated owner controls; public purchasing and financial schemas unchanged."""
from alembic import op
revision='p3_0001'
down_revision='p1_0001'
branch_labels=None
depends_on=None


def upgrade():
    from app.purchasing_v4.isolation import require_isolated
    require_isolated(op.get_bind())
    op.execute('''
CREATE TABLE purchasing_v4.authority_supplier (
 id uuid PRIMARY KEY, foundation_supplier_id uuid UNIQUE REFERENCES purchasing_v4.supplier(id),
 origin text NOT NULL CHECK(origin IN ('FOUNDATION','LOCAL')),
 provenance text NOT NULL CHECK(btrim(provenance)<>''), created_at timestamptz NOT NULL DEFAULT now(),
 CHECK((origin='FOUNDATION' AND foundation_supplier_id=id) OR (origin='LOCAL' AND foundation_supplier_id IS NULL))
);
INSERT INTO purchasing_v4.authority_supplier(id,foundation_supplier_id,origin,provenance)
 SELECT id,id,'FOUNDATION','Accepted immutable Phase1 supplier identity' FROM purchasing_v4.supplier;
CREATE TABLE purchasing_v4.control_command (
 id uuid PRIMARY KEY, request_digest char(64) NOT NULL CHECK(request_digest ~ '^[a-f0-9]{64}$'),
 actor_id bigint NOT NULL REFERENCES public.principals(id), reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 2000),
 action text NOT NULL, result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.control_head (
 kind text NOT NULL CHECK(kind IN ('GLOBAL','CATEGORY','PRODUCT','MANUAL','SUPPLIER','OFFER','ASSIGNMENT','REVIEW')),
 record_key text NOT NULL CHECK(length(record_key) BETWEEN 1 AND 200), version bigint NOT NULL CHECK(version>0),
 payload jsonb NOT NULL, last_command uuid NOT NULL REFERENCES purchasing_v4.control_command(id),
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(kind,record_key)
);
CREATE TABLE purchasing_v4.control_revision (
 kind text NOT NULL, record_key text NOT NULL, version bigint NOT NULL CHECK(version>0),
 before_payload jsonb, payload jsonb NOT NULL, command_id uuid NOT NULL REFERENCES purchasing_v4.control_command(id),
 actor_id bigint NOT NULL REFERENCES public.principals(id), reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(kind,record_key,version), UNIQUE(command_id,kind,record_key)
);
CREATE INDEX v4_controls_manual_store ON purchasing_v4.control_head((payload->>'store_id'),record_key) WHERE kind='MANUAL';
CREATE INDEX v4_controls_offer_product ON purchasing_v4.control_head((payload->>'variation_id'),record_key) WHERE kind='OFFER';
CREATE INDEX v4_controls_supplier_assignment ON purchasing_v4.control_head((payload->>'supplier_id'),record_key) WHERE kind='ASSIGNMENT';
CREATE TABLE purchasing_v4.management_catalog (
 variation_id uuid PRIMARY KEY REFERENCES purchasing_v4.variation(id), square_variation_id text NOT NULL,
 source_namespace text NOT NULL, label text NOT NULL, sku text, categories jsonb NOT NULL,
 source_digest char(64) NOT NULL, search_vector tsvector NOT NULL, observed_at timestamptz NOT NULL
);
CREATE INDEX v4_management_search ON purchasing_v4.management_catalog USING gin(search_vector);
CREATE TABLE purchasing_v4.review_evidence (
 id char(64) PRIMARY KEY, category text NOT NULL, payload jsonb NOT NULL, source_digest char(64) NOT NULL,
 observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_review_category ON purchasing_v4.review_evidence(category,id);
CREATE TABLE purchasing_v4.history_check (
 id uuid PRIMARY KEY, actor_id bigint NOT NULL REFERENCES public.principals(id), configuration_digest char(64) NOT NULL,
 input_digest char(64) NOT NULL, rows jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION purchasing_v4.validate_control_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v uuid; s uuid; BEGIN
 IF NEW.payload = jsonb_build_object('_removed',true) THEN RETURN NEW; END IF;
 IF NEW.kind IN ('PRODUCT','ASSIGNMENT','MANUAL','OFFER') THEN
   v=(NEW.payload->>'variation_id')::uuid;
   IF v IS NULL OR NOT EXISTS(SELECT 1 FROM purchasing_v4.variation WHERE id=v) THEN RAISE EXCEPTION 'Unknown internal variation'; END IF;
 END IF;
 IF NEW.kind IN ('OFFER','ASSIGNMENT','SUPPLIER') THEN
   s=(NEW.payload->>'supplier_id')::uuid;
   IF s IS NULL OR NOT EXISTS(SELECT 1 FROM purchasing_v4.authority_supplier WHERE id=s) THEN RAISE EXCEPTION 'Unknown internal supplier'; END IF;
 END IF;
 IF NEW.kind='MANUAL' THEN
   IF NOT EXISTS(SELECT 1 FROM public.stores WHERE id=(NEW.payload->>'store_id')::bigint) THEN RAISE EXCEPTION 'Unknown store'; END IF;
   IF NEW.payload->>'state' IS NULL OR NEW.payload->>'state' NOT IN ('ACTIVE','INACTIVE','AUTOMATIC') THEN RAISE EXCEPTION 'Explicit manual policy state required'; END IF;
   IF NEW.payload->>'state'='ACTIVE' THEN
     IF coalesce(jsonb_typeof(NEW.payload->'par_units'),'null')<>'number' OR coalesce(jsonb_typeof(NEW.payload->'level_units'),'null')<>'number' THEN RAISE EXCEPTION 'Active manual requires known units'; END IF;
     IF (NEW.payload->>'par_units')::numeric<0 OR (NEW.payload->>'par_units')::numeric >= (NEW.payload->>'level_units')::numeric
       OR (NEW.payload->>'par_units')::numeric<>trunc((NEW.payload->>'par_units')::numeric)
       OR (NEW.payload->>'level_units')::numeric<>trunc((NEW.payload->>'level_units')::numeric) THEN RAISE EXCEPTION 'Active manual requires integral 0<=PAR<LEVEL'; END IF;
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER v4_control_references BEFORE INSERT OR UPDATE ON purchasing_v4.control_head FOR EACH ROW EXECUTE FUNCTION purchasing_v4.validate_control_reference();
CREATE FUNCTION purchasing_v4.control_revision_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Retain control heads; append an explicit removal revision'; END IF;
 IF TG_OP='UPDATE' AND (NEW.kind<>OLD.kind OR NEW.record_key<>OLD.record_key OR NEW.version<>OLD.version+1) THEN RAISE EXCEPTION 'Control update must advance exactly one version'; END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.control_revision r WHERE r.kind=NEW.kind AND r.record_key=NEW.record_key AND r.version=NEW.version
   AND r.payload=NEW.payload AND r.command_id=NEW.last_command) THEN RAISE EXCEPTION 'Matching immutable audit revision required'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER v4_control_audit BEFORE INSERT OR UPDATE OR DELETE ON purchasing_v4.control_head FOR EACH ROW EXECUTE FUNCTION purchasing_v4.control_revision_guard();
CREATE TRIGGER v4_control_no_truncate BEFORE TRUNCATE ON purchasing_v4.control_head FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['authority_supplier','control_command','control_revision','review_evidence','history_check'] LOOP
  EXECUTE format('CREATE TRIGGER v4_control_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',name);
  EXECUTE format('CREATE TRIGGER v4_control_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',name);
 END LOOP;
END $$;
''')


def downgrade():
    raise RuntimeError('Retain policy and audit history; code rollback and forward recovery only')
