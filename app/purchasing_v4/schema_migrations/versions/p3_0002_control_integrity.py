"""Canonical control identities and deferred global supplier relationship integrity."""
from alembic import op
revision='p3_0002'
down_revision='p3_0001'
branch_labels=None
depends_on=None


def upgrade():
    from app.purchasing_v4.isolation import require_isolated
    require_isolated(op.get_bind())
    op.execute('''
CREATE OR REPLACE FUNCTION purchasing_v4.validate_control_reference() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v uuid; s uuid; store bigint; BEGIN
 IF NEW.payload=jsonb_build_object('_removed',true) THEN RETURN NEW; END IF;
 IF NEW.kind IN ('PRODUCT','ASSIGNMENT','MANUAL','OFFER') THEN
  v=(NEW.payload->>'variation_id')::uuid;
  IF v IS NULL OR NOT EXISTS(SELECT 1 FROM purchasing_v4.variation WHERE id=v) THEN RAISE EXCEPTION 'Unknown internal variation'; END IF;
 END IF;
 IF NEW.kind IN ('PRODUCT','ASSIGNMENT') AND NEW.record_key<>v::text THEN RAISE EXCEPTION 'One global canonical record per variation'; END IF;
 IF NEW.kind IN ('OFFER','SUPPLIER') OR (NEW.kind='ASSIGNMENT' AND NEW.payload->>'supplier_id' IS NOT NULL) THEN
  s=(NEW.payload->>'supplier_id')::uuid;
  IF s IS NULL OR NOT EXISTS(SELECT 1 FROM purchasing_v4.authority_supplier WHERE id=s) THEN RAISE EXCEPTION 'Unknown internal supplier'; END IF;
 END IF;
 IF NEW.kind='SUPPLIER' AND NEW.record_key<>s::text THEN RAISE EXCEPTION 'Canonical supplier identity required'; END IF;
 IF NEW.kind='OFFER' AND NEW.record_key<>v::text||':'||s::text THEN RAISE EXCEPTION 'Canonical product-supplier offer required'; END IF;
 IF NEW.kind='GLOBAL' AND NEW.record_key<>'global' THEN RAISE EXCEPTION 'Only one global settings record'; END IF;
 IF NEW.kind='CATEGORY' AND NEW.record_key IS DISTINCT FROM NEW.payload->>'category_id' THEN RAISE EXCEPTION 'Canonical category identity required'; END IF;
 IF NEW.kind='REVIEW' AND NOT EXISTS(SELECT 1 FROM purchasing_v4.review_evidence WHERE id=NEW.record_key) THEN RAISE EXCEPTION 'Unknown preserved review evidence'; END IF;
 IF NEW.kind='MANUAL' THEN
  store=(NEW.payload->>'store_id')::bigint;
  IF store IS NULL OR NOT EXISTS(SELECT 1 FROM public.stores WHERE id=store) OR NEW.record_key<>v::text||':'||store::text THEN RAISE EXCEPTION 'Canonical product-store policy required'; END IF;
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
CREATE FUNCTION purchasing_v4.validate_assignment_relationship() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_payload jsonb; offer jsonb; profile jsonb; selected jsonb; BEGIN
 SELECT payload INTO current_payload FROM purchasing_v4.control_head WHERE kind=NEW.kind AND record_key=NEW.record_key;
 IF NEW.kind='ASSIGNMENT' AND current_payload->>'supplier_id' IS NOT NULL THEN
  SELECT payload INTO offer FROM purchasing_v4.control_head WHERE kind='OFFER' AND record_key=NEW.record_key||':'||(current_payload->>'supplier_id');
  SELECT payload INTO profile FROM purchasing_v4.control_head WHERE kind='SUPPLIER' AND record_key=current_payload->>'supplier_id';
  IF offer IS NULL OR NOT coalesce((offer->>'approved')::boolean,false) OR profile IS NULL OR NOT coalesce((profile->>'active')::boolean,false) THEN RAISE EXCEPTION 'Default assignment needs an approved offer and active owner supplier profile'; END IF;
 ELSIF NEW.kind='OFFER' THEN
  SELECT payload INTO selected FROM purchasing_v4.control_head WHERE kind='ASSIGNMENT' AND record_key=split_part(NEW.record_key,':',1);
  IF selected->>'supplier_id'=split_part(NEW.record_key,':',2) AND NOT coalesce((current_payload->>'approved')::boolean,false) THEN RAISE EXCEPTION 'Resolve default assignment before removing its approved offer'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE CONSTRAINT TRIGGER v4_assignment_relationship AFTER INSERT OR UPDATE ON purchasing_v4.control_head
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.validate_assignment_relationship();
CREATE INDEX v4_management_categories ON purchasing_v4.management_catalog USING gin(categories);
''')


def downgrade():
    raise RuntimeError('Preserve control identities and audit records; code rollback and forward recovery only')
