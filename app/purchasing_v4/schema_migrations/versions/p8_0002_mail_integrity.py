"""Retained correspondence transitions and restricted email intake authority."""
from alembic import op
revision='p8_0002'
down_revision='p8_0001'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('''
 DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['mail_delivery','mail_attempt','mail_event','mail_message','mail_attachment','mail_import','mail_resolution'] LOOP
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=('purchasing_v4.'||t)::regclass AND tgname='no_truncate') THEN
 EXECUTE format('CREATE TRIGGER no_truncate BEFORE TRUNCATE ON purchasing_v4.%I FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation()',t); END IF;
 END LOOP; END $$;
 CREATE FUNCTION purchasing_v4.mail_transition_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.delivery_id<>OLD.delivery_id OR NEW.version<>OLD.version+1 OR (OLD.provider_id IS NOT NULL AND NEW.provider_id IS DISTINCT FROM OLD.provider_id) OR (OLD.first_attempt_at IS NOT NULL AND NEW.first_attempt_at IS DISTINCT FROM OLD.first_attempt_at) THEN RAISE EXCEPTION 'Immutable mail identity and successive version required'; END IF;
 IF NEW.state='SENDING' AND (OLD.state NOT IN ('NOT_SENT','FAILED') OR NEW.lease_id IS NULL) THEN RAISE EXCEPTION 'Uncertain or accepted mail cannot be blindly resent'; END IF;
 IF OLD.state='DELIVERED' AND NEW.state NOT IN ('DELIVERED','HELD') THEN RAISE EXCEPTION 'Confirmed delivery cannot regress to sending or acceptance'; END IF;
 IF NEW.state IN ('ACCEPTED','DELIVERED') AND NEW.provider_id IS NULL THEN RAISE EXCEPTION 'Provider message identity required'; END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.mail_attempt a WHERE a.delivery_id=NEW.delivery_id AND a.outcome=NEW.state AND a.evidence=NEW.evidence AND (a.evidence->>'state_version')::bigint=NEW.version) THEN RAISE EXCEPTION 'Immutable exact attempt evidence required'; END IF;
 RETURN NEW; END $$;
 CREATE TRIGGER mail_state_transition BEFORE UPDATE ON purchasing_v4.mail_state FOR EACH ROW EXECUTE FUNCTION purchasing_v4.mail_transition_guard();
 CREATE VIEW purchasing_v4.mail_intake_authority WITH(security_barrier=true) AS
 SELECT a.id AS attachment_id,d.vendor_authorization_id AS request_id,d.owner_id
 FROM purchasing_v4.mail_attachment a JOIN purchasing_v4.mail_message m ON m.provider_id=a.provider_message_id
 JOIN purchasing_v4.mail_delivery d ON d.revision_id IS NOT NULL
 WHERE m.metadata->>'from'=d.envelope->>'recipient' AND m.metadata->'authentication'->>'dmarc'='pass'
 AND m.metadata->'to' @> jsonb_build_array(d.envelope->>'reply_to');
 GRANT SELECT ON purchasing_v4.mail_intake_authority,purchasing_v4.quotation_head,purchasing_v4.quotation_key TO erupted_v4_mail_worker;
 GRANT SELECT(id,request_id,public_projection,signature,template,sha256) ON purchasing_v4.quotation_revision TO erupted_v4_mail_worker;
 GRANT UPDATE(version) ON purchasing_v4.quotation_head TO erupted_v4_mail_worker;
 GRANT SELECT ON purchasing_v4.commitment_owner TO erupted_v4_mail_worker;
 GRANT SELECT,INSERT ON purchasing_v4.quotation_upload,purchasing_v4.quotation_response TO erupted_v4_mail_worker;
 ''')

def downgrade():raise RuntimeError('Retain authenticated correspondence, event identities and worker permissions; recover forward')
