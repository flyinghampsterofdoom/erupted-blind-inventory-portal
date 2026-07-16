from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.auth import Principal, Role
from app.v2.audit import V2AuditEvent, redact_metadata, write_v2_audit_event
from app.v2.feature_exposure import FeatureExposure
from app.v2.results import ActionResult, ResultKind, SaveOutcome
from app.v2.statuses import presentation_status
from app.v2.store_scope import ScopeMode, ScopedStore, resolve_store_scope


STORES = (ScopedStore(10, 'North'), ScopedStore(20, 'South'))


def _principal(role, store_id=None):
    return Principal(id=3, username='employee', role=role, store_id=store_id, active=True)


def test_assigned_store_is_locked_and_write_compatible():
    scope = resolve_store_scope(principal=_principal(Role.STORE, 10), authorized_stores=STORES[:1])
    assert scope.mode == ScopeMode.ASSIGNED
    assert scope.store_ids == (10,)
    assert scope.locked is True
    assert scope.write_compatible is True


def test_store_employee_cannot_expand_scope_with_query_values():
    with pytest.raises(HTTPException) as exc:
        resolve_store_scope(
            principal=_principal(Role.STORE, 10),
            authorized_stores=STORES[:1],
            requested_store_ids=(20,),
        )
    assert exc.value.status_code == 403


def test_management_scope_supports_single_multiple_and_all_reads():
    principal = _principal(Role.LEAD)
    single = resolve_store_scope(
        principal=principal, authorized_stores=STORES, requested_store_ids=(10,)
    )
    multiple = resolve_store_scope(
        principal=principal, authorized_stores=STORES, requested_store_ids=(10, 20)
    )
    all_scope = resolve_store_scope(principal=principal, authorized_stores=STORES, request_all=True)
    assert (single.mode, multiple.mode, all_scope.mode) == (
        ScopeMode.SINGLE,
        ScopeMode.MULTIPLE,
        ScopeMode.ALL,
    )


def test_unauthorized_partial_intersection_is_forbidden_not_silently_reduced():
    with pytest.raises(HTTPException) as exc:
        resolve_store_scope(
            principal=_principal(Role.ADMIN),
            authorized_stores=STORES,
            requested_store_ids=(10, 999),
        )
    assert exc.value.status_code == 403


def test_ordinary_write_requires_one_store():
    with pytest.raises(HTTPException) as exc:
        resolve_store_scope(
            principal=_principal(Role.ADMIN), authorized_stores=STORES, request_all=True, for_write=True
        )
    assert exc.value.status_code == 409


def test_route_level_scope_failure_is_clear():
    app = FastAPI()

    @app.get('/scope')
    def scope(request: Request):
        raw = tuple(int(value) for value in request.query_params.getlist('store_id'))
        resolved = resolve_store_scope(
            principal=_principal(Role.LEAD), authorized_stores=STORES, requested_store_ids=raw
        )
        return {'ids': resolved.store_ids, 'mode': resolved.mode.value}

    client = TestClient(app)
    assert client.get('/scope?store_id=10').json() == {'ids': [10], 'mode': 'single'}
    response = client.get('/scope?store_id=999')
    assert response.status_code == 403


def test_status_registry_preserves_unknown_values_and_sync_category():
    assert presentation_status('DRAFT').label == 'Draft'
    assert presentation_status('SUCCESS').category == 'sync'
    unknown = presentation_status('NEW_VENDOR_STATE')
    assert unknown.key == 'unknown'
    assert 'NEW_VENDOR_STATE' in unknown.label


def test_action_result_distinguishes_local_save_and_external_failure():
    result = ActionResult(
        kind=ResultKind.EXTERNAL_FAILURE,
        message='Saved locally; Square did not complete.',
        save_outcome=SaveOutcome.LOCAL_SAVED,
        safe_retry=True,
    ).as_json()
    assert result['kind'] == 'external_failure'
    assert result['save_outcome'] == 'local_saved'
    assert result['safe_retry'] is True
    assert result['correlation_id']


def test_feature_exposure_is_separate_global_or_per_principal_data():
    exposure = FeatureExposure(frozenset({'global'}), frozenset({(3, 'tester')}))
    assert exposure.enabled('global', principal_id=99)
    assert exposure.enabled('tester', principal_id=3)
    assert not exposure.enabled('tester', principal_id=4)
    assert not exposure.enabled('unfinished', principal_id=3)
    assert not FeatureExposure.from_settings().enabled('unfinished', principal_id=3)


class _AuditDb:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def test_v2_audit_adapter_redacts_sensitive_metadata_and_keeps_actor():
    with pytest.raises(ValueError, match='authenticated employee'):
        V2AuditEvent(actor_principal_id=0, action='CREATED', domain='FORMS', entity_type='example', entity_id=5)
    assert redact_metadata({'token': 'unsafe', 'nested': {'password_hash': 'unsafe'}}) == {
        'token': '[REDACTED]',
        'nested': {'password_hash': '[REDACTED]'},
    }
    db = _AuditDb()
    write_v2_audit_event(
        db,
        event=V2AuditEvent(
            actor_principal_id=44,
            action='CREATED',
            domain='FORMS',
            entity_type='example',
            entity_id=5,
            store_ids=(10,),
            metadata={'access_token': 'unsafe'},
        ),
        ip=None,
    )
    assert db.rows[0].actor_principal_id == 44
    assert db.rows[0].action == 'V2:FORMS:CREATED'
    assert db.rows[0].meta['metadata']['access_token'] == '[REDACTED]'
