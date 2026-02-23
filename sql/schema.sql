-- PostgreSQL schema for Blind Inventory Portal

CREATE EXTENSION IF NOT EXISTS citext;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'principal_role') THEN
    CREATE TYPE principal_role AS ENUM ('ADMIN', 'MANAGER', 'LEAD', 'STORE');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'session_status') THEN
    CREATE TYPE session_status AS ENUM ('DRAFT', 'SUBMITTED');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'snapshot_section_type') THEN
    CREATE TYPE snapshot_section_type AS ENUM ('CATEGORY', 'RECOUNT');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'opening_checklist_item_type') THEN
    CREATE TYPE opening_checklist_item_type AS ENUM ('PARENT', 'SUB');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'checklist_answer_value') THEN
    CREATE TYPE checklist_answer_value AS ENUM ('Y', 'N', 'NA');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'checklist_notes_type') THEN
    CREATE TYPE checklist_notes_type AS ENUM ('NONE', 'ISSUE', 'MAINTENANCE', 'SUPPLY', 'FOLLOW_UP', 'OTHER');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'daily_chore_sheet_status') THEN
    CREATE TYPE daily_chore_sheet_status AS ENUM ('DRAFT', 'SUBMITTED');
  END IF;
END;
$$;

ALTER TYPE principal_role ADD VALUE IF NOT EXISTS 'ADMIN';
ALTER TYPE principal_role ADD VALUE IF NOT EXISTS 'LEAD';

CREATE TABLE IF NOT EXISTS stores (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  square_location_id TEXT UNIQUE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS principals (
  id BIGSERIAL PRIMARY KEY,
  username CITEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  role principal_role NOT NULL,
  store_id BIGINT REFERENCES stores(id),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT principals_store_role_ck CHECK (
    (role = 'STORE' AND store_id IS NOT NULL) OR
    (role IN ('ADMIN', 'MANAGER', 'LEAD') AND store_id IS NULL)
  )
);
ALTER TABLE principals DROP CONSTRAINT IF EXISTS principals_store_role_ck;
ALTER TABLE principals
  ADD CONSTRAINT principals_store_role_ck CHECK (
    (role = 'STORE' AND store_id IS NOT NULL) OR
    (role IN ('ADMIN', 'MANAGER', 'LEAD') AND store_id IS NULL)
  );

CREATE TABLE IF NOT EXISTS campaigns (
  id BIGSERIAL PRIMARY KEY,
  label TEXT NOT NULL,
  category_filter TEXT,
  brand_filter TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS count_groups (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  position INTEGER NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS count_group_campaigns (
  group_id BIGINT NOT NULL REFERENCES count_groups(id) ON DELETE CASCADE,
  campaign_id BIGINT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (group_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS count_sessions (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id),
  campaign_id BIGINT NOT NULL REFERENCES campaigns(id),
  employee_name TEXT NOT NULL,
  status session_status NOT NULL DEFAULT 'DRAFT',
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  submitted_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  submitted_at TIMESTAMPTZ
);

ALTER TABLE count_sessions ADD COLUMN IF NOT EXISTS count_group_id BIGINT REFERENCES count_groups(id);
ALTER TABLE count_sessions ADD COLUMN IF NOT EXISTS source_forced_count_id BIGINT;
ALTER TABLE count_sessions ADD COLUMN IF NOT EXISTS includes_recount BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE count_sessions ADD COLUMN IF NOT EXISTS submit_inventory_fetched_at TIMESTAMPTZ;
ALTER TABLE count_sessions ADD COLUMN IF NOT EXISTS variance_signature VARCHAR(128);
ALTER TABLE count_sessions ADD COLUMN IF NOT EXISTS stable_variance BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS store_forced_counts (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  campaign_id BIGINT REFERENCES campaigns(id),
  count_group_id BIGINT REFERENCES count_groups(id),
  source_session_id BIGINT REFERENCES count_sessions(id),
  reason TEXT NOT NULL,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  consumed_at TIMESTAMPTZ,
  active BOOLEAN NOT NULL DEFAULT TRUE
);
ALTER TABLE store_forced_counts ADD COLUMN IF NOT EXISTS count_group_id BIGINT REFERENCES count_groups(id);
ALTER TABLE store_forced_counts ALTER COLUMN campaign_id DROP NOT NULL;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'store_forced_counts_count_group_id_fkey'
  ) THEN
    ALTER TABLE store_forced_counts
      ADD CONSTRAINT store_forced_counts_count_group_id_fkey
      FOREIGN KEY (count_group_id) REFERENCES count_groups(id);
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'count_sessions_source_forced_count_id_fkey'
  ) THEN
    ALTER TABLE count_sessions
      ADD CONSTRAINT count_sessions_source_forced_count_id_fkey
      FOREIGN KEY (source_forced_count_id) REFERENCES store_forced_counts(id);
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS snapshot_lines (
  session_id BIGINT NOT NULL REFERENCES count_sessions(id) ON DELETE CASCADE,
  variation_id TEXT NOT NULL,
  sku TEXT,
  item_name TEXT NOT NULL,
  variation_name TEXT NOT NULL,
  expected_on_hand NUMERIC(14,3) NOT NULL,
  source_catalog_version TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (session_id, variation_id)
);

ALTER TABLE snapshot_lines
  ADD COLUMN IF NOT EXISTS section_type snapshot_section_type NOT NULL DEFAULT 'CATEGORY';

CREATE TABLE IF NOT EXISTS entries (
  session_id BIGINT NOT NULL REFERENCES count_sessions(id) ON DELETE CASCADE,
  variation_id TEXT NOT NULL,
  counted_qty NUMERIC(14,3) NOT NULL,
  updated_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (session_id, variation_id),
  CONSTRAINT entries_non_negative_ck CHECK (counted_qty >= 0)
);

CREATE TABLE IF NOT EXISTS store_rotation_state (
  store_id BIGINT PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
  next_campaign_id BIGINT REFERENCES campaigns(id),
  next_group_id BIGINT REFERENCES count_groups(id),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE store_rotation_state ADD COLUMN IF NOT EXISTS next_group_id BIGINT REFERENCES count_groups(id);
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_name = 'store_rotation_state_next_group_id_fkey'
  ) THEN
    ALTER TABLE store_rotation_state
      ADD CONSTRAINT store_rotation_state_next_group_id_fkey
      FOREIGN KEY (next_group_id) REFERENCES count_groups(id);
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS store_recount_state (
  store_id BIGINT PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
  is_active BOOLEAN NOT NULL DEFAULT FALSE,
  previous_signature VARCHAR(128),
  rounds INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS store_recount_items (
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  variation_id TEXT NOT NULL,
  sku TEXT,
  item_name TEXT NOT NULL,
  variation_name TEXT NOT NULL,
  last_variance NUMERIC(14,3) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (store_id, variation_id)
);

CREATE TABLE IF NOT EXISTS auth_events (
  id BIGSERIAL PRIMARY KEY,
  attempted_username CITEXT NOT NULL,
  success BOOLEAN NOT NULL,
  failure_reason TEXT,
  principal_id BIGINT REFERENCES principals(id),
  ip INET,
  user_agent TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  actor_principal_id BIGINT REFERENCES principals(id),
  action TEXT NOT NULL,
  session_id BIGINT REFERENCES count_sessions(id),
  ip INET,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS web_sessions (
  id BIGSERIAL PRIMARY KEY,
  session_token VARCHAR(128) NOT NULL UNIQUE,
  principal_id BIGINT NOT NULL REFERENCES principals(id),
  ip INET,
  user_agent TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS opening_checklist_items (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  item_type opening_checklist_item_type NOT NULL,
  parent_item_id BIGINT REFERENCES opening_checklist_items(id),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS opening_checklist_submissions (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  submitted_by_name TEXT NOT NULL,
  lead_name TEXT,
  previous_employee TEXT,
  summary_notes_type checklist_notes_type NOT NULL DEFAULT 'NONE',
  summary_notes TEXT,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS opening_checklist_answers (
  submission_id BIGINT NOT NULL REFERENCES opening_checklist_submissions(id) ON DELETE CASCADE,
  item_id BIGINT NOT NULL REFERENCES opening_checklist_items(id),
  answer checklist_answer_value NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (submission_id, item_id)
);

CREATE TABLE IF NOT EXISTS daily_chore_tasks (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  section TEXT NOT NULL,
  prompt TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS daily_chore_sheets (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  sheet_date DATE NOT NULL,
  employee_name TEXT NOT NULL,
  status daily_chore_sheet_status NOT NULL DEFAULT 'DRAFT',
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  submitted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT daily_chore_sheets_store_date_uniq UNIQUE (store_id, sheet_date)
);

CREATE TABLE IF NOT EXISTS daily_chore_entries (
  sheet_id BIGINT NOT NULL REFERENCES daily_chore_sheets(id) ON DELETE CASCADE,
  task_id BIGINT NOT NULL REFERENCES daily_chore_tasks(id),
  completed BOOLEAN NOT NULL DEFAULT FALSE,
  completed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (sheet_id, task_id)
);

CREATE INDEX IF NOT EXISTS idx_principals_store_id ON principals(store_id);
CREATE INDEX IF NOT EXISTS idx_count_sessions_store_created ON count_sessions(store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_count_sessions_status ON count_sessions(status);
CREATE INDEX IF NOT EXISTS idx_count_sessions_group ON count_sessions(count_group_id);
CREATE INDEX IF NOT EXISTS idx_count_sessions_forced ON count_sessions(source_forced_count_id);
CREATE INDEX IF NOT EXISTS idx_count_groups_position ON count_groups(position, active);
CREATE INDEX IF NOT EXISTS idx_count_group_campaigns_group ON count_group_campaigns(group_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_lines_section ON snapshot_lines(session_id, section_type);
CREATE INDEX IF NOT EXISTS idx_store_forced_counts_active ON store_forced_counts(store_id, active, created_at);
CREATE INDEX IF NOT EXISTS idx_store_recount_items_store ON store_recount_items(store_id);
CREATE INDEX IF NOT EXISTS idx_auth_events_created ON auth_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action_created ON audit_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_web_sessions_token ON web_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_web_sessions_principal ON web_sessions(principal_id);
CREATE INDEX IF NOT EXISTS idx_opening_checklist_items_store ON opening_checklist_items(store_id, active, position);
CREATE INDEX IF NOT EXISTS idx_opening_checklist_submissions_store_date ON opening_checklist_submissions(store_id, submitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_opening_checklist_answers_submission ON opening_checklist_answers(submission_id);
CREATE INDEX IF NOT EXISTS idx_daily_chore_tasks_store ON daily_chore_tasks(store_id, active, position);
CREATE INDEX IF NOT EXISTS idx_daily_chore_sheets_store_date ON daily_chore_sheets(store_id, sheet_date DESC);
CREATE INDEX IF NOT EXISTS idx_daily_chore_entries_sheet ON daily_chore_entries(sheet_id);

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_principals_updated_at ON principals;
CREATE TRIGGER trg_principals_updated_at
BEFORE UPDATE ON principals
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_count_sessions_updated_at ON count_sessions;
CREATE TRIGGER trg_count_sessions_updated_at
BEFORE UPDATE ON count_sessions
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_store_rotation_state_updated_at ON store_rotation_state;
CREATE TRIGGER trg_store_rotation_state_updated_at
BEFORE UPDATE ON store_rotation_state
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_daily_chore_sheets_updated_at ON daily_chore_sheets;
CREATE TRIGGER trg_daily_chore_sheets_updated_at
BEFORE UPDATE ON daily_chore_sheets
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_store_recount_state_updated_at ON store_recount_state;
CREATE TRIGGER trg_store_recount_state_updated_at
BEFORE UPDATE ON store_recount_state
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_store_recount_items_updated_at ON store_recount_items;
CREATE TRIGGER trg_store_recount_items_updated_at
BEFORE UPDATE ON store_recount_items
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
