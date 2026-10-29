"""Isolated editable worksheets; no purchasing commitment or public-schema writer."""
from alembic import op
revision='p4_0001'
down_revision='p3_0003'
branch_labels=None
depends_on=None

def upgrade():
    from app.purchasing_v4.isolation import require_isolated
    require_isolated(op.get_bind())
    op.execute('''
CREATE TABLE purchasing_v4.worksheet_generation (
 id uuid PRIMARY KEY, owner_id bigint NOT NULL REFERENCES public.principals(id),
 configuration_digest text NOT NULL, input_digest text NOT NULL, as_of timestamptz NOT NULL,
 evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE purchasing_v4.worksheet (
 id uuid PRIMARY KEY, owner_id bigint NOT NULL REFERENCES public.principals(id),
 generation_id uuid REFERENCES purchasing_v4.worksheet_generation(id), title text NOT NULL,
 version bigint NOT NULL DEFAULT 0 CHECK(version>=0), state text NOT NULL CHECK(state IN ('EDITABLE','DISCARDED')),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX v4_worksheet_owner ON purchasing_v4.worksheet(owner_id,state,updated_at DESC,id);
CREATE TABLE purchasing_v4.worksheet_row (
 session_id uuid NOT NULL REFERENCES purchasing_v4.worksheet(id), id uuid NOT NULL,
 variation_id uuid NOT NULL REFERENCES purchasing_v4.variation(id), position bigint NOT NULL,
 original jsonb NOT NULL, working jsonb NOT NULL, PRIMARY KEY(session_id,id), UNIQUE(session_id,variation_id)
);
CREATE INDEX v4_worksheet_rows_order ON purchasing_v4.worksheet_row(session_id,position,id);
CREATE TABLE purchasing_v4.worksheet_command (
 session_id uuid NOT NULL REFERENCES purchasing_v4.worksheet(id), id uuid NOT NULL,
 actor_id bigint NOT NULL REFERENCES public.principals(id), request_digest char(64) NOT NULL,
 before_version bigint NOT NULL, after_version bigint NOT NULL, changes jsonb NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(session_id,id), UNIQUE(session_id,after_version)
);
CREATE TABLE purchasing_v4.worksheet_preference (
 owner_id bigint NOT NULL REFERENCES public.principals(id), workspace text NOT NULL,
 version bigint NOT NULL, value jsonb NOT NULL, PRIMARY KEY(owner_id,workspace)
);
CREATE FUNCTION purchasing_v4.worksheet_row_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.id<>OLD.id OR NEW.session_id<>OLD.session_id OR NEW.variation_id<>OLD.variation_id OR NEW.original<>OLD.original OR NEW.position<>OLD.position
 THEN RAISE EXCEPTION 'Worksheet identity and original advice are immutable'; END IF; RETURN NEW; END $$;
CREATE TRIGGER v4_worksheet_identity BEFORE UPDATE ON purchasing_v4.worksheet_row FOR EACH ROW EXECUTE FUNCTION purchasing_v4.worksheet_row_identity_guard();
CREATE TRIGGER v4_worksheet_generation_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.worksheet_generation FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_worksheet_command_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.worksheet_command FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_worksheet_generation_no_truncate BEFORE TRUNCATE ON purchasing_v4.worksheet_generation FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_worksheet_command_no_truncate BEFORE TRUNCATE ON purchasing_v4.worksheet_command FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_worksheet_row_no_delete BEFORE DELETE ON purchasing_v4.worksheet_row FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
''')

def downgrade():raise RuntimeError('Retain worksheets and receipts; rollback code, recover forward')
