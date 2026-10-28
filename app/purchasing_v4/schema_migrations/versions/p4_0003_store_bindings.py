"""Pin allocation identity/order; a later store activation or rename cannot reinterpret packs."""
from alembic import op
revision='p4_0003'
down_revision='p4_0002'
branch_labels=None
depends_on=None

def upgrade():
 from app.purchasing_v4.isolation import require_isolated
 require_isolated(op.get_bind())
 op.execute('''ALTER TABLE purchasing_v4.worksheet ADD COLUMN stores jsonb;
 -- Earlier isolated prototypes indexed by active store ID order. Preserve that exact interpretation.
 DO $$ BEGIN IF EXISTS(SELECT 1 FROM purchasing_v4.worksheet) AND (SELECT count(*) FROM public.stores WHERE active)<>4 THEN RAISE EXCEPTION 'Cannot prove prototype store bindings; review before migration'; END IF; END $$;
 UPDATE purchasing_v4.worksheet SET stores=(SELECT jsonb_agg(jsonb_build_object('id',id,'name',name) ORDER BY id) FROM public.stores WHERE active);
 ALTER TABLE purchasing_v4.worksheet ALTER COLUMN stores SET NOT NULL;
 ALTER TABLE purchasing_v4.worksheet ADD CONSTRAINT v4_worksheet_four_stores CHECK(jsonb_typeof(stores)='array' AND jsonb_array_length(stores)=4);
 CREATE FUNCTION purchasing_v4.worksheet_aggregate_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.id<>OLD.id OR NEW.owner_id<>OLD.owner_id OR NEW.generation_id IS DISTINCT FROM OLD.generation_id OR NEW.stores<>OLD.stores THEN RAISE EXCEPTION 'Worksheet owner, generation and store bindings are immutable'; END IF; RETURN NEW; END $$;
 CREATE TRIGGER v4_worksheet_aggregate_identity BEFORE UPDATE ON purchasing_v4.worksheet FOR EACH ROW EXECUTE FUNCTION purchasing_v4.worksheet_aggregate_identity_guard();''')

def downgrade():raise RuntimeError('Retain original allocation identities; recover forward')
