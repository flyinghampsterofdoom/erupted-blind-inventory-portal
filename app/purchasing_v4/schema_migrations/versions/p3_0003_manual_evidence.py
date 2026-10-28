"""Retain observed legacy manual intent independently of later legacy engine changes."""
from alembic import op
revision='p3_0003'
down_revision='p3_0002'
branch_labels=None
depends_on=None


def upgrade():
    from app.purchasing_v4.isolation import require_isolated
    require_isolated(op.get_bind())
    op.execute('''
CREATE TABLE purchasing_v4.legacy_manual_evidence (
 source_namespace text NOT NULL, legacy_id bigint NOT NULL, store_id bigint,
 mapped_variations jsonb NOT NULL, scope_variations jsonb NOT NULL,
 observation jsonb NOT NULL, original_record jsonb NOT NULL,
 source_digest char(64) NOT NULL CHECK(source_digest ~ '^[0-9a-f]{64}$'),
 captured_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(source_namespace,legacy_id)
);
CREATE INDEX v4_frozen_manual_scope ON purchasing_v4.legacy_manual_evidence USING gin(scope_variations);
CREATE TRIGGER v4_frozen_manual_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.legacy_manual_evidence
 FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
CREATE TRIGGER v4_frozen_manual_no_truncate BEFORE TRUNCATE ON purchasing_v4.legacy_manual_evidence
 FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
''')


def downgrade():
    raise RuntimeError('Retain original manual policy evidence; recover forward without expiring rules')
