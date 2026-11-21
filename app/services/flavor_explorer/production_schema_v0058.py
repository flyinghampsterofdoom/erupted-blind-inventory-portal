"""Frozen additive Product Explorer schema for Alembic revision 20261120_0058.

This file is migration history. Later changes require a new revision instead of
editing these table definitions or importing mutable feature/model metadata.
"""
from __future__ import annotations
import json
from sqlalchemy import (MetaData,Table,Column,BigInteger,Integer,Numeric,String,
    Text,Boolean,DateTime,LargeBinary,ForeignKeyConstraint,UniqueConstraint,
    CheckConstraint,Index,Identity,select,insert,text,inspect,func)
from sqlalchemy.dialects.postgresql import JSONB

EXPLORER_LOCK=1748173701
SCHEMA_VERSION=1
EXTERNAL_TABLES=frozenset({"stores","principals","touchscreen_flavors","touchscreen_flavor_categories"})
IMMUTABLE_TABLES=(
    "fi_flavor_identities","fi_square_flavor_links","fi_notes","fi_sensory_axes",
    "fi_classification_versions","fi_version_notes","fi_version_categories",
    "fi_version_sensory","fi_version_similarities","fi_employee_suggestions",
    "fi_review_decisions","fi_publication_revisions","fi_audit_events",
    "fi_administration_operations","fi_administration_operation_items",
    "fi_administration_operation_reviews","cp_schema","cp_audit","cp_seed_imports",
    "fi_discovery_schema","fi_discovery_operations","fi_discovery_changes",
    "fi_product_photo_schema","fi_product_photo_assets",
    "fi_product_photo_operations","fi_product_photo_events",
    "fi_product_photo_priority_schema","fi_production_schema")
VERSION_CONTENT=("fi_version_notes","fi_version_categories","fi_version_sensory","fi_version_similarities")

TABLES=json.loads("[{\"name\":\"cp_audit\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":true},{\"name\":\"event\",\"type\":[\"string\",30],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"actor_id\",\"type\":[\"integer\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"suggestion_id\",\"type\":[\"integer\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"product_id\",\"type\":[\"string\",160],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before\",\"type\":[\"jsonb\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"after\",\"type\":[\"jsonb\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_at\",\"type\":[\"string\",50],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"suggestion_id\"],\"targets\":[\"cp_suggestions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"cp_audit_actor_existing\",\"kind\":\"fk\",\"columns\":[\"actor_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[]},{\"name\":\"cp_catalog_associations\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"product_id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"square_variation_id\",\"type\":[\"string\",255],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"package_configuration\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"verification_note\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"evidence\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"active\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"string\",\"1\"]}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"product_id\"],\"targets\":[\"cp_products.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"square_variation_id\"]}],\"indexes\":[{\"name\":\"ix_cp_catalog_associations_product_id\",\"columns\":[\"product_id\"],\"unique\":false}]},{\"name\":\"cp_identifiers\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"product_id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"kind\",\"type\":[\"string\",20],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"value\",\"type\":[\"string\",512],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"exact_key\",\"type\":[\"string\",512],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"gtin_key\",\"type\":[\"string\",40],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"package_configuration\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"evidence\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"active\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"string\",\"1\"]}],\"constraints\":[{\"name\":null,\"kind\":\"unique\",\"columns\":[\"product_id\",\"kind\",\"value\",\"package_configuration\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"product_id\"],\"targets\":[\"cp_products.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[{\"name\":\"ix_cp_identifiers_exact_key\",\"columns\":[\"exact_key\"],\"unique\":false},{\"name\":\"ix_cp_identifiers_gtin_key\",\"columns\":[\"gtin_key\"],\"unique\":false},{\"name\":\"ix_cp_identifiers_product_id\",\"columns\":[\"product_id\"],\"unique\":false}]},{\"name\":\"cp_manufacturers\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"name\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"aliases\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[],\"indexes\":[]},{\"name\":\"cp_products\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"manufacturer_id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"name\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"kind\",\"type\":[\"string\",20],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"model\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"family\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"generation\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"resistance_ohm\",\"type\":[\"string\",30],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"capacity_ml\",\"type\":[\"string\",30],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"configuration\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"integrated_coil\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"aliases\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"search_text\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"evidence\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"active\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"manufacturer_id\"],\"targets\":[\"cp_manufacturers.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"check\",\"sql\":\"kind IN ('device','pod','coil','tank','family')\"},{\"name\":null,\"kind\":\"check\",\"sql\":\"version > 0\"}],\"indexes\":[{\"name\":\"ix_cp_products_search_text\",\"columns\":[\"search_text\"],\"unique\":false}]},{\"name\":\"cp_relationships\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"from_id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"to_id\",\"type\":[\"string\",160],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"kind\",\"type\":[\"string\",30],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"conditions\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"evidence\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"active\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"from_id\"],\"targets\":[\"cp_products.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"to_id\"],\"targets\":[\"cp_products.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"check\",\"sql\":\"from_id <> to_id\"},{\"name\":null,\"kind\":\"check\",\"sql\":\"kind IN ('accepts_pod','accepts_coil','includes_tank')\"}],\"indexes\":[{\"name\":\"ix_cp_relationships_from_id\",\"columns\":[\"from_id\"],\"unique\":false},{\"name\":\"ix_cp_relationships_to_id\",\"columns\":[\"to_id\"],\"unique\":false}]},{\"name\":\"cp_schema\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":\"cp_schema_singleton\",\"kind\":\"check\",\"sql\":\"id=1\"}],\"indexes\":[]},{\"name\":\"cp_seed_imports\",\"columns\":[{\"name\":\"digest\",\"type\":[\"string\",64],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"counts\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_at\",\"type\":[\"string\",50],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[],\"indexes\":[]},{\"name\":\"cp_suggestions\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":true},{\"name\":\"employee_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"store_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"kind\",\"type\":[\"string\",30],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"product_id\",\"type\":[\"string\",160],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"target_product_id\",\"type\":[\"string\",160],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"base_version\",\"type\":[\"integer\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"proposed\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"original\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"scanned_identifier\",\"type\":[\"text\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"comment\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"fingerprint\",\"type\":[\"string\",64],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"status\",\"type\":[\"string\",30],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"submitted_at\",\"type\":[\"string\",50],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"decided_at\",\"type\":[\"string\",50],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"decided_by\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"decision_reason\",\"type\":[\"text\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"published\",\"type\":[\"jsonb\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"decided_by\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"employee_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"store_id\"],\"targets\":[\"stores.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"check\",\"sql\":\"status IN ('PENDING','APPROVED','REJECTED','MODIFIED_APPROVED')\"}],\"indexes\":[{\"name\":\"cp_pending_identity\",\"columns\":[\"employee_id\",\"fingerprint\",\"status\"],\"unique\":false},{\"name\":\"ix_cp_suggestions_fingerprint\",\"columns\":[\"fingerprint\"],\"unique\":false},{\"name\":\"ix_cp_suggestions_status\",\"columns\":[\"status\"],\"unique\":false}]},{\"name\":\"fi_administration_operation_items\",\"columns\":[{\"name\":\"operation_id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"before_version_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"restore_version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"after_version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before_snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"after_snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before_verified\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"after_verified\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"after_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"before_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"operation_id\"],\"targets\":[\"fi_administration_operations.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"restore_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[]},{\"name\":\"fi_administration_operation_reviews\",\"columns\":[{\"name\":\"operation_id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"review_group_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"decision_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"decision_id\"],\"targets\":[\"fi_review_decisions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"decision_id\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"operation_id\"],\"targets\":[\"fi_administration_operations.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"review_group_id\"],\"targets\":[\"fi_suggestion_review_groups.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[]},{\"name\":\"fi_administration_operations\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"preview_id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"actor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"kind\",\"type\":[\"string\",24],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"source_operation_id\",\"type\":[\"string\",36],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"plan_fingerprint\",\"type\":[\"string\",64],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"actor_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"preview_id\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"source_operation_id\"],\"targets\":[\"fi_administration_operations.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[]},{\"name\":\"fi_audit_events\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"version_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"actor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"store_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"suggestion_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"review_decision_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"event_type\",\"type\":[\"string\",24],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before_snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"after_snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"occurred_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"actor_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"review_decision_id\"],\"targets\":[\"fi_review_decisions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"store_id\"],\"targets\":[\"stores.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"suggestion_id\"],\"targets\":[\"fi_employee_suggestions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_audit_version_same_flavor\",\"kind\":\"fk\",\"columns\":[\"version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_audit_event_type\",\"kind\":\"check\",\"sql\":\"event_type IN ('SUBMITTED', 'REVIEWED', 'PUBLISHED', 'DIRECT_EDIT', 'ROLLED_BACK')\"}],\"indexes\":[{\"name\":\"fi_audit_flavor_time\",\"columns\":[\"flavor_id\",\"occurred_at\"],\"unique\":false}]},{\"name\":\"fi_classification_versions\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"parent_version_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"restores_version_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"is_baseline\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"false\"],\"default\":false},{\"name\":\"description\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_by_principal_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"created_by_principal_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_versions_flavor_number\",\"kind\":\"unique\",\"columns\":[\"flavor_id\",\"version\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_versions_identity_flavor\",\"kind\":\"unique\",\"columns\":[\"id\",\"flavor_id\"]},{\"name\":\"fi_versions_parent_same_flavor\",\"kind\":\"fk\",\"columns\":[\"parent_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_versions_restore_same_flavor\",\"kind\":\"fk\",\"columns\":[\"restores_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_versions_parent_not_self\",\"kind\":\"check\",\"sql\":\"parent_version_id IS NULL OR parent_version_id <> id\"},{\"name\":\"fi_versions_positive\",\"kind\":\"check\",\"sql\":\"version > 0\"}],\"indexes\":[{\"name\":\"fi_one_unclassified_origin\",\"columns\":[\"flavor_id\"],\"unique\":true,\"where\":\"is_baseline AND restores_version_id IS NULL\"}]},{\"name\":\"fi_discovery_changes\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":true},{\"name\":\"operation_id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"square_variation_id\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before_event_id\",\"type\":[\"integer\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before_excluded\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"excluded\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"before_event_id\",\"square_variation_id\"],\"targets\":[\"fi_discovery_changes.id\",\"fi_discovery_changes.square_variation_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"id\",\"square_variation_id\"]},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"operation_id\",\"square_variation_id\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"operation_id\"],\"targets\":[\"fi_discovery_operations.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"check\",\"sql\":\"before_event_id IS NULL OR before_event_id<id\"},{\"name\":null,\"kind\":\"check\",\"sql\":\"excluded<>before_excluded\"}],\"indexes\":[{\"name\":\"fi_discovery_variation_latest\",\"columns\":[\"square_variation_id\",\"id\"],\"unique\":false}]},{\"name\":\"fi_discovery_operations\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"preview_id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"actor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"excluded\",\"type\":[\"boolean\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"plan_fingerprint\",\"type\":[\"string\",64],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"products\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"actor_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"preview_id\"]},{\"name\":null,\"kind\":\"check\",\"sql\":\"length(plan_fingerprint)=64\"}],\"indexes\":[]},{\"name\":\"fi_discovery_schema\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"check\",\"sql\":\"id=1\"}],\"indexes\":[]},{\"name\":\"fi_employee_suggestions\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"review_group_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"base_version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"employee_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"store_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"author_context\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"original_snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"proposed_patch\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"comment\",\"type\":[\"text\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"submitted_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"employee_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_submissions_group_same_baseline\",\"kind\":\"fk\",\"columns\":[\"review_group_id\",\"flavor_id\",\"base_version_id\"],\"targets\":[\"fi_suggestion_review_groups.id\",\"fi_suggestion_review_groups.flavor_id\",\"fi_suggestion_review_groups.base_version_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"store_id\"],\"targets\":[\"stores.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[{\"name\":\"fi_submissions_employee_time\",\"columns\":[\"employee_id\",\"submitted_at\"],\"unique\":false},{\"name\":\"fi_submissions_flavor_time\",\"columns\":[\"flavor_id\",\"submitted_at\"],\"unique\":false}]},{\"name\":\"fi_notes\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"slug\",\"type\":[\"string\",80],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"label\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"family\",\"type\":[\"string\",80],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"legacy_category_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"legacy_category_id\"],\"targets\":[\"touchscreen_flavor_categories.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"legacy_category_id\"]},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"slug\"]},{\"name\":\"fi_notes_slug_nonempty\",\"kind\":\"check\",\"sql\":\"length(slug) > 0\"}],\"indexes\":[]},{\"name\":\"fi_product_photo_assets\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",64],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"data\",\"type\":[\"binary\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"mime\",\"type\":[\"string\",40],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"width\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"height\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"byte_size\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"check\",\"sql\":\"byte_size=length(data) AND byte_size BETWEEN 1 AND 2097152\"},{\"name\":null,\"kind\":\"check\",\"sql\":\"length(id)=64 AND mime='image/webp'\"},{\"name\":null,\"kind\":\"check\",\"sql\":\"width BETWEEN 1 AND 1200 AND height BETWEEN 1 AND 1200\"},{\"name\":\"fi_photo_content_sha256\",\"kind\":\"check\",\"sql\":\"encode(sha256(data),'hex')=id\"}],\"indexes\":[]},{\"name\":\"fi_product_photo_events\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":true},{\"name\":\"operation_id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"square_variation_id\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"before_event_id\",\"type\":[\"integer\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"policy_mode\",\"type\":[\"string\",10],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"before_event_id\",\"square_variation_id\"],\"targets\":[\"fi_product_photo_events.id\",\"fi_product_photo_events.square_variation_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"id\",\"square_variation_id\"]},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"operation_id\",\"square_variation_id\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"operation_id\"],\"targets\":[\"fi_product_photo_operations.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"check\",\"sql\":\"before_event_id IS NULL OR before_event_id<id\"},{\"kind\":\"check\",\"name\":\"fi_photo_policy_mode\",\"sql\":\"policy_mode IN ('custom','catalog','hidden')\"}],\"indexes\":[{\"name\":\"fi_product_photo_variation_latest\",\"columns\":[\"square_variation_id\",\"id\"],\"unique\":false}]},{\"name\":\"fi_product_photo_operations\",\"columns\":[{\"name\":\"id\",\"type\":[\"string\",36],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"actor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"created_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"product\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"mode\",\"type\":[\"string\",10],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"asset_id\",\"type\":[\"string\",64],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"attribution\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"restored_from_event_id\",\"type\":[\"integer\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"source_kind\",\"type\":[\"string\",16],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"candidate_action\",\"type\":[\"string\",16],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"string\",\"legacy\"]}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"actor_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"asset_id\"],\"targets\":[\"fi_product_photo_assets.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"check\",\"sql\":\"(mode='custom' AND asset_id IS NOT NULL) OR (mode<>'custom' AND asset_id IS NULL)\"},{\"name\":null,\"kind\":\"check\",\"sql\":\"mode IN ('catalog','custom','hidden')\"},{\"kind\":\"check\",\"name\":\"fi_photo_source_kind\",\"sql\":\"source_kind IN ('manufacturer','own','legacy')\"},{\"kind\":\"check\",\"name\":\"fi_photo_candidate_action\",\"sql\":\"candidate_action IN ('legacy','assign','remove','policy')\"},{\"kind\":\"check\",\"name\":\"fi_photo_candidate_shape\",\"sql\":\"(candidate_action='legacy' AND source_kind IS NULL) OR (candidate_action='assign' AND source_kind IS NOT NULL AND mode='custom' AND asset_id IS NOT NULL) OR (candidate_action='remove' AND source_kind IS NOT NULL AND mode='catalog' AND asset_id IS NULL) OR (candidate_action='policy' AND source_kind IS NULL AND mode IN ('catalog','hidden') AND asset_id IS NULL)\"}],\"indexes\":[]},{\"name\":\"fi_product_photo_schema\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"check\",\"sql\":\"id=1\"}],\"indexes\":[]},{\"name\":\"fi_publication_revisions\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"revision\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"action\",\"type\":[\"string\",16],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reviewed_by_principal_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"review_decision_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"published_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":\"fi_publication_flavor_revision\",\"kind\":\"unique\",\"columns\":[\"flavor_id\",\"revision\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"review_decision_id\"],\"targets\":[\"fi_review_decisions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"unique\",\"columns\":[\"review_decision_id\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"reviewed_by_principal_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_publication_version_same_flavor\",\"kind\":\"fk\",\"columns\":[\"version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_publication_version_once\",\"kind\":\"unique\",\"columns\":[\"version_id\"]},{\"name\":\"fi_publication_action\",\"kind\":\"check\",\"sql\":\"action IN ('PUBLISH', 'DIRECT_EDIT', 'ROLLBACK')\"},{\"name\":\"fi_publication_revision_positive\",\"kind\":\"check\",\"sql\":\"revision > 0\"}],\"indexes\":[]},{\"name\":\"fi_review_decisions\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"review_group_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"base_version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"decision\",\"type\":[\"string\",24],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reviewer_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"published_version_id\",\"type\":[\"bigint\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"proposed_patch_snapshot\",\"type\":[\"jsonb\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"published_snapshot\",\"type\":[\"jsonb\"],\"nullable\":true,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reviewed_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":\"fi_decisions_published_same_flavor\",\"kind\":\"fk\",\"columns\":[\"published_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_decisions_group_same_baseline\",\"kind\":\"fk\",\"columns\":[\"review_group_id\",\"flavor_id\",\"base_version_id\"],\"targets\":[\"fi_suggestion_review_groups.id\",\"fi_suggestion_review_groups.flavor_id\",\"fi_suggestion_review_groups.base_version_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_decisions_one_per_group\",\"kind\":\"unique\",\"columns\":[\"review_group_id\"]},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"reviewer_id\"],\"targets\":[\"principals.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_decisions_allowed\",\"kind\":\"check\",\"sql\":\"decision IN ('APPROVED', 'REJECTED', 'MODIFIED_APPROVED')\"},{\"name\":\"fi_decisions_publication_required\",\"kind\":\"check\",\"sql\":\"(decision = 'REJECTED' AND published_version_id IS NULL) OR (decision IN ('APPROVED', 'MODIFIED_APPROVED') AND published_version_id IS NOT NULL)\"}],\"indexes\":[]},{\"name\":\"fi_sensory_axes\",\"columns\":[{\"name\":\"slug\",\"type\":[\"string\",80],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"label\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"description\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[],\"indexes\":[]},{\"name\":\"fi_square_flavor_links\",\"columns\":[{\"name\":\"square_variation_id\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"square_item_id\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"observed_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[{\"name\":\"ix_fi_square_flavor_links_flavor_id\",\"columns\":[\"flavor_id\"],\"unique\":false},{\"name\":\"ix_fi_square_flavor_links_square_item_id\",\"columns\":[\"square_item_id\"],\"unique\":false}]},{\"name\":\"fi_suggestion_review_groups\",\"columns\":[{\"name\":\"id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"base_version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"patch_fingerprint\",\"type\":[\"string\",64],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"status\",\"type\":[\"string\",24],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"string\",\"PENDING\"],\"default\":\"PENDING\"},{\"name\":\"created_at\",\"type\":[\"datetime\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\",\"server_default\":[\"sql\",\"now()\"]}],\"constraints\":[{\"name\":\"fi_groups_baseline_same_flavor\",\"kind\":\"fk\",\"columns\":[\"base_version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_groups_identity_baseline\",\"kind\":\"unique\",\"columns\":[\"id\",\"flavor_id\",\"base_version_id\"]},{\"name\":\"fi_groups_sha256\",\"kind\":\"check\",\"sql\":\"length(patch_fingerprint) = 64\"},{\"name\":\"fi_groups_status\",\"kind\":\"check\",\"sql\":\"status IN ('PENDING', 'APPROVED', 'REJECTED', 'MODIFIED_APPROVED')\"}],\"indexes\":[{\"name\":\"fi_groups_pending_fingerprint\",\"columns\":[\"flavor_id\",\"base_version_id\",\"patch_fingerprint\"],\"unique\":true,\"where\":\"status = 'PENDING'\"}]},{\"name\":\"fi_version_categories\",\"columns\":[{\"name\":\"version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"category_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"category_id\"],\"targets\":[\"touchscreen_flavor_categories.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"version_id\"],\"targets\":[\"fi_classification_versions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[]},{\"name\":\"fi_version_notes\",\"columns\":[{\"name\":\"version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"note_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"note_id\"],\"targets\":[\"fi_notes.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"version_id\"],\"targets\":[\"fi_classification_versions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null}],\"indexes\":[]},{\"name\":\"fi_version_sensory\",\"columns\":[{\"name\":\"version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"axis_slug\",\"type\":[\"string\",80],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"value\",\"type\":[\"numeric\",4,3],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"axis_slug\"],\"targets\":[\"fi_sensory_axes.slug\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":null,\"kind\":\"fk\",\"columns\":[\"version_id\"],\"targets\":[\"fi_classification_versions.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_sensory_unit_interval\",\"kind\":\"check\",\"sql\":\"value >= 0 AND value <= 1\"}],\"indexes\":[]},{\"name\":\"fi_version_similarities\",\"columns\":[{\"name\":\"version_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"related_flavor_id\",\"type\":[\"bigint\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"weight\",\"type\":[\"numeric\",4,3],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"},{\"name\":\"reason\",\"type\":[\"text\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"name\":null,\"kind\":\"fk\",\"columns\":[\"related_flavor_id\"],\"targets\":[\"fi_flavor_identities.id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_similarity_source_version\",\"kind\":\"fk\",\"columns\":[\"version_id\",\"flavor_id\"],\"targets\":[\"fi_classification_versions.id\",\"fi_classification_versions.flavor_id\"],\"ondelete\":\"RESTRICT\",\"onupdate\":null},{\"name\":\"fi_similarity_not_self\",\"kind\":\"check\",\"sql\":\"flavor_id <> related_flavor_id\"},{\"name\":\"fi_similarity_weight\",\"kind\":\"check\",\"sql\":\"weight > 0 AND weight <= 1\"}],\"indexes\":[]},{\"name\":\"fi_product_photo_priority_schema\",\"columns\":[{\"name\":\"id\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":true,\"autoincrement\":\"auto\"},{\"name\":\"version\",\"type\":[\"integer\"],\"nullable\":false,\"primary_key\":false,\"autoincrement\":\"auto\"}],\"constraints\":[{\"kind\":\"check\",\"name\":\"fi_photo_priority_singleton\",\"sql\":\"id=1\"}],\"indexes\":[]}]")

def _type(spec):
    name,*args=spec
    return {"bigint":BigInteger,"integer":Integer,"numeric":Numeric,
            "string":String,"text":Text,"boolean":Boolean,"datetime":lambda:DateTime(timezone=True),
            "binary":LargeBinary,"jsonb":JSONB}[name](*args)

def metadata():
    m=MetaData()
    # Existing portal tables are FK/read targets only; this migration never creates them.
    Table("stores",m,Column("id",BigInteger,primary_key=True),
          Column("name",Text,nullable=False),Column("square_location_id",Text),
          Column("active",Boolean,nullable=False))
    Table("principals",m,Column("id",BigInteger,primary_key=True),
          Column("username",Text,nullable=False),Column("role",Text,nullable=False),
          Column("active",Boolean,nullable=False),Column("store_id",BigInteger))
    Table("touchscreen_flavor_categories",m,Column("id",BigInteger,primary_key=True),
          Column("name",Text,nullable=False),Column("slug",Text,nullable=False),
          Column("category_type",String(16),nullable=False))
    Table("touchscreen_flavors",m,Column("id",BigInteger,primary_key=True))
    Table("fi_flavor_identities",m,Column("id",BigInteger,Identity(),primary_key=True),
          Column("legacy_flavor_id",BigInteger,unique=True),
          Column("created_at",DateTime(timezone=True),nullable=False,server_default=text("now()")),
          ForeignKeyConstraint(["legacy_flavor_id"],["touchscreen_flavors.id"],ondelete="RESTRICT"))
    for definition in TABLES:
        columns=[]
        for source in definition["columns"]:
            options={key:source[key] for key in ("nullable","primary_key","autoincrement")}
            if "default" in source:options["default"]=source["default"]
            if "server_default" in source:
                kind,value=source["server_default"]
                options["server_default"]=text(value) if kind=="sql" else value
            columns.append(Column(source["name"],_type(source["type"]),**options))
        constraints=[]
        for source in definition["constraints"]:
            kind=source["kind"];options={"name":source.get("name")}
            if kind=="fk":
                constraints.append(ForeignKeyConstraint(source["columns"],source["targets"],
                    ondelete=source.get("ondelete"),onupdate=source.get("onupdate"),**options))
            elif kind=="unique":constraints.append(UniqueConstraint(*source["columns"],**options))
            else:constraints.append(CheckConstraint(source["sql"],**options))
        table=Table(definition["name"],m,*columns,*constraints)
        for source in definition["indexes"]:
            options={"unique":source["unique"]}
            if "where" in source:options["postgresql_where"]=text(source["where"])
            Index(source["name"],*[table.c[name] for name in source["columns"]],**options)
    Table("fi_production_schema",m,Column("id",Integer,primary_key=True),
          Column("version",Integer,nullable=False),CheckConstraint("id=1"))
    Table("fi_catalog_observations",m,Column("id",Integer,primary_key=True),
          Column("snapshot",JSONB),Column("identity",JSONB),
          Column("generation",BigInteger,nullable=False,server_default=text("0")),
          Column("observed_at",DateTime(timezone=True)),Column("last_success_at",DateTime(timezone=True)),
          Column("last_attempt_at",DateTime(timezone=True)),Column("last_error",Text),
          CheckConstraint("id=1"),CheckConstraint("generation>=0"))
    Table("fi_refresh_lease",m,Column("id",Integer,primary_key=True),
          Column("lease_key",Text,nullable=False),Column("owner",Text),
          Column("expires_at",DateTime(timezone=True)),CheckConstraint("id=1"),
          CheckConstraint("(owner IS NULL)=(expires_at IS NULL)"))
    return m

def feature_tables(m):
    return [table for table in m.sorted_tables if table.name not in EXTERNAL_TABLES]

def _trigger(c,name,timing,table,function,*args):
    arguments=",".join("'"+value+"'" for value in args)
    c.exec_driver_sql('CREATE TRIGGER "'+name+'" '+timing+' ON "'+table+
        '" FOR EACH ROW EXECUTE FUNCTION '+function+'('+arguments+')')

def _function(c,name,body):
    c.execute(text("CREATE FUNCTION "+name+"() RETURNS trigger LANGUAGE plpgsql AS $explorer$ "+body+" $explorer$"))

def install_guards(c):
    _function(c,"fi_explorer_immutable","""
        BEGIN RAISE EXCEPTION 'Explorer history is append-only' USING ERRCODE='23514'; END;
    """)
    for name in IMMUTABLE_TABLES:
        _trigger(c,name+"_immutable","BEFORE UPDATE OR DELETE",name,"fi_explorer_immutable")
    _function(c,"fi_explorer_admin","""
        DECLARE actor bigint;
        BEGIN
          actor := (to_jsonb(NEW)->>TG_ARGV[0])::bigint;
          IF NOT EXISTS (SELECT 1 FROM principals WHERE id=actor AND active AND role IN ('ADMIN','MANAGER')) THEN
            RAISE EXCEPTION 'Active administrator required' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    for table,actor in (
        ("fi_administration_operations","actor_id"),
        ("fi_review_decisions","reviewer_id"),
        ("fi_publication_revisions","reviewed_by_principal_id"),
        ("fi_discovery_operations","actor_id"),("fi_product_photo_operations","actor_id")):
        _trigger(c,table+"_admin","BEFORE INSERT",table,"fi_explorer_admin",actor)
    _function(c,"fi_explorer_version","""
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.created_by_principal_id AND active
              AND (role IN ('ADMIN','MANAGER') OR NEW.is_baseline)) THEN
            RAISE EXCEPTION 'Active administrator required' USING ERRCODE='23514';
          END IF;
          IF NEW.is_baseline AND (NEW.description<>'' OR NEW.snapshot IS DISTINCT FROM
              '{"description":"","notes":[],"categories":[],"sensory":{},"similarities":[]}'::jsonb) THEN
            RAISE EXCEPTION 'Unclassified baseline must be empty' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_classification_version_guard","BEFORE INSERT","fi_classification_versions","fi_explorer_version")
    _function(c,"fi_explorer_frozen_content","""
        BEGIN
          IF EXISTS (SELECT 1 FROM fi_publication_revisions WHERE version_id=NEW.version_id) THEN
            RAISE EXCEPTION 'Published version content is frozen' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    for name in VERSION_CONTENT:
        _trigger(c,name+"_frozen","BEFORE INSERT",name,"fi_explorer_frozen_content")
    _function(c,"fi_explorer_group","""
        BEGIN
          IF TG_OP='INSERT' THEN
            IF NEW.status<>'PENDING' THEN RAISE EXCEPTION 'New review groups must be pending' USING ERRCODE='23514'; END IF;
          ELSIF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'Review attribution is retained' USING ERRCODE='23514';
          ELSIF OLD.status<>'PENDING' OR (to_jsonb(OLD)-'status') IS DISTINCT FROM (to_jsonb(NEW)-'status')
              OR NOT EXISTS (SELECT 1 FROM fi_review_decisions WHERE review_group_id=OLD.id AND decision=NEW.status) THEN
            RAISE EXCEPTION 'Only a matching final review decision may close a group' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_review_group_guard","BEFORE INSERT OR UPDATE OR DELETE","fi_suggestion_review_groups","fi_explorer_group")
    _function(c,"fi_explorer_submission","""
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM fi_suggestion_review_groups WHERE id=NEW.review_group_id AND status='PENDING')
              OR NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.employee_id AND active) THEN
            RAISE EXCEPTION 'Suggestion requires active employee and pending group' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_employee_submission_guard","BEFORE INSERT","fi_employee_suggestions","fi_explorer_submission")
    _function(c,"fi_explorer_review","""
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM fi_suggestion_review_groups WHERE id=NEW.review_group_id AND status='PENDING') THEN
            RAISE EXCEPTION 'Review group is closed' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_review_decision_guard","BEFORE INSERT","fi_review_decisions","fi_explorer_review")
    _function(c,"fi_explorer_publication","""
        BEGIN
          IF NEW.revision<>(SELECT COALESCE(MAX(revision),0)+1 FROM fi_publication_revisions WHERE flavor_id=NEW.flavor_id) THEN
            RAISE EXCEPTION 'Publication revision must extend current history' USING ERRCODE='23514';
          END IF;
          IF NEW.review_decision_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM fi_review_decisions
              WHERE id=NEW.review_decision_id AND flavor_id=NEW.flavor_id AND published_version_id=NEW.version_id
                AND decision IN ('APPROVED','MODIFIED_APPROVED')) THEN
            RAISE EXCEPTION 'Review decision and publication must agree' USING ERRCODE='23514';
          END IF;
          IF NEW.action='ROLLBACK' AND NOT EXISTS (SELECT 1 FROM fi_classification_versions
              WHERE id=NEW.version_id AND restores_version_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Rollback requires a new restoring version' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_publication_chain","BEFORE INSERT","fi_publication_revisions","fi_explorer_publication")
    _function(c,"fi_explorer_audit","""
        BEGIN
          IF TG_TABLE_NAME='fi_publication_revisions' THEN
            INSERT INTO fi_audit_events(flavor_id,version_id,actor_id,event_type,before_snapshot,after_snapshot,reason)
            VALUES(NEW.flavor_id,NEW.version_id,NEW.reviewed_by_principal_id,
              CASE NEW.action WHEN 'ROLLBACK' THEN 'ROLLED_BACK' WHEN 'DIRECT_EDIT' THEN 'DIRECT_EDIT' ELSE 'PUBLISHED' END,
              COALESCE((SELECT v.snapshot FROM fi_publication_revisions p JOIN fi_classification_versions v ON v.id=p.version_id
                WHERE p.flavor_id=NEW.flavor_id AND p.revision<NEW.revision ORDER BY p.revision DESC LIMIT 1),'{}'::jsonb),
              (SELECT snapshot FROM fi_classification_versions WHERE id=NEW.version_id),NEW.reason);
          ELSIF TG_TABLE_NAME='fi_employee_suggestions' THEN
            INSERT INTO fi_audit_events(flavor_id,actor_id,store_id,suggestion_id,event_type,before_snapshot,after_snapshot,reason)
            VALUES(NEW.flavor_id,NEW.employee_id,NEW.store_id,NEW.id,'SUBMITTED',NEW.original_snapshot,NEW.proposed_patch,
              COALESCE(NEW.comment,'Employee suggestion'));
          ELSE
            INSERT INTO fi_audit_events(flavor_id,version_id,actor_id,review_decision_id,event_type,before_snapshot,after_snapshot,reason)
            VALUES(NEW.flavor_id,NEW.published_version_id,NEW.reviewer_id,NEW.id,'REVIEWED',
              (SELECT snapshot FROM fi_classification_versions WHERE id=NEW.base_version_id),
              CASE WHEN NEW.published_snapshot IS NULL OR NEW.published_snapshot='null'::jsonb
                THEN NEW.proposed_patch_snapshot ELSE NEW.published_snapshot END,NEW.reason);
          END IF;
          RETURN NEW;
        END;
    """)
    for table in ("fi_publication_revisions","fi_employee_suggestions","fi_review_decisions"):
        _trigger(c,table+"_audit","AFTER INSERT",table,"fi_explorer_audit")
    _function(c,"fi_explorer_cp_pending","""
        BEGIN
          IF NEW.status<>'PENDING' OR NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.employee_id AND active)
              OR NEW.decided_by IS NOT NULL OR NEW.decided_at IS NOT NULL
              OR (NEW.published IS NOT NULL AND NEW.published<>'null'::jsonb) THEN
            RAISE EXCEPTION 'Compatibility proposals require an active employee and pending initial state' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"cp_pending_guard","BEFORE INSERT","cp_suggestions","fi_explorer_cp_pending")
    _function(c,"fi_explorer_cp_suggestion","""
        BEGIN
          IF TG_OP='DELETE' THEN
            RAISE EXCEPTION 'Compatibility suggestions are retained' USING ERRCODE='23514';
          END IF;
          IF OLD.status<>'PENDING' OR NEW.status NOT IN ('APPROVED','REJECTED','MODIFIED_APPROVED')
              OR (to_jsonb(OLD)-ARRAY['status','decided_at','decided_by','decision_reason','published'])
                IS DISTINCT FROM (to_jsonb(NEW)-ARRAY['status','decided_at','decided_by','decision_reason','published']) THEN
            RAISE EXCEPTION 'Compatibility original proposals and decisions are immutable' USING ERRCODE='23514';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM principals WHERE id=NEW.decided_by AND active AND role IN ('ADMIN','MANAGER','LEAD')) THEN
            RAISE EXCEPTION 'Active Lead or administrator required' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"cp_suggestion_guard","BEFORE UPDATE OR DELETE","cp_suggestions","fi_explorer_cp_suggestion")
    _function(c,"fi_explorer_discovery_event","""
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM fi_discovery_operations o,
              LATERAL jsonb_array_elements(o.products) p,
              LATERAL jsonb_array_elements_text(p->'squareVariationIds') v
              WHERE o.id=NEW.operation_id AND v=NEW.square_variation_id AND o.excluded=NEW.excluded) THEN
            RAISE EXCEPTION 'Variation outside recorded discovery action' USING ERRCODE='23514';
          END IF;
          IF COALESCE(NEW.before_event_id,0)<>(SELECT COALESCE(MAX(id),0) FROM fi_discovery_changes WHERE square_variation_id=NEW.square_variation_id)
              OR NEW.before_excluded IS DISTINCT FROM COALESCE((SELECT excluded FROM fi_discovery_changes
                WHERE square_variation_id=NEW.square_variation_id ORDER BY id DESC LIMIT 1),false) THEN
            RAISE EXCEPTION 'Discovery visibility head changed' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_discovery_event_guard","BEFORE INSERT","fi_discovery_changes","fi_explorer_discovery_event")
    _function(c,"fi_explorer_photo_event","""
        DECLARE operation fi_product_photo_operations%ROWTYPE;previous_policy text;
        BEGIN
          SELECT * INTO operation FROM fi_product_photo_operations WHERE id=NEW.operation_id;
          IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(operation.product->'squareVariationIds') v
              WHERE v=NEW.square_variation_id) THEN
            RAISE EXCEPTION 'Variation outside recorded photo action' USING ERRCODE='23514';
          END IF;
          IF COALESCE(NEW.before_event_id,0)<>(SELECT COALESCE(MAX(id),0) FROM fi_product_photo_events WHERE square_variation_id=NEW.square_variation_id) THEN
            RAISE EXCEPTION 'Product photo head changed' USING ERRCODE='23514';
          END IF;
          IF operation.candidate_action='legacy' AND NEW.policy_mode IS NOT NULL
              OR operation.candidate_action='assign' AND NEW.policy_mode IS DISTINCT FROM 'custom'
              OR operation.candidate_action='policy' AND NEW.policy_mode IS DISTINCT FROM operation.mode THEN
            RAISE EXCEPTION 'Photo policy does not match the recorded action' USING ERRCODE='23514';
          END IF;
          IF operation.candidate_action='remove' THEN
            SELECT COALESCE(e.policy_mode,o.mode) INTO previous_policy FROM fi_product_photo_events e
              JOIN fi_product_photo_operations o ON o.id=e.operation_id
              WHERE e.id=NEW.before_event_id;
            IF NEW.policy_mode IS DISTINCT FROM COALESCE(previous_policy,'catalog') THEN
              RAISE EXCEPTION 'Candidate removal must preserve the photo policy' USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END;
    """)
    _trigger(c,"fi_product_photo_event_guard","BEFORE INSERT","fi_product_photo_events","fi_explorer_photo_event")

FUNCTIONS=("fi_explorer_immutable","fi_explorer_admin","fi_explorer_version","fi_explorer_frozen_content",
    "fi_explorer_group","fi_explorer_submission","fi_explorer_review","fi_explorer_publication",
    "fi_explorer_audit","fi_explorer_cp_pending","fi_explorer_cp_suggestion","fi_explorer_discovery_event","fi_explorer_photo_event")
MARKERS=("fi_production_schema","cp_schema","fi_discovery_schema","fi_product_photo_schema","fi_product_photo_priority_schema")
AXES=(("sweetness","Sweetness"),("tartness","Tartness"),("creaminess","Creaminess"),
      ("richness","Richness"),("brightness","Brightness"),("cooling","Cooling"),("crispness","Crispness"))

def upgrade(connection):
    if connection.dialect.name!="postgresql":raise RuntimeError("Product Explorer production migration requires PostgreSQL.")
    m=metadata();existing=set(inspect(connection).get_table_names())
    if existing.intersection(table.name for table in feature_tables(m)):
        raise RuntimeError("Existing Product Explorer tables require exact schema reconciliation.")
    for table in feature_tables(m):table.create(connection)
    install_guards(connection)
    for name in MARKERS:
        connection.execute(insert(m.tables[name]).values(id=1,version=1))
    connection.execute(insert(m.tables["fi_catalog_observations"]).values(id=1,generation=0))
    connection.execute(insert(m.tables["fi_refresh_lease"]).values(id=1,lease_key="explorer-square-refresh"))
    connection.execute(insert(m.tables["fi_sensory_axes"]),[
        {"slug":slug,"label":label,"description":"Normalized 0 to 1 sensory intensity"} for slug,label in AXES])

def downgrade(connection):
    m=metadata()
    ignored=set(MARKERS)|{"fi_sensory_axes","fi_catalog_observations","fi_refresh_lease"}
    for table in feature_tables(m):
        if table.name not in ignored and connection.scalar(select(func.count()).select_from(table)):
            raise RuntimeError("Retained Product Explorer data requires a compatible application rollback.")
    if connection.scalar(select(m.tables["fi_catalog_observations"].c.snapshot).where(
            m.tables["fi_catalog_observations"].c.id==1)) is not None:
        raise RuntimeError("Retained Product Explorer observations require a compatible application rollback.")
    if connection.scalar(select(m.tables["fi_refresh_lease"].c.owner).where(
            m.tables["fi_refresh_lease"].c.id==1)) is not None:
        raise RuntimeError("An active Product Explorer lease prevents schema retirement.")
    for table in reversed(feature_tables(m)):table.drop(connection)
    for name in FUNCTIONS:connection.exec_driver_sql("DROP FUNCTION "+name+"()")

def verify_schema(engine):
    if engine.dialect.name!="postgresql":raise RuntimeError("Product Explorer production storage requires PostgreSQL.")
    m=metadata()
    with engine.connect() as c:
        inspector=inspect(c);present=set(inspector.get_table_names())
        required={table.name for table in feature_tables(m)}
        if required-present:raise RuntimeError("Product Explorer migration0058 is incomplete.")
        for table in feature_tables(m):
            actual={column["name"] for column in inspector.get_columns(table.name)}
            if set(table.c.keys())-actual:raise RuntimeError("Product Explorer table columns are incomplete: "+table.name)
        for name in MARKERS:
            rows=c.execute(select(m.tables[name])).mappings().all()
            if len(rows)!=1 or rows[0]["id"]!=1 or rows[0]["version"]!=1:
                raise RuntimeError("Unsupported Product Explorer feature version: "+name)
        triggers=set(c.scalars(text("SELECT tgname FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=current_schema() AND NOT t.tgisinternal")))
        expected={name+"_immutable" for name in IMMUTABLE_TABLES}
        expected|={name+"_frozen" for name in VERSION_CONTENT}
        expected|={name+"_admin" for name in ("fi_administration_operations","fi_review_decisions",
            "fi_publication_revisions","fi_discovery_operations","fi_product_photo_operations")}
        expected|={name+"_audit" for name in ("fi_publication_revisions","fi_employee_suggestions","fi_review_decisions")}
        expected|={"fi_classification_version_guard","fi_review_group_guard","fi_employee_submission_guard",
            "fi_review_decision_guard","fi_publication_chain","cp_pending_guard","cp_suggestion_guard",
            "fi_discovery_event_guard","fi_product_photo_event_guard"}
        if expected-triggers:raise RuntimeError("Product Explorer integrity guards are incomplete.")
        for singleton in ("fi_catalog_observations","fi_refresh_lease"):
            if c.scalar(select(func.count()).select_from(m.tables[singleton]))!=1:
                raise RuntimeError("Product Explorer runtime singleton is unavailable.")
    return {"schemaVersion":1,"tables":len(required),"guardCount":len(expected)}
