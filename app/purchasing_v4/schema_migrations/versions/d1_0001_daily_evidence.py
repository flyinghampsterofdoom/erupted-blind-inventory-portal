"""Add isolated collector provenance; retained legacy financial tables unchanged."""
from alembic import op
revision='d1_0001'
down_revision='p9_0001'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''
    ALTER TABLE public.ordering_inventory_refresh_runs ALTER COLUMN refreshed_by_principal_id DROP NOT NULL;
    CREATE TABLE purchasing_v4.collection_attempt (
      id uuid PRIMARY KEY, command_key text NOT NULL, reason text NOT NULL CHECK(reason IN ('DAILY','MANUAL','RECOVERY')),
      started_at timestamptz NOT NULL, completed_at timestamptz NOT NULL,
      status text NOT NULL CHECK(status IN ('COMPLETE','PARTIAL','FAILED')),
      manifest jsonb NOT NULL, error_code text,
      CHECK(completed_at>=started_at));
    CREATE UNIQUE INDEX collection_success_command ON purchasing_v4.collection_attempt(command_key) WHERE status='COMPLETE';
    CREATE TABLE purchasing_v4.collection_page (
      attempt_id uuid REFERENCES purchasing_v4.collection_attempt(id), ordinal integer NOT NULL,
      digest char(64) NOT NULL,payload jsonb NOT NULL, PRIMARY KEY(attempt_id,ordinal));
    CREATE TABLE purchasing_v4.inventory_checkpoint (
      attempt_id uuid REFERENCES purchasing_v4.collection_attempt(id), variation_id text NOT NULL,
      store_id bigint NOT NULL, business_day date NOT NULL, observed_at timestamptz NOT NULL,
      state text NOT NULL CHECK(state IN ('KNOWN','UNKNOWN')), quantity numeric,
      source_calculated_at timestamptz, reason text NOT NULL,
      PRIMARY KEY(attempt_id,variation_id,store_id),
      CHECK((state='KNOWN' AND quantity IS NOT NULL) OR (state='UNKNOWN' AND quantity IS NULL)));
    CREATE INDEX daily_checkpoint_pair ON purchasing_v4.inventory_checkpoint(variation_id,store_id,business_day,observed_at);
    CREATE TABLE purchasing_v4.movement_leg (
      event_type text NOT NULL,event_id text NOT NULL,location_id text NOT NULL,variation_id text NOT NULL,
      occurred_at timestamptz NOT NULL,business_date date NOT NULL,quantity_delta numeric NOT NULL,
      physical_count boolean NOT NULL,source_updated_at timestamptz NOT NULL,calculated_after_at timestamptz NOT NULL,
      calculated_before_at timestamptz NOT NULL,source_digest char(64) NOT NULL,synchronized_at timestamptz NOT NULL,
      PRIMARY KEY(event_type,event_id,location_id));
    CREATE INDEX daily_movement_scope ON purchasing_v4.movement_leg(location_id,variation_id,occurred_at);
    CREATE VIEW purchasing_v4.shared_inventory_events AS
      SELECT n.* FROM purchasing_v4.movement_leg n WHERE NOT EXISTS (
        SELECT 1 FROM public.purchasing_inventory_events e WHERE e.event_type=n.event_type AND e.event_id=n.event_id
        AND e.synchronized_at>n.synchronized_at)
      UNION ALL SELECT e.* FROM public.purchasing_inventory_events e WHERE NOT EXISTS (
        SELECT 1 FROM purchasing_v4.movement_leg n WHERE n.event_type=e.event_type AND n.event_id=e.event_id
        AND n.synchronized_at>=e.synchronized_at);
    ''')
    # Source pages/checkpoints are the immutable provenance. Legs are a replaceable read projection.
    for name in ('collection_attempt','collection_page','inventory_checkpoint'):
        op.execute(f'''CREATE TRIGGER daily_{name}_immutable BEFORE UPDATE OR DELETE ON purchasing_v4.{name}
          FOR EACH ROW EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();
          CREATE TRIGGER daily_{name}_no_truncate BEFORE TRUNCATE ON purchasing_v4.{name}
          FOR EACH STATEMENT EXECUTE FUNCTION purchasing_v4.reject_fact_mutation();''')


def downgrade():
    raise RuntimeError('Collector evidence retained: disable runner, roll back code, recover forward; never delete checkpoints')
