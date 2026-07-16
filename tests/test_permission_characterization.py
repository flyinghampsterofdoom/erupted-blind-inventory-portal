from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.auth import Principal, Role, assert_store_scope, is_admin_role, require_role
from app.routers.management import employee_logs_access, employee_logs_admin_access
from app.routers.v2 import _visible_navigation
from app.services.access_control_service import (
    effective_permission_flags,
    fallback_allowed_for_role,
    principal_has_permission,
)
from app.services.employee_log_service import list_employees_for_entry


CAPABILITIES = (
    'management.access',
    'management.admin',
    'management.groups',
    'management.users',
    'store.access',
)

EXPECTED_DEFAULTS = {
    Role.ADMIN: (True, True, True, True, False),
    Role.MANAGER: (True, True, True, False, False),
    Role.LEAD: (True, False, False, False, False),
    Role.STORE: (False, False, False, False, True),
}


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _OverrideDb:
    def __init__(self, *values):
        self.values = list(values)

    def execute(self, _query):
        return _ScalarResult(self.values.pop(0))


class _AllResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _BulkOverrideDb:
    def __init__(self, principal_rows, role_rows):
        self.results = [_AllResult(principal_rows), _AllResult(role_rows)]

    def execute(self, _query):
        return self.results.pop(0)


def _principal(role=Role.LEAD, store_id=None):
    return Principal(id=7, username='person', role=role, store_id=store_id, active=True)


@pytest.mark.parametrize('role', list(Role))
def test_default_capability_matrix(role):
    actual = tuple(fallback_allowed_for_role(role=role, permission_key=key) for key in CAPABILITIES)
    assert actual == EXPECTED_DEFAULTS[role]


def test_role_allow_and_deny_overrides_replace_fallback():
    principal = _principal(Role.LEAD)
    assert principal_has_permission(
        _OverrideDb(None, True),
        principal=principal,
        permission_key='management.admin',
        fallback_allowed=False,
    ) is True
    assert principal_has_permission(
        _OverrideDb(None, False),
        principal=principal,
        permission_key='management.access',
        fallback_allowed=True,
    ) is False


def test_principal_override_precedes_role_override_and_fallback():
    principal = _principal(Role.LEAD)
    assert principal_has_permission(
        _OverrideDb(False),
        principal=principal,
        permission_key='management.access',
        fallback_allowed=True,
    ) is False
    assert principal_has_permission(
        _OverrideDb(True),
        principal=principal,
        permission_key='management.admin',
        fallback_allowed=False,
    ) is True


def test_bulk_effective_flags_preserve_principal_role_fallback_precedence():
    flags = effective_permission_flags(
        _BulkOverrideDb(
            principal_rows=[
                SimpleNamespace(permission_key='management.access', allowed=False),
                SimpleNamespace(permission_key='nav.reports.cogs', allowed=True),
            ],
            role_rows=[
                SimpleNamespace(permission_key='management.access', allowed=True),
                SimpleNamespace(permission_key='nav.reports.inventory_velocity', allowed=True),
            ],
        ),
        principal=_principal(Role.LEAD),
    )
    assert flags['management.access'] is False
    assert flags['nav.reports.cogs'] is True
    assert flags['nav.reports.inventory_velocity'] is True
    assert flags['nav.store_operations.daily_chores'] is True
    assert flags['management.admin'] is False


def test_literal_admin_dependency_ignores_manager_legacy_admin_behavior():
    literal_admin = require_role(Role.ADMIN)
    assert literal_admin(_principal(Role.ADMIN)).role == Role.ADMIN
    with pytest.raises(HTTPException) as exc:
        literal_admin(_principal(Role.MANAGER))
    assert exc.value.status_code == 403
    assert is_admin_role(Role.MANAGER) is True
    assert is_admin_role(Role.LEAD) is False


def test_employee_log_wrappers_preserve_lead_and_legacy_admin_rules():
    assert employee_logs_access(_principal(Role.LEAD)).role == Role.LEAD
    with pytest.raises(HTTPException):
        employee_logs_access(_principal(Role.STORE, store_id=2))
    assert employee_logs_admin_access(_principal(Role.ADMIN)).role == Role.ADMIN
    assert employee_logs_admin_access(_principal(Role.MANAGER)).role == Role.MANAGER
    with pytest.raises(HTTPException):
        employee_logs_admin_access(_principal(Role.LEAD))


class _EmployeeRows:
    def scalars(self):
        return self

    def all(self):
        return []


class _CaptureDb:
    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))
        return _EmployeeRows()


def test_visible_to_leads_filter_is_applied_unless_hidden_access_is_explicit():
    lead_db = _CaptureDb()
    list_employees_for_entry(lead_db, include_hidden=False)
    assert 'employees.visible_to_leads IS true' in lead_db.statements[0]
    admin_db = _CaptureDb()
    list_employees_for_entry(admin_db, include_hidden=True)
    assert 'employees.visible_to_leads IS true' not in admin_db.statements[0]


def test_store_ownership_enforcement_is_store_role_only():
    assert_store_scope(_principal(Role.STORE, store_id=10), 10)
    with pytest.raises(HTTPException) as exc:
        assert_store_scope(_principal(Role.STORE, store_id=10), 20)
    assert exc.value.status_code == 403
    assert_store_scope(_principal(Role.LEAD), 20)


def test_navigation_visibility_is_permission_data_not_literal_route_authorization():
    request = SimpleNamespace(
        url=SimpleNamespace(path='/v2/overview'),
        state=SimpleNamespace(permission_flags={'management.access': True, 'management.admin': False})
    )
    keys = [section.key for section in _visible_navigation(request)]
    assert keys == ['overview']
