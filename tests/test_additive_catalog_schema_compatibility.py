"""Bounded compatibility for the additive catalog policy cache migration."""
import pytest
from app import schema_contract as schema

@pytest.mark.parametrize('revision',['20261024_0050','20261026_0051'])
def test_old_business_runtime_accepts_reviewed_additive_schema(monkeypatch,revision):
    monkeypatch.setattr(schema.settings,'schema_revision_check_enabled',True)
    monkeypatch.setattr(schema,'current_revision',lambda engine:revision)
    schema.assert_supported_schema(object())

@pytest.mark.parametrize('revision',[None,'20261024_0049','20261026_0052'])
def test_old_business_runtime_rejects_unreviewed_schema(monkeypatch,revision):
    monkeypatch.setattr(schema.settings,'schema_revision_check_enabled',True)
    monkeypatch.setattr(schema,'current_revision',lambda engine:revision)
    with pytest.raises(schema.UnsupportedSchemaError):schema.assert_supported_schema(object())


def test_compatibility_does_not_change_the_migration_target():
    assert schema.HEAD_REVISION=='20261024_0050'
    assert schema.SUPPORTED_REVISIONS==frozenset({'20261024_0050','20261026_0051'})
