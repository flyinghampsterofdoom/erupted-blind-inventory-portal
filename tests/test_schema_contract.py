from pathlib import Path
import hashlib

import pytest

from app.schema_contract import HEAD_REVISION, UnsupportedSchemaError, assert_supported_schema, stamp_matching_database


def test_baseline_uses_full_deployed_schema_and_contains_runtime_gtin_columns():
    schema = Path('sql/schema.sql').read_text(encoding='utf-8')
    migration = Path('migrations/versions/20260715_0001_v1_schema_baseline.py').read_text(encoding='utf-8')
    assert schema.count('CREATE TABLE IF NOT EXISTS') == 72
    assert 'CREATE EXTENSION IF NOT EXISTS citext' in schema
    assert 'vendor_sku_configs ADD COLUMN IF NOT EXISTS gtin' in schema
    assert 'purchase_order_lines ADD COLUMN IF NOT EXISTS gtin' in schema
    assert "revision = '20260715_0001'" in migration
    assert hashlib.sha256(schema.encode('utf-8')).hexdigest() in migration


class _RevisionEngine:
    pass


def test_startup_accepts_only_supported_revision(monkeypatch):
    monkeypatch.setattr('app.schema_contract.current_revision', lambda _engine: HEAD_REVISION)
    assert_supported_schema(_RevisionEngine())
    monkeypatch.setattr('app.schema_contract.current_revision', lambda _engine: None)
    with pytest.raises(UnsupportedSchemaError, match='unversioned'):
        assert_supported_schema(_RevisionEngine())


def test_stamp_rejects_unknown_revision_before_connecting():
    with pytest.raises(UnsupportedSchemaError, match='unsupported revision'):
        stamp_matching_database(
            database_url='postgresql+psycopg:///unused',
            reference_url='postgresql+psycopg:///unused',
            revision='unknown',
        )
