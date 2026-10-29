"""One canonical mail command per immutable request revision; preserve old commands."""
from alembic import op
revision='p8_0006'
down_revision='p8_0005'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('''
 ALTER TABLE purchasing_v4.integration_command DROP CONSTRAINT IF EXISTS integration_command_kind_check;
 ALTER TABLE purchasing_v4.integration_command ADD CONSTRAINT integration_command_kind_check CHECK(kind IN ('GENERATE_DOCUMENTS','DELIVER_DOCUMENTS','PURCHASING_EMAIL'));
 ALTER TABLE purchasing_v4.integration_command DROP CONSTRAINT IF EXISTS integration_command_vendor_authorization_id_kind_key;
 CREATE UNIQUE INDEX IF NOT EXISTS integration_original_kind_identity ON purchasing_v4.integration_command(vendor_authorization_id,kind) WHERE kind<>'PURCHASING_EMAIL';
 CREATE UNIQUE INDEX IF NOT EXISTS integration_mail_route_identity ON purchasing_v4.integration_command(vendor_authorization_id,(payload->>'reply_to')) WHERE kind='PURCHASING_EMAIL';
 CREATE FUNCTION purchasing_v4.mail_outbox_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.kind='PURCHASING_EMAIL' AND NOT EXISTS(SELECT 1 FROM purchasing_v4.mail_delivery d WHERE d.id=NEW.id AND d.vendor_authorization_id=NEW.vendor_authorization_id AND d.payload_digest=NEW.payload_digest AND d.envelope=NEW.payload) THEN RAISE EXCEPTION 'Canonical purchasing email requires its exact immutable correspondence binding'; END IF;
 RETURN NEW; END $$;
 CREATE CONSTRAINT TRIGGER mail_outbox_binding AFTER INSERT ON purchasing_v4.integration_command DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION purchasing_v4.mail_outbox_binding_guard();
 ''')

def downgrade():raise RuntimeError('Keep issued revision identities and original integration commands; recover forward')
