"""Restricted owner identity and normalized inbound address provenance."""
from alembic import op
revision='p8_0003'
down_revision='p8_0002'
branch_labels=None
depends_on=None

def upgrade():
 op.execute('''CREATE VIEW purchasing_v4.mail_owner WITH(security_barrier=true) AS SELECT p.id FROM public.principals p JOIN purchasing_v4.commitment_owner o ON o.principal_id=p.id WHERE p.active AND p.role='ADMIN';
 GRANT SELECT ON purchasing_v4.mail_owner TO erupted_v4_mail_worker;
 CREATE OR REPLACE VIEW purchasing_v4.mail_intake_authority WITH(security_barrier=true) AS
 SELECT a.id AS attachment_id,d.vendor_authorization_id AS request_id,d.owner_id
 FROM purchasing_v4.mail_attachment a JOIN purchasing_v4.mail_message m ON m.provider_id=a.provider_message_id
 JOIN purchasing_v4.mail_delivery d ON d.revision_id IS NOT NULL
 WHERE lower(coalesce(substring(m.metadata->>'from' from '<([^<>]+)>'),m.metadata->>'from'))=d.envelope->>'recipient'
 AND m.metadata->'authentication'->>'dmarc'='pass'
 AND EXISTS(SELECT 1 FROM jsonb_array_elements_text(m.metadata->'to') recipient WHERE lower(coalesce(substring(recipient from '<([^<>]+)>'),recipient))=d.envelope->>'reply_to');''')

def downgrade():raise RuntimeError('Retain correspondence and recover forward')
