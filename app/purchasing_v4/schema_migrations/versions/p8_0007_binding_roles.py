"""Check correspondence bindings only for the new outbox command category."""
from alembic import op
revision='p8_0007'
down_revision='p8_0006'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('''CREATE OR REPLACE FUNCTION purchasing_v4.mail_outbox_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.kind<>'PURCHASING_EMAIL' THEN RETURN NEW; END IF;
 IF NOT EXISTS(SELECT 1 FROM purchasing_v4.mail_delivery d WHERE d.id=NEW.id AND d.vendor_authorization_id=NEW.vendor_authorization_id AND d.payload_digest=NEW.payload_digest AND d.envelope=NEW.payload) THEN RAISE EXCEPTION 'Canonical purchasing email requires its exact immutable correspondence binding'; END IF;
 RETURN NEW; END $$;''')

def downgrade():raise RuntimeError('Retain binding integrity and recover forward')
