"""Bind correspondence to the existing canonical integration outbox, not a second sender."""
from alembic import op
revision='p8_0005'
down_revision='p8_0004'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('''
 GRANT erupted_v4_mail_worker TO erupted_v4_outbox_worker;
 ALTER TABLE purchasing_v4.integration_command DROP CONSTRAINT IF EXISTS integration_command_kind_check;
 ALTER TABLE purchasing_v4.integration_command ADD CONSTRAINT integration_command_kind_check CHECK(kind IN ('GENERATE_DOCUMENTS','DELIVER_DOCUMENTS','PURCHASING_EMAIL'));
 ALTER TABLE purchasing_v4.integration_command DROP CONSTRAINT IF EXISTS integration_command_vendor_authorization_id_kind_key;
 CREATE UNIQUE INDEX IF NOT EXISTS integration_original_kind_identity ON purchasing_v4.integration_command(vendor_authorization_id,kind) WHERE kind<>'PURCHASING_EMAIL';
 CREATE UNIQUE INDEX IF NOT EXISTS integration_mail_route_identity ON purchasing_v4.integration_command(vendor_authorization_id,(payload->>'reply_to')) WHERE kind='PURCHASING_EMAIL';
 INSERT INTO purchasing_v4.integration_command(id,vendor_authorization_id,kind,payload,payload_digest)
 SELECT d.id,d.vendor_authorization_id,'PURCHASING_EMAIL',d.envelope,d.payload_digest FROM purchasing_v4.mail_delivery d ON CONFLICT(id) DO NOTHING;
 INSERT INTO purchasing_v4.integration_state(command_id,state,evidence)
 SELECT s.delivery_id,CASE WHEN s.state IN ('ACCEPTED','DELIVERED') THEN 'ACKNOWLEDGED' WHEN s.state IN ('SENDING','UNCERTAIN') THEN 'AMBIGUOUS' WHEN s.state IN ('FAILED','HELD') THEN 'FAILED' ELSE 'PENDING' END,
 jsonb_build_object('source','Retained isolated Phase8 correspondence evidence','mail_state',s.state,'provider_id',s.provider_id)
 FROM purchasing_v4.mail_state s ON CONFLICT(command_id) DO NOTHING;
 ALTER TABLE purchasing_v4.mail_delivery ADD CONSTRAINT mail_canonical_outbox_identity FOREIGN KEY(id) REFERENCES purchasing_v4.integration_command(id);
 ''')

def downgrade():raise RuntimeError('Canonical delivery identities and correspondence evidence are retained; recover forward')
