"""Isolated, append-only identity/provenance foundation; no purchasing writers.

Revision ID: p1_0001
Revises: none (independent purchasing_v4 schema)
"""
from alembic import op

revision = 'p1_0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    from app.purchasing_v4.isolation import require_isolated
    require_isolated(op.get_bind())
    op.execute('''
CREATE TABLE purchasing_v4.import_run (
 id uuid PRIMARY KEY, source_namespace text NOT NULL CHECK (btrim(source_namespace) <> ''),
 source_digest varchar(64) NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
 transform_version text NOT NULL, plan_digest varchar(64) NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
 summary jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(source_namespace, source_digest, transform_version)
);
CREATE TABLE purchasing_v4.product (
 id uuid PRIMARY KEY, created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.variation (
 id uuid PRIMARY KEY, product_id uuid REFERENCES purchasing_v4.product(id),
 created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_variation_product ON purchasing_v4.variation(product_id);
CREATE TABLE purchasing_v4.product_reference (
 source_namespace text NOT NULL, square_item_id text NOT NULL CHECK (btrim(square_item_id) <> ''),
 product_id uuid NOT NULL REFERENCES purchasing_v4.product(id),
 created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 PRIMARY KEY(source_namespace, square_item_id), UNIQUE(source_namespace, product_id)
);
CREATE TABLE purchasing_v4.variation_reference (
 source_namespace text NOT NULL, square_variation_id text NOT NULL CHECK (btrim(square_variation_id) <> ''),
 variation_id uuid NOT NULL REFERENCES purchasing_v4.variation(id),
 created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 PRIMARY KEY(source_namespace, square_variation_id), UNIQUE(source_namespace, variation_id)
);
CREATE TABLE purchasing_v4.supplier (
 id uuid PRIMARY KEY, created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.supplier_reference (
 source_namespace text NOT NULL, legacy_vendor_id bigint NOT NULL CHECK (legacy_vendor_id > 0),
 square_vendor_id text CHECK (square_vendor_id IS NULL OR btrim(square_vendor_id) <> ''),
 supplier_id uuid NOT NULL REFERENCES purchasing_v4.supplier(id),
 created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 PRIMARY KEY(source_namespace, legacy_vendor_id), UNIQUE(source_namespace, square_vendor_id),
 UNIQUE(source_namespace, supplier_id)
);
CREATE TABLE purchasing_v4.source_observation (
 run_id uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 source_table text NOT NULL, source_key text NOT NULL,
 source_digest varchar(64) NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
 evidence jsonb NOT NULL, PRIMARY KEY(run_id, source_table, source_key)
);
CREATE TABLE purchasing_v4.identity_exception (
 run_id uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 code text NOT NULL, source_table text NOT NULL, source_key text NOT NULL,
 details jsonb NOT NULL, PRIMARY KEY(run_id, code, source_table, source_key)
);
CREATE TABLE purchasing_v4.supplier_candidate (
 run_id uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 source_config_id bigint NOT NULL, variation_id uuid REFERENCES purchasing_v4.variation(id),
 supplier_id uuid REFERENCES purchasing_v4.supplier(id), explicit_default boolean NOT NULL,
 disposition text NOT NULL CHECK (disposition IN ('UNAMBIGUOUS_EVIDENCE','REVIEW_REQUIRED')),
 PRIMARY KEY(run_id, source_config_id)
);
COMMENT ON TABLE purchasing_v4.supplier_candidate IS
 'Historical mapping evidence only; not an active or approved supplier assignment';
CREATE TABLE purchasing_v4.legacy_procurement_reference (
 source_namespace text NOT NULL, legacy_po_id bigint NOT NULL CHECK (legacy_po_id > 0),
 original_digest varchar(64) NOT NULL CHECK (original_digest ~ '^[0-9a-f]{64}$'),
 created_by_run uuid NOT NULL REFERENCES purchasing_v4.import_run(id),
 PRIMARY KEY(source_namespace, legacy_po_id)
);
CREATE FUNCTION purchasing_v4.reject_fact_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'V4 foundation facts are immutable; append provenance or explicit future correction';
END $$;
DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['import_run','product','variation','product_reference',
 'variation_reference','supplier','supplier_reference','source_observation',
 'identity_exception','supplier_candidate','legacy_procurement_reference'] LOOP
  EXECUTE format('CREATE TRIGGER v4_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.%I '
                 'FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()', name);
  EXECUTE format('CREATE TRIGGER v4_no_truncate BEFORE TRUNCATE ON purchasing_v4.%I '
                 'FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()', name);
 END LOOP;
END $$;
''')


def downgrade():
    raise RuntimeError('Retain identity/provenance records; code rollback and forward recovery only')
