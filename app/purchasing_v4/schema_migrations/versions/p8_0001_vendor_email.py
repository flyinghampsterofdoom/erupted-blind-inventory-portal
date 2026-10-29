"""Private vendor email evidence, durable processing and permanent retention."""
from alembic import op
revision='p8_0001'
down_revision='p7_0008'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('''
 CREATE TABLE purchasing_v4.mail_delivery(
 id uuid PRIMARY KEY,vendor_authorization_id uuid NOT NULL REFERENCES purchasing_v4.authorized_vendor(id),
 revision_id uuid REFERENCES purchasing_v4.quotation_revision(id),owner_id integer NOT NULL REFERENCES public.principals(id),
 envelope jsonb NOT NULL,payload_digest char(64) NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
 CREATE UNIQUE INDEX mail_delivery_one_revision ON purchasing_v4.mail_delivery(vendor_authorization_id,coalesce(revision_id,'00000000-0000-0000-0000-000000000000'::uuid));
 CREATE TABLE purchasing_v4.mail_state(
 delivery_id uuid PRIMARY KEY REFERENCES purchasing_v4.mail_delivery(id),state text NOT NULL DEFAULT 'NOT_SENT'
 CHECK(state IN ('NOT_SENT','SENDING','ACCEPTED','DELIVERED','FAILED','UNCERTAIN','HELD')),
 version bigint NOT NULL DEFAULT 0,lease_id uuid,first_attempt_at timestamptz,provider_id text UNIQUE,
 evidence jsonb NOT NULL DEFAULT '{}',updated_at timestamptz NOT NULL DEFAULT now());
 CREATE TABLE purchasing_v4.mail_attempt(id uuid PRIMARY KEY,delivery_id uuid NOT NULL REFERENCES purchasing_v4.mail_delivery(id),kind text NOT NULL,outcome text NOT NULL,evidence jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
 CREATE INDEX mail_attempt_delivery ON purchasing_v4.mail_attempt(delivery_id,created_at);
 CREATE INDEX mail_state_work ON purchasing_v4.mail_state(state,updated_at);
 CREATE TABLE purchasing_v4.mail_event(id text PRIMARY KEY,payload_digest char(64) NOT NULL,body bytea NOT NULL,payload jsonb NOT NULL,received_at timestamptz NOT NULL DEFAULT now());
 CREATE TABLE purchasing_v4.mail_event_state(event_id text PRIMARY KEY REFERENCES purchasing_v4.mail_event(id),state text NOT NULL DEFAULT 'PENDING' CHECK(state IN ('PENDING','PROCESSING','DONE','FAILED')),version bigint NOT NULL DEFAULT 0,evidence jsonb NOT NULL DEFAULT '{}',updated_at timestamptz NOT NULL DEFAULT now());
 CREATE INDEX mail_event_work ON purchasing_v4.mail_event_state(state,updated_at);
 CREATE TABLE purchasing_v4.mail_message(provider_id text PRIMARY KEY,event_id text NOT NULL REFERENCES purchasing_v4.mail_event(id),metadata jsonb NOT NULL,original bytea NOT NULL,sha256 char(64) NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
 CREATE TABLE purchasing_v4.mail_attachment(id uuid PRIMARY KEY,provider_message_id text NOT NULL REFERENCES purchasing_v4.mail_message(provider_id),provider_attachment_id text NOT NULL,filename text NOT NULL,metadata jsonb NOT NULL,content bytea,sha256 char(64),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(provider_message_id,provider_attachment_id));
 CREATE TABLE purchasing_v4.mail_import(attachment_id uuid PRIMARY KEY REFERENCES purchasing_v4.mail_attachment(id),request_id uuid REFERENCES purchasing_v4.authorized_vendor(id),result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
 CREATE TABLE purchasing_v4.mail_resolution(id uuid PRIMARY KEY,owner_id integer NOT NULL REFERENCES public.principals(id),subject text NOT NULL,reason text NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
 DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['mail_delivery','mail_attempt','mail_event','mail_message','mail_attachment','mail_import','mail_resolution'] LOOP
 EXECUTE format('CREATE TRIGGER immutable_fact BEFORE UPDATE OR DELETE ON purchasing_v4.%I FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t);
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='erupted_v4_mail_worker') THEN CREATE ROLE erupted_v4_mail_worker NOLOGIN; END IF;
 END $$;
 GRANT USAGE ON SCHEMA purchasing_v4 TO erupted_v4_mail_worker;
 GRANT SELECT,INSERT ON purchasing_v4.mail_attempt,purchasing_v4.mail_message,purchasing_v4.mail_attachment,purchasing_v4.mail_import TO erupted_v4_mail_worker;
 GRANT SELECT ON purchasing_v4.mail_delivery,purchasing_v4.mail_event TO erupted_v4_mail_worker;
 GRANT SELECT,UPDATE ON purchasing_v4.mail_state,purchasing_v4.mail_event_state TO erupted_v4_mail_worker;
 ''')

def downgrade():raise RuntimeError('Correspondence and purchasing evidence are retained indefinitely; disable integration and recover forward')
