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

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'change_box_count_status') THEN
    CREATE TYPE change_box_count_status AS ENUM ('DRAFT', 'SUBMITTED');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'non_sellable_stock_take_status') THEN
    CREATE TYPE non_sellable_stock_take_status AS ENUM ('DRAFT', 'SUBMITTED');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'purchase_order_status') THEN
    CREATE TYPE purchase_order_status AS ENUM (
      'DRAFT',
      'IN_TRANSIT',
      'RECEIVED_SPLIT_PENDING',
      'SENT_TO_STORES',
      'COMPLETED',
      'CANCELLED'
    );
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'purchase_order_confidence_state') THEN
    CREATE TYPE purchase_order_confidence_state AS ENUM ('NORMAL', 'LOW');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'par_level_source') THEN
    CREATE TYPE par_level_source AS ENUM ('MANUAL', 'DYNAMIC');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'purchase_order_receipt_status') THEN
    CREATE TYPE purchase_order_receipt_status AS ENUM ('DRAFT', 'SUBMITTED');
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'square_sync_status') THEN
    CREATE TYPE square_sync_status AS ENUM ('PENDING', 'SUCCESS', 'FAILED');
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

CREATE TABLE IF NOT EXISTS ordering_math_settings (
  id INTEGER PRIMARY KEY DEFAULT 1,
  default_reorder_weeks INTEGER NOT NULL DEFAULT 5,
  default_stock_up_weeks INTEGER NOT NULL DEFAULT 10,
  default_history_lookback_days INTEGER NOT NULL DEFAULT 120,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ordering_math_settings_single_row_ck CHECK (id = 1),
  CONSTRAINT ordering_math_settings_reorder_weeks_ck CHECK (default_reorder_weeks > 0),
  CONSTRAINT ordering_math_settings_stock_up_weeks_ck CHECK (default_stock_up_weeks > default_reorder_weeks),
  CONSTRAINT ordering_math_settings_history_days_ck CHECK (
    default_history_lookback_days >= 7 AND default_history_lookback_days <= 730
  )
);

CREATE TABLE IF NOT EXISTS vendors (
  id BIGSERIAL PRIMARY KEY,
  square_vendor_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  last_synced_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vendor_contacts (
  id BIGSERIAL PRIMARY KEY,
  vendor_id BIGINT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  contact_name TEXT,
  email_to TEXT NOT NULL,
  email_cc TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vendor_ordering_settings (
  vendor_id BIGINT PRIMARY KEY REFERENCES vendors(id) ON DELETE CASCADE,
  reorder_weeks INTEGER NOT NULL DEFAULT 5,
  stock_up_weeks INTEGER NOT NULL DEFAULT 10,
  history_lookback_days INTEGER NOT NULL DEFAULT 120,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT vendor_ordering_settings_reorder_weeks_ck CHECK (reorder_weeks > 0),
  CONSTRAINT vendor_ordering_settings_stock_up_weeks_ck CHECK (stock_up_weeks > reorder_weeks),
  CONSTRAINT vendor_ordering_settings_history_days_ck CHECK (history_lookback_days >= 7 AND history_lookback_days <= 730)
);

CREATE TABLE IF NOT EXISTS vendor_sku_configs (
  id BIGSERIAL PRIMARY KEY,
  vendor_id BIGINT NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
  sku TEXT NOT NULL,
  square_variation_id TEXT,
  unit_cost NUMERIC(14,4) NOT NULL DEFAULT 0,
  pack_size INTEGER NOT NULL DEFAULT 1,
  min_order_qty INTEGER NOT NULL DEFAULT 0,
  is_default_vendor BOOLEAN NOT NULL DEFAULT TRUE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT vendor_sku_configs_pack_size_ck CHECK (pack_size >= 1),
  CONSTRAINT vendor_sku_configs_min_order_qty_ck CHECK (min_order_qty >= 0),
  CONSTRAINT vendor_sku_configs_vendor_sku_uniq UNIQUE (vendor_id, sku)
);
ALTER TABLE vendor_sku_configs ADD COLUMN IF NOT EXISTS square_variation_id TEXT;
ALTER TABLE vendor_sku_configs ADD COLUMN IF NOT EXISTS unit_cost NUMERIC(14,4);
UPDATE vendor_sku_configs SET unit_cost = 0 WHERE unit_cost IS NULL;
ALTER TABLE vendor_sku_configs ALTER COLUMN unit_cost SET DEFAULT 0;
ALTER TABLE vendor_sku_configs ALTER COLUMN unit_cost SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_vendor_sku_configs_default_vendor
ON vendor_sku_configs(sku)
WHERE is_default_vendor IS TRUE AND active IS TRUE;

CREATE TABLE IF NOT EXISTS purchase_order_pdf_templates (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  legal_disclaimer TEXT,
  is_generic BOOLEAN NOT NULL DEFAULT FALSE,
  vendor_id BIGINT REFERENCES vendors(id) ON DELETE CASCADE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT purchase_order_pdf_templates_vendor_uniq UNIQUE (vendor_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_order_pdf_templates_generic_uniq
ON purchase_order_pdf_templates (is_generic)
WHERE is_generic IS TRUE;
CREATE INDEX IF NOT EXISTS idx_purchase_order_pdf_templates_vendor
ON purchase_order_pdf_templates (vendor_id, active);

CREATE TABLE IF NOT EXISTS par_levels (
  id BIGSERIAL PRIMARY KEY,
  sku TEXT NOT NULL,
  vendor_id BIGINT REFERENCES vendors(id) ON DELETE SET NULL,
  store_id BIGINT REFERENCES stores(id) ON DELETE SET NULL,
  manual_par_level INTEGER,
  manual_stock_up_level INTEGER,
  suggested_par_level INTEGER,
  par_source par_level_source NOT NULL DEFAULT 'MANUAL',
  confidence_score NUMERIC(5,4),
  confidence_state purchase_order_confidence_state NOT NULL DEFAULT 'LOW',
  locked_manual BOOLEAN NOT NULL DEFAULT TRUE,
  confidence_streak_up INTEGER NOT NULL DEFAULT 0,
  confidence_streak_down INTEGER NOT NULL DEFAULT 0,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT par_levels_manual_non_negative_ck CHECK (manual_par_level IS NULL OR manual_par_level >= 0),
  CONSTRAINT par_levels_manual_stock_up_non_negative_ck CHECK (manual_stock_up_level IS NULL OR manual_stock_up_level >= 0),
  CONSTRAINT par_levels_suggested_non_negative_ck CHECK (suggested_par_level IS NULL OR suggested_par_level >= 0),
  CONSTRAINT par_levels_confidence_score_ck CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1))
);
ALTER TABLE par_levels ADD COLUMN IF NOT EXISTS store_id BIGINT REFERENCES stores(id) ON DELETE SET NULL;
ALTER TABLE par_levels ADD COLUMN IF NOT EXISTS manual_stock_up_level INTEGER;
ALTER TABLE par_levels DROP CONSTRAINT IF EXISTS par_levels_manual_stock_up_non_negative_ck;
ALTER TABLE par_levels
  ADD CONSTRAINT par_levels_manual_stock_up_non_negative_ck CHECK (
    manual_stock_up_level IS NULL OR manual_stock_up_level >= 0
  );
ALTER TABLE par_levels DROP CONSTRAINT IF EXISTS par_levels_sku_vendor_uniq;
CREATE UNIQUE INDEX IF NOT EXISTS idx_par_levels_vendor_sku_global
ON par_levels (vendor_id, sku)
WHERE store_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_par_levels_vendor_store_sku
ON par_levels (vendor_id, store_id, sku)
WHERE store_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS purchase_orders (
  id BIGSERIAL PRIMARY KEY,
  vendor_id BIGINT NOT NULL REFERENCES vendors(id),
  status purchase_order_status NOT NULL DEFAULT 'DRAFT',
  reorder_weeks INTEGER NOT NULL DEFAULT 5,
  stock_up_weeks INTEGER NOT NULL DEFAULT 10,
  history_lookback_days INTEGER NOT NULL DEFAULT 120,
  notes TEXT,
  pdf_path TEXT,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  submitted_by_principal_id BIGINT REFERENCES principals(id),
  ordered_at TIMESTAMPTZ,
  submitted_at TIMESTAMPTZ,
  email_sent_at TIMESTAMPTZ,
  email_sent_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT purchase_orders_reorder_weeks_ck CHECK (reorder_weeks > 0),
  CONSTRAINT purchase_orders_stock_up_weeks_ck CHECK (stock_up_weeks > reorder_weeks),
  CONSTRAINT purchase_orders_history_days_ck CHECK (history_lookback_days >= 7 AND history_lookback_days <= 730)
);

CREATE TABLE IF NOT EXISTS purchase_order_lines (
  id BIGSERIAL PRIMARY KEY,
  purchase_order_id BIGINT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  variation_id TEXT NOT NULL,
  sku TEXT,
  item_name TEXT NOT NULL,
  variation_name TEXT NOT NULL,
  unit_cost NUMERIC(14,4),
  unit_price NUMERIC(14,2),
  suggested_qty INTEGER NOT NULL DEFAULT 0,
  ordered_qty INTEGER NOT NULL DEFAULT 0,
  received_qty_total INTEGER NOT NULL DEFAULT 0,
  in_transit_qty INTEGER NOT NULL DEFAULT 0,
  confidence_score NUMERIC(5,4),
  confidence_state purchase_order_confidence_state NOT NULL DEFAULT 'NORMAL',
  par_source par_level_source NOT NULL DEFAULT 'MANUAL',
  manual_par_level INTEGER,
  suggested_par_level INTEGER,
  removed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT purchase_order_lines_suggested_qty_ck CHECK (suggested_qty >= 0),
  CONSTRAINT purchase_order_lines_ordered_qty_ck CHECK (ordered_qty >= 0),
  CONSTRAINT purchase_order_lines_received_qty_total_ck CHECK (received_qty_total >= 0),
  CONSTRAINT purchase_order_lines_in_transit_qty_ck CHECK (in_transit_qty >= 0),
  CONSTRAINT purchase_order_lines_confidence_score_ck CHECK (
    confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)
  ),
  CONSTRAINT purchase_order_lines_manual_par_ck CHECK (manual_par_level IS NULL OR manual_par_level >= 0),
  CONSTRAINT purchase_order_lines_suggested_par_ck CHECK (suggested_par_level IS NULL OR suggested_par_level >= 0),
  CONSTRAINT purchase_order_lines_order_variation_uniq UNIQUE (purchase_order_id, variation_id)
);

CREATE TABLE IF NOT EXISTS purchase_order_store_allocations (
  id BIGSERIAL PRIMARY KEY,
  purchase_order_line_id BIGINT NOT NULL REFERENCES purchase_order_lines(id) ON DELETE CASCADE,
  store_id BIGINT NOT NULL REFERENCES stores(id),
  expected_qty INTEGER NOT NULL DEFAULT 0,
  allocated_qty INTEGER NOT NULL DEFAULT 0,
  manual_par_level INTEGER,
  store_received_qty INTEGER,
  variance_qty INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT purchase_order_store_allocations_expected_qty_ck CHECK (expected_qty >= 0),
  CONSTRAINT purchase_order_store_allocations_allocated_qty_ck CHECK (allocated_qty >= 0),
  CONSTRAINT purchase_order_store_allocations_manual_par_ck CHECK (manual_par_level IS NULL OR manual_par_level >= 0),
  CONSTRAINT purchase_order_store_allocations_store_received_qty_ck CHECK (
    store_received_qty IS NULL OR store_received_qty >= 0
  ),
  CONSTRAINT purchase_order_store_allocations_line_store_uniq UNIQUE (purchase_order_line_id, store_id)
);
ALTER TABLE purchase_order_store_allocations ADD COLUMN IF NOT EXISTS manual_par_level INTEGER;
ALTER TABLE purchase_order_store_allocations DROP CONSTRAINT IF EXISTS purchase_order_store_allocations_manual_par_ck;
ALTER TABLE purchase_order_store_allocations
  ADD CONSTRAINT purchase_order_store_allocations_manual_par_ck CHECK (
    manual_par_level IS NULL OR manual_par_level >= 0
  );

CREATE TABLE IF NOT EXISTS purchase_order_receipts (
  id BIGSERIAL PRIMARY KEY,
  purchase_order_id BIGINT NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  status purchase_order_receipt_status NOT NULL DEFAULT 'DRAFT',
  received_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  received_at TIMESTAMPTZ,
  is_partial BOOLEAN NOT NULL DEFAULT FALSE,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS purchase_order_receipt_lines (
  id BIGSERIAL PRIMARY KEY,
  receipt_id BIGINT NOT NULL REFERENCES purchase_order_receipts(id) ON DELETE CASCADE,
  purchase_order_line_id BIGINT NOT NULL REFERENCES purchase_order_lines(id) ON DELETE CASCADE,
  expected_qty INTEGER NOT NULL DEFAULT 0,
  received_qty INTEGER NOT NULL DEFAULT 0,
  difference_qty INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT purchase_order_receipt_lines_expected_qty_ck CHECK (expected_qty >= 0),
  CONSTRAINT purchase_order_receipt_lines_received_qty_ck CHECK (received_qty >= 0),
  CONSTRAINT purchase_order_receipt_lines_receipt_line_uniq UNIQUE (receipt_id, purchase_order_line_id)
);

CREATE TABLE IF NOT EXISTS square_sync_events (
  id BIGSERIAL PRIMARY KEY,
  purchase_order_id BIGINT REFERENCES purchase_orders(id) ON DELETE SET NULL,
  purchase_order_line_id BIGINT REFERENCES purchase_order_lines(id) ON DELETE SET NULL,
  store_id BIGINT REFERENCES stores(id) ON DELETE SET NULL,
  sync_type TEXT NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL UNIQUE,
  status square_sync_status NOT NULL DEFAULT 'PENDING',
  request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_payload JSONB,
  error_text TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT square_sync_events_attempt_count_ck CHECK (attempt_count >= 0)
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

CREATE TABLE IF NOT EXISTS change_box_counts (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  employee_name TEXT NOT NULL,
  status change_box_count_status NOT NULL DEFAULT 'DRAFT',
  total_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  submitted_by_principal_id BIGINT REFERENCES principals(id),
  submitted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS change_box_count_lines (
  count_id BIGINT NOT NULL REFERENCES change_box_counts(id) ON DELETE CASCADE,
  denomination_code VARCHAR(64) NOT NULL,
  denomination_label TEXT NOT NULL,
  position INTEGER NOT NULL,
  unit_value NUMERIC(10,2) NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  line_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (count_id, denomination_code),
  CONSTRAINT change_box_count_lines_quantity_non_negative_ck CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS non_sellable_items (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS non_sellable_stock_takes (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  employee_name TEXT NOT NULL,
  status non_sellable_stock_take_status NOT NULL DEFAULT 'DRAFT',
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  submitted_by_principal_id BIGINT REFERENCES principals(id),
  submitted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS non_sellable_stock_take_lines (
  stock_take_id BIGINT NOT NULL REFERENCES non_sellable_stock_takes(id) ON DELETE CASCADE,
  item_id BIGINT NOT NULL REFERENCES non_sellable_items(id),
  item_name TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (stock_take_id, item_id),
  CONSTRAINT non_sellable_stock_take_lines_quantity_non_negative_ck CHECK (quantity >= 0)
);
ALTER TABLE non_sellable_stock_take_lines
  ALTER COLUMN quantity TYPE NUMERIC(12,3) USING quantity::numeric;

CREATE TABLE IF NOT EXISTS customer_request_items (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL UNIQUE,
  request_count INTEGER NOT NULL DEFAULT 0,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT customer_request_items_request_count_non_negative_ck CHECK (request_count >= 0)
);

CREATE TABLE IF NOT EXISTS customer_request_submissions (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  notes TEXT,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer_request_lines (
  id BIGSERIAL PRIMARY KEY,
  submission_id BIGINT NOT NULL REFERENCES customer_request_submissions(id) ON DELETE CASCADE,
  item_id BIGINT NOT NULL REFERENCES customer_request_items(id),
  raw_name TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT customer_request_lines_quantity_positive_ck CHECK (quantity > 0)
);

CREATE TABLE IF NOT EXISTS change_form_submissions (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  employee_name TEXT NOT NULL,
  signature_full_name TEXT NOT NULL,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS change_form_lines (
  id BIGSERIAL PRIMARY KEY,
  submission_id BIGINT NOT NULL REFERENCES change_form_submissions(id) ON DELETE CASCADE,
  section VARCHAR(64) NOT NULL,
  denomination_code VARCHAR(64) NOT NULL,
  denomination_label TEXT NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  unit_value NUMERIC(10,2) NOT NULL,
  line_amount NUMERIC(14,2) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT change_form_lines_quantity_non_negative_ck CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS change_box_inventory_settings (
  store_id BIGINT PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
  target_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS change_box_inventory_lines (
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  denomination_code VARCHAR(64) NOT NULL,
  denomination_label TEXT NOT NULL,
  unit_value NUMERIC(10,2) NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (store_id, denomination_code),
  CONSTRAINT change_box_inventory_lines_quantity_non_negative_ck CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS change_box_audit_submissions (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  auditor_name TEXT NOT NULL,
  target_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS change_box_audit_lines (
  id BIGSERIAL PRIMARY KEY,
  audit_submission_id BIGINT NOT NULL REFERENCES change_box_audit_submissions(id) ON DELETE CASCADE,
  denomination_code VARCHAR(64) NOT NULL,
  denomination_label TEXT NOT NULL,
  unit_value NUMERIC(10,2) NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  line_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT change_box_audit_lines_quantity_non_negative_ck CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS exchange_return_forms (
  id BIGSERIAL PRIMARY KEY,
  store_id BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
  employee_name TEXT NOT NULL,
  original_purchase_date DATE NOT NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  original_ticket_number TEXT NOT NULL,
  exchange_ticket_number TEXT NOT NULL,
  items_text TEXT NOT NULL,
  reason_text TEXT NOT NULL,
  refund_given BOOLEAN NOT NULL,
  refund_approved_by TEXT NOT NULL,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS master_safe_inventory_settings (
  id INTEGER PRIMARY KEY DEFAULT 1,
  target_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT master_safe_inventory_settings_single_row_ck CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS master_safe_inventory_lines (
  denomination_code VARCHAR(64) PRIMARY KEY,
  denomination_label TEXT NOT NULL,
  unit_value NUMERIC(10,2) NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  updated_by_principal_id BIGINT REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT master_safe_inventory_lines_quantity_non_negative_ck CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS master_safe_audit_submissions (
  id BIGSERIAL PRIMARY KEY,
  auditor_name TEXT NOT NULL,
  target_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  created_by_principal_id BIGINT NOT NULL REFERENCES principals(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS master_safe_audit_lines (
  id BIGSERIAL PRIMARY KEY,
  audit_submission_id BIGINT NOT NULL REFERENCES master_safe_audit_submissions(id) ON DELETE CASCADE,
  denomination_code VARCHAR(64) NOT NULL,
  denomination_label TEXT NOT NULL,
  unit_value NUMERIC(10,2) NOT NULL,
  quantity INTEGER NOT NULL DEFAULT 0,
  line_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT master_safe_audit_lines_quantity_non_negative_ck CHECK (quantity >= 0)
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
CREATE INDEX IF NOT EXISTS idx_change_box_counts_store_created ON change_box_counts(store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_box_count_lines_count ON change_box_count_lines(count_id);
CREATE INDEX IF NOT EXISTS idx_non_sellable_items_active ON non_sellable_items(active, name);
CREATE INDEX IF NOT EXISTS idx_non_sellable_stock_takes_store_created ON non_sellable_stock_takes(store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_non_sellable_stock_take_lines_take ON non_sellable_stock_take_lines(stock_take_id);
CREATE INDEX IF NOT EXISTS idx_customer_request_items_active ON customer_request_items(active, name);
CREATE INDEX IF NOT EXISTS idx_customer_request_submissions_store_created ON customer_request_submissions(store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_customer_request_lines_submission ON customer_request_lines(submission_id);
CREATE INDEX IF NOT EXISTS idx_change_form_submissions_store_created ON change_form_submissions(store_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_form_lines_submission ON change_form_lines(submission_id);
CREATE INDEX IF NOT EXISTS idx_change_box_inventory_lines_store ON change_box_inventory_lines(store_id, denomination_code);
CREATE INDEX IF NOT EXISTS idx_change_box_audit_submissions_store_created ON change_box_audit_submissions(store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_box_audit_lines_submission ON change_box_audit_lines(audit_submission_id);
CREATE INDEX IF NOT EXISTS idx_exchange_return_forms_store_created ON exchange_return_forms(store_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_master_safe_audit_submissions_created ON master_safe_audit_submissions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_master_safe_audit_lines_submission ON master_safe_audit_lines(audit_submission_id);
CREATE INDEX IF NOT EXISTS idx_vendors_active_name ON vendors(active, name);
CREATE INDEX IF NOT EXISTS idx_vendor_contacts_vendor_active ON vendor_contacts(vendor_id, active);
CREATE INDEX IF NOT EXISTS idx_vendor_sku_configs_vendor_active ON vendor_sku_configs(vendor_id, active);
CREATE INDEX IF NOT EXISTS idx_vendor_sku_configs_square_variation ON vendor_sku_configs(square_variation_id);
CREATE INDEX IF NOT EXISTS idx_par_levels_sku ON par_levels(sku);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_vendor_created ON purchase_orders(vendor_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchase_orders_status_created ON purchase_orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_order ON purchase_order_lines(purchase_order_id);
CREATE INDEX IF NOT EXISTS idx_purchase_order_lines_variation ON purchase_order_lines(variation_id);
CREATE INDEX IF NOT EXISTS idx_purchase_order_store_allocations_store ON purchase_order_store_allocations(store_id);
CREATE INDEX IF NOT EXISTS idx_purchase_order_receipts_order ON purchase_order_receipts(purchase_order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_purchase_order_receipt_lines_receipt ON purchase_order_receipt_lines(receipt_id);
CREATE INDEX IF NOT EXISTS idx_square_sync_events_status_created ON square_sync_events(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_square_sync_events_order ON square_sync_events(purchase_order_id);

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

DROP TRIGGER IF EXISTS trg_change_box_counts_updated_at ON change_box_counts;
CREATE TRIGGER trg_change_box_counts_updated_at
BEFORE UPDATE ON change_box_counts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_non_sellable_items_updated_at ON non_sellable_items;
CREATE TRIGGER trg_non_sellable_items_updated_at
BEFORE UPDATE ON non_sellable_items
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_non_sellable_stock_takes_updated_at ON non_sellable_stock_takes;
CREATE TRIGGER trg_non_sellable_stock_takes_updated_at
BEFORE UPDATE ON non_sellable_stock_takes
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_customer_request_items_updated_at ON customer_request_items;
CREATE TRIGGER trg_customer_request_items_updated_at
BEFORE UPDATE ON customer_request_items
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_change_box_inventory_settings_updated_at ON change_box_inventory_settings;
CREATE TRIGGER trg_change_box_inventory_settings_updated_at
BEFORE UPDATE ON change_box_inventory_settings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_change_box_inventory_lines_updated_at ON change_box_inventory_lines;
CREATE TRIGGER trg_change_box_inventory_lines_updated_at
BEFORE UPDATE ON change_box_inventory_lines
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_master_safe_inventory_settings_updated_at ON master_safe_inventory_settings;
CREATE TRIGGER trg_master_safe_inventory_settings_updated_at
BEFORE UPDATE ON master_safe_inventory_settings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_master_safe_inventory_lines_updated_at ON master_safe_inventory_lines;
CREATE TRIGGER trg_master_safe_inventory_lines_updated_at
BEFORE UPDATE ON master_safe_inventory_lines
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_store_recount_state_updated_at ON store_recount_state;
CREATE TRIGGER trg_store_recount_state_updated_at
BEFORE UPDATE ON store_recount_state
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_store_recount_items_updated_at ON store_recount_items;
CREATE TRIGGER trg_store_recount_items_updated_at
BEFORE UPDATE ON store_recount_items
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_ordering_math_settings_updated_at ON ordering_math_settings;
CREATE TRIGGER trg_ordering_math_settings_updated_at
BEFORE UPDATE ON ordering_math_settings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_vendors_updated_at ON vendors;
CREATE TRIGGER trg_vendors_updated_at
BEFORE UPDATE ON vendors
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_vendor_contacts_updated_at ON vendor_contacts;
CREATE TRIGGER trg_vendor_contacts_updated_at
BEFORE UPDATE ON vendor_contacts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_vendor_ordering_settings_updated_at ON vendor_ordering_settings;
CREATE TRIGGER trg_vendor_ordering_settings_updated_at
BEFORE UPDATE ON vendor_ordering_settings
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_vendor_sku_configs_updated_at ON vendor_sku_configs;
CREATE TRIGGER trg_vendor_sku_configs_updated_at
BEFORE UPDATE ON vendor_sku_configs
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_order_pdf_templates_updated_at ON purchase_order_pdf_templates;
CREATE TRIGGER trg_purchase_order_pdf_templates_updated_at
BEFORE UPDATE ON purchase_order_pdf_templates
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_par_levels_updated_at ON par_levels;
CREATE TRIGGER trg_par_levels_updated_at
BEFORE UPDATE ON par_levels
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_orders_updated_at ON purchase_orders;
CREATE TRIGGER trg_purchase_orders_updated_at
BEFORE UPDATE ON purchase_orders
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_order_lines_updated_at ON purchase_order_lines;
CREATE TRIGGER trg_purchase_order_lines_updated_at
BEFORE UPDATE ON purchase_order_lines
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_order_store_allocations_updated_at ON purchase_order_store_allocations;
CREATE TRIGGER trg_purchase_order_store_allocations_updated_at
BEFORE UPDATE ON purchase_order_store_allocations
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_order_receipts_updated_at ON purchase_order_receipts;
CREATE TRIGGER trg_purchase_order_receipts_updated_at
BEFORE UPDATE ON purchase_order_receipts
FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_square_sync_events_updated_at ON square_sync_events;
CREATE TRIGGER trg_square_sync_events_updated_at
BEFORE UPDATE ON square_sync_events
FOR EACH ROW EXECUTE FUNCTION set_updated_at();
